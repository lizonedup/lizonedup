/*
 * lizone.c -- LiZoneDedup implementation for FEMU ZNS mode.
 * See lizone.h for the mode overview.
 *
 * This file is the artifact's femu/lizone.c with the spdkhost extensions
 * described in lizone.h (fallback hint probing, the reset-pending
 * protocol, host-discard UREFs, and the stats/zone-query introspection
 * commands).  Original evaluation logic is unchanged; extensions are
 * delimited by "spdkhost:" comments.
 */
#include "qemu/osdep.h"
#include <math.h>
#include "qemu/units.h"
#include "qapi/error.h"
#include "hw/pci/pci.h"
#include "hw/qdev-properties.h"
#include "qemu/log.h"
#include "qemu/thread.h"
#include "zns.h"
#include "lizone.h"

/* controller compute (ns), calibrated to a 2 GHz quad-core ARM */
#define LZ_T_SHA_NS        6800
#define LZ_T_SHA_HW_NS     2000   /* hardware hash engine (unified spec) */
#define LZ_T_SIMHASH_NS    1400
#define LZ_T_ROUTE_NS       100
#define LZ_T_FILTER_NS      150
#define LZ_T_SCAN_NS        250
#define LZ_T_DREF_NS         50
#define LZ_T_STATUS_NS       50
#define LZ_T_DPARSE_NS      100
#define LZ_T_LZ4_NS        8200
#define LZ_T_DMA_NS         500

#define LZ_INS_BYTES        22
#define LZ_DREF_BYTES       16

static LZCtrl LZ;
static QemuMutex lz_lock;
static int lz_sha_hw = 0;      /* 1 = SHA on the hardware engine */
static int lz_inited;

/* ------------------------------------------------------------------ */
/* 64-bit -> 64-bit open-addressing map (used for the global index,
 * the seen-set, and the descriptor table)                             */

typedef struct lz_map_ent {
    uint64_t key;
    uint64_t val;
    uint64_t seq;
} lz_map_ent;

typedef struct lz_map {
    lz_map_ent *e;
    uint32_t cap;      /* power of two */
    uint32_t used;
} lz_map;

static void lzmap_init(lz_map *m)
{
    m->cap = 1 << 16;
    m->e = g_malloc0(sizeof(lz_map_ent) * m->cap);
    m->used = 0;
}

static lz_map_ent *lzmap_find(lz_map *m, uint64_t key)
{
    uint32_t h = (uint32_t)(key ^ (key >> 32)) & (m->cap - 1);
    while (m->e[h].key || m->e[h].val) {
        if (m->e[h].key == key && m->e[h].val) {
            return &m->e[h];
        }
        h = (h + 1) & (m->cap - 1);
    }
    return NULL;
}

static void lzmap_grow(lz_map *m)
{
    lz_map_ent *oe = m->e;
    uint32_t ocap = m->cap, i;
    m->cap *= 2;
    m->e = g_malloc0(sizeof(lz_map_ent) * m->cap);
    for (i = 0; i < ocap; i++) {
        if (oe[i].key || oe[i].val) {
            uint32_t h = (uint32_t)(oe[i].key ^ (oe[i].key >> 32)) &
                         (m->cap - 1);
            while (m->e[h].key || m->e[h].val) {
                h = (h + 1) & (m->cap - 1);
            }
            m->e[h] = oe[i];
        }
    }
    g_free(oe);
}

static void lzmap_put(lz_map *m, uint64_t key, uint64_t val)
{
    uint32_t h;
    if (m->used * 2 >= m->cap) {
        lzmap_grow(m);
    }
    h = (uint32_t)(key ^ (key >> 32)) & (m->cap - 1);
    while (m->e[h].key || m->e[h].val) {
        if (m->e[h].key == key) {
            m->e[h].val = val;
            return;
        }
        h = (h + 1) & (m->cap - 1);
    }
    m->e[h].key = key;
    m->e[h].val = val;
    m->e[h].seq = 0;
    m->used++;
}

static int lzmap_has(lz_map *m, uint64_t key)
{
    return lzmap_find(m, key) != NULL;
}

static void lzmap_del(lz_map *m, uint64_t key)
{
    lz_map_ent *e = lzmap_find(m, key);
    if (e) {
        e->val = 0;                    /* tombstone via val=0, keep key */
    }
}

static lz_map LZ_global, LZ_seen, LZ_desc;

/* ------------------------------------------------------------------ */
/* hashing                                                             */

static uint64_t lz_mix64(uint64_t x)
{
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

static uint64_t lz_fingerprint(const uint8_t *pg)
{
    uint64_t h = 0x243F6A8885A308D3ULL;
    int i;
    for (i = 0; i < 4096 / 64; i++) {
        uint64_t w;
        memcpy(&w, pg + i * 64, 8);
        h = lz_mix64(h ^ w);
        memcpy(&w, pg + i * 64 + 56, 8);
        h = lz_mix64(h ^ w);
    }
    return lz_mix64(lz_mix64(h));
}

static uint32_t lz_simhash(const uint8_t *pg)
{
    int votes[LZ_LSH_BITS];
    int b, j;
    uint32_t digest = 0;

    memset(votes, 0, sizeof(votes));
    for (b = 0; b < 4096 / 64; b++) {
        uint64_t h = 0x1122334455667788ULL;
        const uint64_t *w = (const uint64_t *)(pg + b * 64);
        for (j = 0; j < 8; j++) {
            h ^= w[j];
            h *= 0x9E3779B97F4A7C15ULL;
            h ^= h >> 29;
        }
        for (j = 0; j < LZ_LSH_BITS; j++) {
            votes[j] += (h >> j) & 1 ? 1 : -1;
        }
    }
    for (j = 0; j < LZ_LSH_BITS; j++) {
        if (votes[j] > 0) {
            digest |= 1u << j;
        }
    }
    return digest;
}

static float lz_cr_analyze(const uint8_t *pg)
{
    /* integer-only compressibility estimator (no libm): adjacent-equal
     * byte fraction + zero coverage, calibrated to page-class LZ4 */
    int eq = 0, zero = 0, i;
    for (i = 1; i < 4096; i++) {
        if (pg[i] == pg[i - 1]) {
            eq++;
        }
    }
    for (i = 0; i < 4096; i += 16) {
        if (pg[i] == 0 && pg[i + 1] == 0 && pg[i + 8] == 0) {
            zero++;
        }
    }
    if (zero >= 250) {
        return 0.02f;
    }
    float f = (float)eq / 4095.0f;
    if (f > 0.35f) {
        f = 0.35f;
    }
    return 0.30f + 0.70f * (1.0f - f / 0.35f);
}

/* ------------------------------------------------------------------ */
/* per-zone identity index                                             */

static void lz_zone_grow(LZZone *z)
{
    uint32_t ncap = z->cap ? z->cap * 2 : 1024;
    lz_ent *nt = g_malloc0(sizeof(lz_ent) * ncap);
    uint32_t i;
    for (i = 0; i < z->cap; i++) {
        if (z->tab[i].fp) {
            uint32_t h = z->tab[i].fp & (ncap - 1);
            while (nt[h].fp) {
                h = (h + 1) & (ncap - 1);
            }
            nt[h] = z->tab[i];
        }
    }
    g_free(z->tab);
    z->tab = nt;
    z->cap = ncap;
    LZ.dram_bytes += sizeof(lz_ent) * ncap;
}

static lz_ent *lz_tab_lookup(LZZone *z, uint64_t fp)
{
    uint32_t h, n;
    if (!z->cap) {
        return NULL;
    }
    h = fp & (z->cap - 1);
    for (n = 0; n < z->cap; n++) {
        lz_ent *e = &z->tab[h];
        if (!e->fp) {
            return NULL;
        }
        if (e->fp == fp) {
            return e;
        }
        h = (h + 1) & (z->cap - 1);
    }
    return NULL;
}

static void lz_tab_insert(LZZone *z, uint64_t fp, uint64_t lba)
{
    uint32_t h;
    if (z->used * 2 >= z->cap) {
        lz_zone_grow(z);
    }
    h = fp & (z->cap - 1);
    while (z->tab[h].fp) {
        if (z->tab[h].fp == fp) {
            return;
        }
        h = (h + 1) & (z->cap - 1);
    }
    z->tab[h].fp = fp;
    z->tab[h].lba = (uint32_t)lba;
    z->tab[h].tag = z->used;
    z->used++;
}

static void lz_bloom_init(LZZone *z, uint32_t keys)
{
    double bits = keys * 9.6 < 64 ? 64 : keys * 9.6;
    z->bloom_bits = ((uint32_t)bits + 63) / 64 * 64;
    z->bloom = g_malloc0(z->bloom_bits / 8);
    z->bloom_keys = 0;
    LZ.dram_bytes += z->bloom_bits / 8;
}

static void lz_bloom_add(LZZone *z, uint64_t fp)
{
    uint64_t h1 = lz_mix64(fp), h2 = lz_mix64(fp ^ 0xA5A5A5A5A5A5A5A5ULL);
    int k;
    if (!z->bloom) {
        return;
    }
    for (k = 0; k < 6; k++) {
        uint64_t idx = (h1 + (uint64_t)k * h2) % z->bloom_bits;
        z->bloom[idx >> 6] |= 1ULL << (idx & 63);
    }
    z->bloom_keys++;
}

static int lz_bloom_may(LZZone *z, uint64_t fp)
{
    uint64_t h1 = lz_mix64(fp), h2 = lz_mix64(fp ^ 0xA5A5A5A5A5A5A5A5ULL);
    int k;
    if (!z->bloom) {
        return 1;
    }
    for (k = 0; k < 6; k++) {
        uint64_t idx = (h1 + (uint64_t)k * h2) % z->bloom_bits;
        if (!(z->bloom[idx >> 6] & (1ULL << (idx & 63)))) {
            return 0;
        }
    }
    return 1;
}

/* ------------------------------------------------------------------ */
/* helpers                                                             */

static uint8_t pg_buf[4096] __attribute__((aligned(4096)));

static int lz_req_page(FemuCtrl *n, NvmeRequest *req, uint32_t page,
                       uint8_t *out)
{
    uint64_t off = (uint64_t)page * 4096;
    uint64_t pos = 0;
    int i;
    for (i = 0; i < req->qsg.nsg && pos < 4096; i++) {
        ScatterGatherEntry *sg = &req->qsg.sg[i];
        uint64_t skip;
        if (off >= sg->len) {
            off -= sg->len;
            continue;
        }
        skip = sg->len - off;
        if (skip > 4096 - pos) {
            skip = 4096 - pos;
        }
        pci_dma_read(PCI_DEVICE(n), sg->base + off, out + pos, skip);
        pos += skip;
        off = 0;
    }
    if (pos < 4096) {
        memset(out + pos, 0, 4096 - pos);
    }
    return pos ? 0 : -1;
}

static uint32_t lz_group_of(uint32_t lsh)
{
    uint32_t bucket = lsh >> (LZ_LSH_BITS - LZ_ROUTE_BITS);
    return bucket >> (LZ_ROUTE_BITS - 5);   /* 32 route groups */
}

/* LPN conversion: LOGICAL_PAGE_SIZE(4K)/lbasz LBAs per LPN */
static inline uint64_t lz_lba2lpn(FemuCtrl *n, uint64_t lba)
{
    uint32_t spp = 4096 / n->zns->lbasz ? 4096 / n->zns->lbasz : 1;
    return lba / spp;
}

static inline uint32_t lz_zone_of_lba(FemuCtrl *n, uint64_t lba)
{
    if (n->zone_size_log2 > 0) {
        return lba >> n->zone_size_log2;
    }
    return lba / n->zone_size;
}

static void lz_ensure_zone(FemuCtrl *n, int zid)
{
    if (zid < LZ.n_zones) {
        return;
    }
    while (zid >= LZ.cap_zones) {
        LZ.cap_zones = LZ.cap_zones ? LZ.cap_zones * 2 : 256;
        LZ.zones = g_realloc(LZ.zones, sizeof(LZZone) * LZ.cap_zones);
    }
    while (LZ.n_zones <= zid) {
        memset(&LZ.zones[LZ.n_zones], 0, sizeof(LZZone));
        LZ.zones[LZ.n_zones].zid = LZ.n_zones;
        LZ.n_zones++;
    }
}

/* metadata NAND timing (plane clocks) */
static uint64_t lz_md_pages_io(struct zns_ssd *zns, int pages, int is_read,
                               uint64_t stime)
{
    struct ppa ppa;
    struct nand_cmd nc;
    uint64_t maxlat = 0, lat;
    int i;
    static uint32_t md_seq;

    memset(&ppa, 0, sizeof(ppa));
    nc.type = USER_IO;
    nc.cmd = is_read ? NAND_READ : NAND_WRITE;
    nc.stime = stime;

    for (i = 0; i < pages; i++) {
        ppa.g.ch = (md_seq++) % zns->num_ch;
        ppa.g.fc = 0;
        ppa.g.pl = 0;
        ppa.g.blk = 0;
        ppa.g.pg = 0;
        ppa.g.spg = 0;
        lat = zns_advance_status(zns, &ppa, &nc);
        if (lat > maxlat) {
            maxlat = lat;
        }
    }
    return maxlat;
}

static uint64_t lz_md_append(struct zns_ssd *zns, uint64_t bytes,
                             uint64_t stime)
{
    uint64_t lat = 0;
    LZ.md_pending_bytes += bytes;
    LZ.md_write_bytes += bytes;
    while (LZ.md_pending_bytes >= 4096) {
        LZ.md_pending_bytes -= 4096;
        LZ.md_pages_written++;
        lat += lz_md_pages_io(zns, 1, 0, stime);
    }
    return lat;
}

/* ------------------------------------------------------------------ */
/* init / exit                                                         */

void lz_init(FemuCtrl *n)
{
    memset(&LZ, 0, sizeof(LZ));
    LZ.mode = n->zns_params.lz_mode;
    lz_sha_hw = n->zns_params.lz_sha_hw;
    LZ.r = n->zns_params.lz_r > 0 ? n->zns_params.lz_r : 2;
    if (LZ.r > 6) {
        LZ.r = 6;
    }
    LZ.fallback = n->zns_params.lz_fallback;
    LZ.enabled = (LZ.mode != LZ_MODE_NONE);
    LZ.n = n;
    LZ.zns = n->zns;
    memset(LZ.primary, -1, sizeof(LZ.primary));
    qemu_mutex_init(&lz_lock);

    if (LZ.enabled) {
        lzmap_init(&LZ_seen);
        lzmap_init(&LZ_desc);
        if (LZ.mode == LZ_MODE_ORACLE || LZ.mode == LZ_MODE_GLOBALINDEX) {
            lzmap_init(&LZ_global);
        }
    }
    lz_inited = 1;
    femu_log("LiZoneDedup: mode=%d r=%d fallback=%d (%s)\n", LZ.mode, LZ.r,
             LZ.fallback, LZ.enabled ? "enabled" : "passthrough");
}

void lz_exit(FemuCtrl *n)
{
    if (!lz_inited) {
        return;
    }
    lz_dump_stats(n);
}

int lz_is_enabled(FemuCtrl *n)
{
    return lz_inited && LZ.enabled;
}

/* ------------------------------------------------------------------ */
/* C1 routing                                                          */

static int lz_zone_is_appendable(FemuCtrl *n, int zid)
{
    NvmeZone *zone;
    if (zid < 0 || zid >= (int)n->num_zones) {
        return 0;
    }
    zone = &n->zone_array[zid];
    if (zone->d.wp >= zns_zone_wr_boundary(zone)) {
        return 0;
    }
    switch (zns_get_zone_state(zone)) {
    case NVME_ZONE_STATE_READ_ONLY:
    case NVME_ZONE_STATE_OFFLINE:
    case NVME_ZONE_STATE_FULL:
        return 0;
    default:
        return 1;
    }
}

static void lz_seal_zone(FemuCtrl *n, int zid)
{
    LZZone *z;
    uint64_t idx_bytes, flt_bytes, pages;
    if (zid < 0 || zid >= LZ.n_zones) {
        return;
    }
    z = &LZ.zones[zid];
    if (z->state != 0) {
        return;
    }
    z->state = 1;
    z->sealed_at = LZ.n_writes;
    LZ.n_seals++;
    idx_bytes = (uint64_t)z->used * 22;
    flt_bytes = (uint64_t)z->used * 9.84 / 8 + 64;
    pages = (idx_bytes + flt_bytes + 4095) / 4096;
    LZ.md_write_bytes += pages * 4096;
    LZ.md_pages_written += pages;
}

int lz_pick_zone(FemuCtrl *n, uint32_t lsh)
{
    uint32_t g = lz_group_of(lsh);
    int zid = LZ.primary[g];
    int nz = -1, i;

    if (zid >= 0 && lz_zone_is_appendable(n, zid)) {
        return zid;
    }
    if (zid >= 0) {
        lz_ensure_zone(n, zid);
        lz_seal_zone(n, zid);
        memmove(&LZ.lineage[g][1], &LZ.lineage[g][0],
                sizeof(int) * (LZ_LINEAGE_DEPTH - 1));
        LZ.lineage[g][0] = zid;
        if (LZ.nlin[g] < LZ_LINEAGE_DEPTH) {
            LZ.nlin[g]++;
        }
    }
    for (i = 0; i < (int)n->num_zones; i++) {
        uint8_t zs = zns_get_zone_state(&n->zone_array[i]);
        if (zs == NVME_ZONE_STATE_EMPTY) {
            nz = i;
            break;
        }
    }
    if (nz < 0) {
        for (i = 0; i < (int)n->num_zones; i++) {
            if (lz_zone_is_appendable(n, i)) {
                nz = i;
                break;
            }
        }
    }
    if (nz < 0) {
        return zid >= 0 ? zid : 0;
    }
    lz_ensure_zone(n, nz);
    if (LZ.zones[nz].state == 2 || LZ.zones[nz].state == 1) {
        /* recycling a reset zone: fresh structures */
        LZ.zones[nz].state = 0;
        LZ.zones[nz].used = 0;
        if (LZ.zones[nz].bloom) {
            memset(LZ.zones[nz].bloom, 0, LZ.zones[nz].bloom_bits / 8);
            LZ.zones[nz].bloom_keys = 0;
        }
    }
    if (!LZ.zones[nz].tab) {
        LZ.zones[nz].cap = 1024;
        LZ.zones[nz].tab = g_malloc0(sizeof(lz_ent) * 1024);
        LZ.zones[nz].used = 0;
        LZ.dram_bytes += sizeof(lz_ent) * 1024;
        lz_bloom_init(&LZ.zones[nz], 8192);
    }
    LZ.zones[nz].written_at = LZ.n_writes;
    LZ.primary[g] = nz;
    return nz;
}

/* ------------------------------------------------------------------ */
/* write decision                                                      */

uint64_t lz_decide_write(FemuCtrl *n, NvmeRequest *req, uint32_t zone_idx)
{
    return lz_decide_write_hint(n, req, zone_idx, -1);
}

uint64_t lz_decide_write_hint(FemuCtrl *n, NvmeRequest *req, uint32_t zone_idx,
                              int hint_zone)
{
    uint64_t comp_ns = (lz_sha_hw ? LZ_T_SHA_HW_NS : LZ_T_SHA_NS)
                 + LZ_T_SIMHASH_NS + LZ_T_ROUTE_NS;
    uint64_t fp;
    uint32_t lsh;
    uint8_t *pg = pg_buf;

    if (!lz_inited || !LZ.enabled) {
        return LZ_T_DMA_NS;
    }
    req->lz_zone = 1;   /* a dedup decision was made for this request */
    if (LZ.mode == LZ_MODE_BALLOON) {
        lz_req_page(n, req, 0, pg);
        req->lz_cr = lz_cr_analyze(pg);
        req->lz_hit = 0;
        LZ.n_writes++;
        LZ.compute_ns += LZ_T_LZ4_NS + LZ_T_DMA_NS;
        return LZ_T_LZ4_NS + LZ_T_DMA_NS;
    }

    if (lz_req_page(n, req, 0, pg) != 0) {
        req->lz_hit = 0;
        return comp_ns;
    }
    fp = lz_fingerprint(pg);
    lsh = lz_simhash(pg);
    req->lz_fp = fp;
    req->lz_cr = 1.0f;

    qemu_mutex_lock(&lz_lock);
    LZ.n_writes++;
    LZ.compute_ns += comp_ns;

    if (LZ.mode == LZ_MODE_ORACLE || LZ.mode == LZ_MODE_GLOBALINDEX) {
        lz_map_ent *e;
        if (LZ.mode == LZ_MODE_GLOBALINDEX) {
            LZ.md_read_pages += 1.3;
            comp_ns += LZ_T_SCAN_NS * 2;
        }
        e = lzmap_find(&LZ_global, fp);
        if (e) {
            req->lz_hit = 1;
            req->lz_src_lpn = e->val;
            LZ.n_hits++;
            qemu_mutex_unlock(&lz_lock);
            return comp_ns;
        }
        lzmap_put(&LZ_global, fp, lz_lba2lpn(n, req->slba));
        if (LZ.mode == LZ_MODE_GLOBALINDEX) {
            LZ.md_read_pages += 1.0;
            LZ.md_rmw_pages += 1;
            LZ.md_write_bytes += 4096;
        }
        qemu_mutex_unlock(&lz_lock);
        return comp_ns;
    }

    /* per-zone candidate search */
    {
        uint32_t g = lz_group_of(lsh);
        int cands[LZ_LINEAGE_DEPTH + 8];
        int nc = 0, i, probed = 0;
        int src_zone = -1;
        uint64_t src_lpn = 0;

        /* spdkhost: fallback mode probes the host-chosen zone first */
        if (hint_zone >= 0 && hint_zone < (int)n->num_zones) {
            cands[nc++] = hint_zone;
        }
        if (LZ.primary[g] >= 0 && LZ.primary[g] != hint_zone) {
            cands[nc++] = LZ.primary[g];
        }
        for (i = 0; i < LZ.nlin[g] && nc < LZ_LINEAGE_DEPTH + 6; i++) {
            if (LZ.lineage[g][i] != hint_zone) {
                cands[nc++] = LZ.lineage[g][i];
            }
        }
        for (i = 0; i < 5 && nc < LZ_LINEAGE_DEPTH + 6; i++) {
            int g2 = g ^ (1 << i);
            if (LZ.primary[g2] >= 0 && LZ.primary[g2] != hint_zone) {
                cands[nc++] = LZ.primary[g2];
            }
        }

        for (i = 0; i < nc && probed < LZ.r; i++) {
            int zid = cands[i];
            LZZone *z;
            lz_ent *e;
            if (zid < 0) {
                continue;
            }
            lz_ensure_zone(n, zid);
            z = &LZ.zones[zid];
            if (z->state == 2 || z->state == 3) {
                continue;
            }
            probed++;
            comp_ns += LZ_T_FILTER_NS;

            if (LZ.mode == LZ_MODE_DATAPATH) {
                LZ.md_read_pages += 1;
                e = lz_tab_lookup(z, fp);
                if (e) {
                    src_zone = zid;
                    src_lpn = e->lba;
                    break;
                }
                continue;
            }
            if (z->state == 0 && LZ.mode == LZ_MODE_FULL) {
                e = lz_tab_lookup(z, fp);
                if (e && z->used - e->tag < LZ_WI_ENTRIES) {
                    LZ.wi_hits++;
                    src_zone = zid;
                    src_lpn = e->lba;
                    break;
                }
            }
            if (!lz_bloom_may(z, fp)) {
                continue;
            }
            if (z->state != 0) {
                uint32_t flt_pages = (uint32_t)(z->used * 9.84 / 8 / 4096) + 1;
                if (LZ.mode == LZ_MODE_FULL) {
                    if (LZ.filter_cache_init &&
                        LZ.filter_cache[zid % LZ_FILTER_CACHE_ZONES]) {
                        LZ.filter_hits++;
                    } else {
                        LZ.filter_misses++;
                        LZ.md_read_pages += flt_pages;
                        LZ.filter_cache[zid % LZ_FILTER_CACHE_ZONES] = 1;
                        LZ.filter_cache_init = 1;
                    }
                } else {
                    LZ.md_read_pages += flt_pages;
                }
            }
            e = lz_tab_lookup(z, fp);
            if (LZ.mode == LZ_MODE_FULL) {
                uint64_t key = ((uint64_t)zid << 32) | (fp & 0x7F);
                if (LZ.shard_cache_init &&
                    LZ.shard_cache[key % LZ_SHARD_CACHE_PAGES] == key) {
                    LZ.shard_hits++;
                } else {
                    LZ.shard_misses++;
                    LZ.md_read_pages += 1;
                    LZ.shard_cache[key % LZ_SHARD_CACHE_PAGES] = key;
                    LZ.shard_cache_init = 1;
                }
            } else {
                LZ.md_read_pages += 1;
            }
            if (e) {
                src_zone = zid;
                src_lpn = e->lba;
                break;
            }
        }

        if (src_zone >= 0) {
            req->lz_hit = 1;
            req->lz_src_lpn = src_lpn;
            LZ.n_hits++;
            if (LZ.zones[src_zone].state != 0) {
                LZ.hit_src_sealed++;
            }
            if ((uint32_t)src_zone != zone_idx) {
                LZ.cross_zone_descs++;
                LZ.zones[src_zone].incoming_refs++;
                if (LZ.zones[src_zone].incoming_refs > LZ.incoming_peak) {
                    LZ.incoming_peak = LZ.zones[src_zone].incoming_refs;
                }
            }
            LZ.compute_ns += comp_ns - (LZ_T_SHA_NS + LZ_T_SIMHASH_NS +
                                       LZ_T_ROUTE_NS);
            qemu_mutex_unlock(&lz_lock);
            return comp_ns;
        }

        if (lzmap_has(&LZ_seen, fp)) {
            LZ.routing_fn++;
        } else {
            lzmap_put(&LZ_seen, fp, 1);
        }

        {
            LZZone *z;
            lz_ensure_zone(n, zone_idx);
            z = &LZ.zones[zone_idx];
            /* spdkhost: a fallback append may target a zone whose LZ
             * structures were dead (host recycled it); revive. */
            if (z->state == 2) {
                z->state = 0;
                z->used = 0;
                if (z->bloom) {
                    memset(z->bloom, 0, z->bloom_bits / 8);
                    z->bloom_keys = 0;
                }
            }
            if (!z->tab) {
                z->cap = 1024;
                z->tab = g_malloc0(sizeof(lz_ent) * 1024);
                LZ.dram_bytes += sizeof(lz_ent) * 1024;
                lz_bloom_init(z, 8192);
            }
            if (z->state == 0) {
                lz_tab_insert(z, fp, lz_lba2lpn(n, req->slba));
                lz_bloom_add(z, fp);
            }
            if (LZ.mode == LZ_MODE_DATAPATH) {
                LZ.md_read_pages += 1;
                LZ.md_rmw_pages += 1;
                LZ.md_write_bytes += 4096;
            } else {
                LZ.md_write_bytes += LZ_INS_BYTES;
            }
            /* spdkhost: fallback route learning -- the host's zone choice
             * is the routing hint; adopt it into the device route table. */
            if (hint_zone >= 0 && hint_zone != LZ.primary[g] &&
                z->state == 0) {
                if (LZ.primary[g] >= 0) {
                    int old = LZ.primary[g];
                    lz_ensure_zone(n, old);
                    lz_seal_zone(n, old);
                    memmove(&LZ.lineage[g][1], &LZ.lineage[g][0],
                            sizeof(int) * (LZ_LINEAGE_DEPTH - 1));
                    LZ.lineage[g][0] = old;
                    if (LZ.nlin[g] < LZ_LINEAGE_DEPTH) {
                        LZ.nlin[g]++;
                    }
                }
                LZ.zones[hint_zone].written_at = LZ.n_writes;
                LZ.primary[g] = hint_zone;
            }
        }
        LZ.compute_ns += comp_ns - (LZ_T_SHA_NS + LZ_T_SIMHASH_NS +
                                   LZ_T_ROUTE_NS);
        qemu_mutex_unlock(&lz_lock);
        return comp_ns;
    }
}

/* ------------------------------------------------------------------ */
/* FTL write path                                                      */

uint64_t lz_write_ftl(struct zns_ssd *zns, NvmeRequest *req)
{
    uint64_t lat = 0;
    if (!lz_inited || !LZ.enabled) {
        return 0;
    }
    qemu_mutex_lock(&lz_lock);
    if (req->lz_hit) {
        LZ.desc_total++;
        lzmap_put(&LZ_desc, lz_lba2lpn(LZ.n, req->slba), req->lz_src_lpn);
        lat = lz_md_append(zns, LZ_DREF_BYTES, req->stime);
        LZ.compute_ns += LZ_T_DREF_NS;
        if (LZ.mode == LZ_MODE_SHARD) {
            LZ.md_read_pages += 1;
            LZ.md_rmw_pages += 1;
            LZ.md_write_bytes += 4096;
        }
    } else {
        if (LZ.mode >= LZ_MODE_DATAPATH && LZ.mode != LZ_MODE_GLOBALINDEX) {
            lat = lz_md_append(zns, LZ_INS_BYTES, req->stime);
        }
        if (LZ.mode == LZ_MODE_BALLOON || LZ.mode == LZ_MODE_COMBO) {
            LZ.data_bytes_programmed += (uint64_t)(4096.0f * req->lz_cr);
            LZ.cr_pending += req->lz_cr;
        } else {
            LZ.data_bytes_programmed += 4096;
        }
        LZ.data_pages_programmed++;
    }
    qemu_mutex_unlock(&lz_lock);
    return lat;
}

/* ------------------------------------------------------------------ */
/* read path                                                           */

int lz_read_resolve(struct zns_ssd *zns, uint64_t lpn, uint64_t *lat_ns)
{
    lz_map_ent *e;
    *lat_ns = 0;
    if (!lz_inited || !LZ.enabled) {
        return -1;
    }
    qemu_mutex_lock(&lz_lock);
    e = lzmap_find(&LZ_desc, lpn);
    if (!e) {
        qemu_mutex_unlock(&lz_lock);
        return -1;
    }
    LZ.desc_read_extra++;
    if (LZ.desc_total - e->seq < 512) {
        LZ.desc_read_cached++;
    } else {
        LZ.md_read_pages += 1;
        *lat_ns += lz_md_pages_io(zns, 1, 1, 0);
    }
    LZ.compute_ns += LZ_T_STATUS_NS + LZ_T_DPARSE_NS;
    qemu_mutex_unlock(&lz_lock);
    return (int)e->val;
}

/* ------------------------------------------------------------------ */
/* lifecycle                                                           */

/* spdkhost: release all outgoing descriptors owned by zone_idx (the
 * UREF half of the reset protocol); also used by lz_on_discard. */
static void lz_release_outgoing(FemuCtrl *n, uint32_t zone_idx)
{
    uint32_t i;
    for (i = 0; i < LZ_desc.cap; i++) {
        lz_map_ent *e = &LZ_desc.e[i];
        if (e->val && lz_zone_of_lba(LZ.n, e->key * (4096 / LZ.n->zns->lbasz ?
                4096 / LZ.n->zns->lbasz : 1)) == zone_idx) {
            uint32_t src_zone = lz_zone_of_lba(LZ.n,
                e->val * (4096 / LZ.n->zns->lbasz ? 4096 /
                          LZ.n->zns->lbasz : 1));
            lz_ensure_zone(n, src_zone);
            if (LZ.zones[src_zone].incoming_refs > 0) {
                LZ.zones[src_zone].incoming_refs--;
            }
            lzmap_del(&LZ_desc, e->key);
            LZ.n_uref++;
        }
    }
}

int lz_reset_zone_begin(FemuCtrl *n, uint32_t zone_idx)
{
    int deferred = 0;
    if (!lz_inited || !LZ.enabled) {
        return 0;
    }
    qemu_mutex_lock(&lz_lock);
    lz_ensure_zone(n, zone_idx);
    if (LZ.zones[zone_idx].incoming_refs > 0) {
        /* step 2: block new descriptors; step 4: release our outgoing
         * references now; step 5 (wait for incoming) is host-visible. */
        deferred = 1;
        LZ.n_pending_resets++;
        LZ.n_pending_now++;
        LZ.zones[zone_idx].state = LZ_ZSTATE_PENDING;
        lz_release_outgoing(n, zone_idx);
    }
    qemu_mutex_unlock(&lz_lock);
    return deferred;
}

int lz_zone_is_pending(FemuCtrl *n, uint32_t zone_idx)
{
    int r = 0;
    if (!lz_inited || !LZ.enabled || zone_idx >= LZ.n_zones) {
        return 0;
    }
    qemu_mutex_lock(&lz_lock);
    r = (LZ.zones[zone_idx].state == LZ_ZSTATE_PENDING);
    qemu_mutex_unlock(&lz_lock);
    return r;
}

void lz_on_discard(FemuCtrl *n, uint64_t slba, uint32_t nlb)
{
    uint64_t lba;
    if (!lz_inited || !LZ.enabled) {
        return;
    }
    qemu_mutex_lock(&lz_lock);
    for (lba = slba; lba < slba + nlb; lba++) {
        uint64_t lpn = lz_lba2lpn(n, lba);
        lz_map_ent *e = lzmap_find(&LZ_desc, lpn);
        LZ.n_discard_lbas++;
        if (e) {
            uint32_t src_zone = lz_zone_of_lba(LZ.n,
                e->val * (4096 / LZ.n->zns->lbasz ? 4096 /
                          LZ.n->zns->lbasz : 1));
            lz_ensure_zone(n, src_zone);
            if (LZ.zones[src_zone].incoming_refs > 0) {
                LZ.zones[src_zone].incoming_refs--;
            }
            lzmap_del(&LZ_desc, lpn);
            LZ.n_uref++;
            LZ.n_discard_descs++;
            LZ.md_write_bytes += LZ_DREF_BYTES;  /* UREF record */
        }
    }
    qemu_mutex_unlock(&lz_lock);
}

int lz_pop_finalize_zone(FemuCtrl *n)
{
    int i;
    if (!lz_inited || !LZ.enabled) {
        return -1;
    }
    qemu_mutex_lock(&lz_lock);
    for (i = 0; i < LZ.n_zones; i++) {
        if (LZ.zones[i].state == LZ_ZSTATE_PENDING &&
            LZ.zones[i].incoming_refs == 0) {
            qemu_mutex_unlock(&lz_lock);
            return i;
        }
    }
    qemu_mutex_unlock(&lz_lock);
    return -1;
}

void lz_on_zone_reset(FemuCtrl *n, uint32_t zone_idx)
{
    if (!lz_inited || !LZ.enabled) {
        return;
    }
    qemu_mutex_lock(&lz_lock);
    LZ.n_resets++;
    lz_ensure_zone(n, zone_idx);
    if (LZ.zones[zone_idx].state == LZ_ZSTATE_PENDING) {
        LZ.n_pending_now--;
        LZ.n_pending_finalized++;
    }
    lz_release_outgoing(n, zone_idx);   /* idempotent */
    LZ.zones[zone_idx].incoming_refs = 0;
    LZ.zones[zone_idx].state = 2;
    if (LZ.zones[zone_idx].bloom) {
        memset(LZ.zones[zone_idx].bloom, 0,
               LZ.zones[zone_idx].bloom_bits / 8);
        LZ.zones[zone_idx].bloom_keys = 0;
    }
    LZ.zones[zone_idx].used = 0;
    qemu_mutex_unlock(&lz_lock);
}

void lz_on_zone_seal(FemuCtrl *n, uint32_t zone_idx)
{
    qemu_mutex_lock(&lz_lock);
    lz_ensure_zone(n, zone_idx);
    if (zone_idx < LZ.n_zones && LZ.zones[zone_idx].state == 0) {
        lz_seal_zone(n, zone_idx);
    }
    qemu_mutex_unlock(&lz_lock);
}


/* routing digest only (no lock, no identity state) */
uint32_t lz_route_digest(FemuCtrl *n, NvmeRequest *req)
{
    if (!lz_inited) {
        return 0;
    }
    if (lz_req_page(n, req, 0, pg_buf) != 0) {
        return 0;
    }
    return lz_simhash(pg_buf);
}

/* spdkhost: small note-taking helpers used by zns.c */
void lz_note_dup_append(FemuCtrl *n)
{
    if (!lz_inited) {
        return;
    }
    qemu_mutex_lock(&lz_lock);
    LZ.n_dup_append++;
    qemu_mutex_unlock(&lz_lock);
}

void lz_note_append_rejected(FemuCtrl *n)
{
    if (!lz_inited) {
        return;
    }
    qemu_mutex_lock(&lz_lock);
    LZ.n_appends_rejected_pending++;
    qemu_mutex_unlock(&lz_lock);
}

int lz_fallback_active(FemuCtrl *n)
{
    return lz_inited && LZ.enabled && LZ.fallback;
}

/* ------------------------------------------------------------------ */
/* spdkhost: introspection                                             */

void lz_stats_blob(FemuCtrl *n, LZStatsBlob *blob)
{
    memset(blob, 0, sizeof(*blob));
    if (!lz_inited) {
        return;
    }
    qemu_mutex_lock(&lz_lock);
    blob->magic = LZ_STATS_MAGIC;
    blob->version = LZ_STATS_VERSION;
    blob->mode = LZ.mode;
    blob->r = LZ.r;
    blob->fallback = LZ.fallback;
    blob->n_writes = LZ.n_writes;
    blob->n_hits = LZ.n_hits;
    blob->n_dup_append = LZ.n_dup_append;
    blob->data_pages_programmed = LZ.data_pages_programmed;
    blob->data_bytes_programmed = LZ.data_bytes_programmed;
    blob->desc_total = LZ.desc_total;
    blob->cross_zone_descs = LZ.cross_zone_descs;
    blob->hit_src_sealed = LZ.hit_src_sealed;
    blob->routing_fn = LZ.routing_fn;
    blob->md_write_bytes = LZ.md_write_bytes;
    blob->md_read_pages = LZ.md_read_pages;
    blob->md_rmw_pages = LZ.md_rmw_pages;
    blob->n_resets = LZ.n_resets;
    blob->n_pending_resets = LZ.n_pending_resets;
    blob->n_pending_now = LZ.n_pending_now;
    blob->n_pending_finalized = LZ.n_pending_finalized;
    blob->n_uref = LZ.n_uref;
    blob->n_discard_lbas = LZ.n_discard_lbas;
    blob->n_discard_descs = LZ.n_discard_descs;
    blob->n_appends_rejected_pending = LZ.n_appends_rejected_pending;
    blob->n_seals = LZ.n_seals;
    blob->incoming_peak = LZ.incoming_peak;
    qemu_mutex_unlock(&lz_lock);
}

int lz_zone_query(FemuCtrl *n, uint32_t zid, LZZoneInfo *info)
{
    memset(info, 0, sizeof(*info));
    if (!lz_inited || zid >= n->num_zones) {
        return -1;
    }
    qemu_mutex_lock(&lz_lock);
    lz_ensure_zone(n, zid);
    info->magic = LZ_ZINFO_MAGIC;
    info->zid = zid;
    info->lz_state = LZ.zones[zid].state;
    info->incoming_refs = LZ.zones[zid].incoming_refs;
    info->written_at = LZ.zones[zid].written_at;
    info->sealed_at = LZ.zones[zid].sealed_at;
    qemu_mutex_unlock(&lz_lock);
    info->zns_state = zns_get_zone_state(&n->zone_array[zid]);
    info->wp_lba = n->zone_array[zid].d.wp;
    info->wr_boundary = zns_zone_wr_boundary(&n->zone_array[zid]);
    return 0;
}


/* ------------------------------------------------------------------ */
/* spdkhost/E3: route-table checkpoint save/restore (LZ_CMD_CHECKPOINT).
 * The checkpoint covers the C1 route table (32 groups x primary +
 * lineage) and the per-zone reference-plane aggregates (state,
 * incoming) -- everything the paper's recovery path loads before
 * replaying the metadata delta log.  The snapshot lives in controller
 * memory (survives guest-process crashes, i.e., the T6 regime); the
 * timing-model cost of the NAND write/read is reported separately in
 * model_ns so emulator wall-clock distortion does not pollute it.     */

typedef struct LZCkpt {
    uint32_t magic;              /* LZ_CKPT_MAGIC                        */
    uint32_t version;
    uint64_t n_writes;
    uint64_t md_write_bytes_at_save;
    uint64_t md_read_pages_at_save;
    int32_t  primary[LZ_MAX_GROUPS];
    int32_t  lineage[LZ_MAX_GROUPS][LZ_LINEAGE_DEPTH];
    int32_t  nlin[LZ_MAX_GROUPS];
    uint32_t n_zones;
    /* n_zones x {zid, state, incoming_refs} follow */
} LZCkpt;

static uint8_t *lz_ckpt_buf;
static size_t   lz_ckpt_len;

int lz_checkpoint(FemuCtrl *n, int op, LZCkptInfo *info)
{
    memset(info, 0, sizeof(*info));
    info->magic = LZ_CKPT_MAGIC;
    info->op = (uint32_t)op;
    if (!lz_inited || !LZ.enabled) {
        return -1;
    }
    int64_t t0 = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    if (op == 0) {
        qemu_mutex_lock(&lz_lock);
        size_t len = sizeof(LZCkpt) + (size_t)LZ.n_zones * 12;
        lz_ckpt_buf = g_realloc(lz_ckpt_buf, len);
        LZCkpt *ck = (LZCkpt *)lz_ckpt_buf;
        memset(ck, 0, sizeof(*ck));
        ck->magic = LZ_CKPT_MAGIC;
        ck->version = 1;
        ck->n_writes = LZ.n_writes;
        ck->md_write_bytes_at_save = LZ.md_write_bytes;
        ck->md_read_pages_at_save = LZ.md_read_pages;
        memcpy(ck->primary, LZ.primary, sizeof(ck->primary));
        memcpy(ck->lineage, LZ.lineage, sizeof(ck->lineage));
        memcpy(ck->nlin, LZ.nlin, sizeof(ck->nlin));
        ck->n_zones = (uint32_t)LZ.n_zones;
        uint8_t *tail = lz_ckpt_buf + sizeof(LZCkpt);
        int i;
        for (i = 0; i < LZ.n_zones; i++) {
            uint32_t ent[3] = { (uint32_t)LZ.zones[i].zid,
                                (uint32_t)LZ.zones[i].state,
                                LZ.zones[i].incoming_refs };
            memcpy(tail + (size_t)i * 12, ent, 12);
        }
        qemu_mutex_unlock(&lz_lock);
        lz_ckpt_len = len;
        info->ckpt_bytes = (uint32_t)len;
    } else {
        if (!lz_ckpt_buf || lz_ckpt_len < sizeof(LZCkpt)) {
            return -1;
        }
        LZCkpt *ck = (LZCkpt *)lz_ckpt_buf;
        if (ck->magic != LZ_CKPT_MAGIC) {
            return -1;
        }
        qemu_mutex_lock(&lz_lock);
        memcpy(LZ.primary, ck->primary, sizeof(LZ.primary));
        memcpy(LZ.lineage, ck->lineage, sizeof(LZ.lineage));
        memcpy(LZ.nlin, ck->nlin, sizeof(LZ.nlin));
        uint8_t *tail = lz_ckpt_buf + sizeof(LZCkpt);
        uint32_t i;
        for (i = 0; i < ck->n_zones && (int)i < LZ.n_zones; i++) {
            uint32_t ent[3];
            memcpy(ent, tail + (size_t)i * 12, 12);
            if ((int)ent[0] < LZ.n_zones) {
                LZ.zones[ent[0]].incoming_refs = ent[2];
            }
        }
        qemu_mutex_unlock(&lz_lock);
        info->ckpt_bytes = (uint32_t)lz_ckpt_len;
        info->n_writes = ck->n_writes;
        info->md_delta_bytes = LZ.md_write_bytes - ck->md_write_bytes_at_save;
    }
    int64_t t1 = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    info->wall_ns = (uint64_t)(t1 - t0);
    info->ckpt_pages = (info->ckpt_bytes + 4095) / 4096;
    uint64_t lat = (op == 0) ? n->zns->timing.pg_wr_lat[n->zns->flash_type]
                             : n->zns->timing.pg_rd_lat[n->zns->flash_type];
    info->model_ns = (uint64_t)info->ckpt_pages * lat;
    info->replay_pages = info->md_delta_bytes / 4096;
    info->ok = 1;
    return 0;
}

/* ------------------------------------------------------------------ */
/* stats                                                               */

void lz_dump_stats(FemuCtrl *n)
{
    uint64_t md_pages = LZ.md_write_bytes / 4096 + LZ.md_rmw_pages;
}
