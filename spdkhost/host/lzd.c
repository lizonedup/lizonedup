/*
 * lzd.c -- LiZoneDedup host-side library over SPDK (see lzd.h).
 */
#include "spdk/stdinc.h"
#include "spdk/env.h"
#include "spdk/log.h"
#include "spdk/nvme.h"

#include "lzd.h"

#include <pthread.h>
#include <string.h>
#include <errno.h>

#define NVME_OPC_READ           0x02
#define NVME_OPC_DSM            0x09
#define NVME_OPC_ZONE_APPEND    0x7D
#define NVME_OPC_ZONE_MGMT_SEND 0x79
#define NVME_OPC_ZONE_MGMT_RECV 0x7A

#define ZM_RESET 0x04
#define ZM_CLOSE 0x01

/* NVMe zone states (>> 4 of the zs byte) */
#define ZST_EMPTY     0x01
#define ZST_IMP_OPEN  0x02
#define ZST_EXP_OPEN  0x03
#define ZST_CLOSED    0x04
#define ZST_READONLY  0x0D
#define ZST_FULL      0x0E
#define ZST_OFFLINE   0x0F

/* error encoding: -(0x10000 | sc<<1 | sct), zone statuses are sct=1 */
#define LZD_E(sc, sct) (-(0x10000 | ((sc) << 1) | (sct)))
#define LZD_E_ZONE_FULL        LZD_E(0xb9, 1)
#define LZD_E_ZONE_READ_ONLY   LZD_E(0xba, 1)
#define LZD_E_TOO_MANY_ACTIVE  LZD_E(0xbd, 1)
#define LZD_E_TOO_MANY_OPEN    LZD_E(0xbe, 1)

#define LZD_MAX_ZONES 4096
#define LZD_RETRIES   64

struct lzd_ctx {
    struct spdk_nvme_transport_id trid;
    struct spdk_nvme_ctrlr *ctrlr;
    struct spdk_nvme_ns *ns;
    struct spdk_nvme_qpair *qpair;

    enum lzd_mode mode;

    /* geometry (from identify) */
    uint64_t nsze;              /* LBAs                                  */
    uint64_t zone_size;         /* LBAs per zone                         */
    uint32_t num_zones;
    uint32_t lba_shift;

    /* host route table (fallback mirror of device C1) */
    int primary[LZD_MAX_GROUPS];
    int lineage[LZD_MAX_GROUPS][LZD_LINEAGE_DEPTH];
    int nlin[LZD_MAX_GROUPS];

    /* zone state cache (from report-zones) */
    lzd_zone_desc zdesc[LZD_MAX_ZONES];
    uint32_t nzdesc;

    /* stats */
    uint64_t write_ns;
    uint64_t read_ns;
    uint64_t n_route_repick;
};

/* ------------------------------------------------------------------ */
/* sync command plumbing                                              */

struct lzdcmd {
    pthread_mutex_t mu;
    pthread_cond_t cv;
    int done;
    struct spdk_nvme_cpl cpl;
};

static void lzdcmd_cb(void *arg, const struct spdk_nvme_cpl *cpl)
{
    struct lzdcmd *k = arg;
    pthread_mutex_lock(&k->mu);
    k->cpl = *cpl;
    k->done = 1;
    pthread_cond_signal(&k->cv);
    pthread_mutex_unlock(&k->mu);
}

static void lzdcmd_init(struct lzdcmd *k)
{
    pthread_mutex_init(&k->mu, NULL);
    pthread_cond_init(&k->cv, NULL);
    k->done = 0;
}

static void lzdcmd_destroy(struct lzdcmd *k)
{
    pthread_mutex_destroy(&k->mu);
    pthread_cond_destroy(&k->cv);
}

/* returns cdw0 (>=0) on success, negative error code otherwise */
static int lzd_io_raw(struct lzd_ctx *c, struct spdk_nvme_cmd *cmd,
                      void *buf, uint32_t len)
{
    struct lzdcmd k;
    int rc;

    lzdcmd_init(&k);
    rc = spdk_nvme_ctrlr_cmd_io_raw(c->ctrlr, c->qpair, cmd, buf, len,
                                    lzdcmd_cb, &k);
    if (rc != 0) {
        lzdcmd_destroy(&k);
        return -EIO;
    }
    while (!k.done) {
        spdk_nvme_qpair_process_completions(c->qpair, 0);
    }
    lzdcmd_destroy(&k);
    if (spdk_nvme_cpl_is_error(&k.cpl)) {
        return -(0x10000 | (k.cpl.status.sc << 1) |
                 k.cpl.status.sct);
    }
    return (int32_t)k.cpl.cdw0;
}

static int lzd_admin_raw(struct lzd_ctx *c, struct spdk_nvme_cmd *cmd,
                         void *buf, uint32_t len)
{
    struct lzdcmd k;
    uint64_t spins = 0;
    int rc;

    lzdcmd_init(&k);
    rc = spdk_nvme_ctrlr_cmd_admin_raw(c->ctrlr, cmd, buf, len,
                                       lzdcmd_cb, &k);
    if (rc != 0) {
        lzdcmd_destroy(&k);
        return -EIO;
    }
    while (!k.done) {
        spdk_nvme_ctrlr_process_admin_completions(c->ctrlr);
        if (++spins > 200000000ULL) {
            break;
        }
    }
    lzdcmd_destroy(&k);
    if (!k.done) {
        return -ETIMEDOUT;
    }
    if (spdk_nvme_cpl_is_error(&k.cpl)) {
        return -(0x10000 | (k.cpl.status.sc << 1) |
                 k.cpl.status.sct);
    }
    return 0;
}

/* ------------------------------------------------------------------ */
/* probe / geometry                                                    */

static bool lzd_probe_cb(void *cb_ctx,
                         const struct spdk_nvme_transport_id *trid,
                         struct spdk_nvme_ctrlr_opts *opts)
{
    return true;
}

static void lzd_attach_cb(void *cb_ctx,
                          const struct spdk_nvme_transport_id *trid,
                          struct spdk_nvme_ctrlr *ctrlr,
                          const struct spdk_nvme_ctrlr_opts *opts)
{
    struct lzd_ctx **out = cb_ctx;
    struct lzd_ctx *c;

    c = calloc(1, sizeof(*c));
    c->trid = *trid;
    c->ctrlr = ctrlr;
    c->ns = spdk_nvme_ctrlr_get_ns(ctrlr, 1);
    *out = c;
}

static int lzd_identify(struct lzd_ctx *c)
{
    struct spdk_nvme_cmd cmd;
    uint8_t *buf = spdk_dma_zmalloc(4096, 4096, NULL);
    uint8_t *zbuf = spdk_dma_zmalloc(4096, 4096, NULL);
    struct {
        uint64_t nsze;
        uint64_t ncap;
        uint64_t nuse;
        uint8_t rsvd[111];
        uint8_t nlbaf;
        uint8_t rsvd1;
        uint8_t flbas;
        uint8_t rsvd2;
    } __attribute__((packed)) *idns;
    struct {
        uint16_t zoc, ozcs;
        uint32_t mar, mor, rrl, frl;
        uint8_t rsvd[2796];
        struct {
            uint64_t zsze;
            uint8_t zdes, rsvd[7];
        } __attribute__((packed)) lbafe[16];
    } __attribute__((packed)) *idz;
    int rc;

    if (!buf || !zbuf) {
        return -ENOMEM;
    }

    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = SPDK_NVME_OPC_IDENTIFY;
    cmd.nsid = 1;
    cmd.cdw10 = 0;               /* CNS = Identify Namespace */
    rc = lzd_admin_raw(c, &cmd, buf, 4096);
    if (rc) {
        fprintf(stderr, "lzd: identify ns failed rc=%d\n", rc);
        goto out;
    }
    idns = (void *)buf;
    c->nsze = idns->nsze;
    c->lba_shift = 9;            /* FEMU ZNS: 512B LBAs (lbaf 0) */

    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = SPDK_NVME_OPC_IDENTIFY;
    cmd.nsid = 1;
    cmd.cdw10 = 0x05;            /* CNS = Identify Namespace Zoned */
    cmd.cdw11 = 0x02u << 24;     /* CSI = Zoned (cdw11 bits 31:24) */
    rc = lzd_admin_raw(c, &cmd, zbuf, 4096);
    if (rc) {
        fprintf(stderr, "lzd: identify ns-zoned failed rc=%d\n", rc);
        goto out;
    }
    idz = (void *)zbuf;
    c->zone_size = idz->lbafe[0].zsze;
    if (!c->zone_size) {
        rc = -EINVAL;
        goto out;
    }
    c->num_zones = (uint32_t)(c->nsze / c->zone_size);
    if (c->num_zones > LZD_MAX_ZONES) {
        c->num_zones = LZD_MAX_ZONES;
    }
    rc = 0;
out:
    spdk_dma_free(buf);
    spdk_dma_free(zbuf);
    return rc;
}

int lzd_open(struct lzd_ctx **out, enum lzd_mode mode)
{
    struct lzd_ctx *c = NULL;
    int rc;

    *out = NULL;
    rc = spdk_nvme_probe(NULL, &c, lzd_probe_cb, lzd_attach_cb, NULL);
    if (rc != 0 || !c) {
        fprintf(stderr, "lzd: spdk_nvme_probe found no device (rc=%d)\n", rc);
        return -ENODEV;
    }
    c->mode = mode;
    memset(c->primary, -1, sizeof(c->primary));

    rc = lzd_identify(c);
    if (rc) {
        return rc;
    }
    {
        struct spdk_nvme_io_qpair_opts qopts;
        spdk_nvme_ctrlr_get_default_io_qpair_opts(c->ctrlr, &qopts,
                                                  sizeof(qopts));
        c->qpair = spdk_nvme_ctrlr_alloc_io_qpair(c->ctrlr, &qopts,
                                                  sizeof(qopts));
    }
    if (!c->qpair) {
        return -ENOMEM;
    }
    *out = c;
    return 0;
}

void lzd_close(struct lzd_ctx *c)
{
    if (!c) {
        return;
    }
    if (c->qpair) {
        spdk_nvme_ctrlr_free_io_qpair(c->qpair);
    }
    spdk_nvme_detach(c->ctrlr);
    free(c);
}

uint64_t lzd_zone_size(struct lzd_ctx *c)  { return c->zone_size; }
uint32_t lzd_num_zones(struct lzd_ctx *c)  { return c->num_zones; }
uint64_t lzd_ns_size(struct lzd_ctx *c)    { return c->nsze; }
int lzd_route_primary(struct lzd_ctx *c, uint32_t g) { return c->primary[g]; }

uint64_t lzd_stat_write_ns(struct lzd_ctx *c) { return c->write_ns; }
uint64_t lzd_stat_read_ns(struct lzd_ctx *c)  { return c->read_ns; }

struct spdk_nvme_ctrlr *lzd_ctrlr(struct lzd_ctx *c) { return c->ctrlr; }
struct spdk_nvme_qpair *lzd_qpair(struct lzd_ctx *c) { return c->qpair; }

/* ------------------------------------------------------------------ */
/* content hashing (bit-exact mirrors of the device's lizone.c)        */

static uint64_t lzd_mix64(uint64_t x)
{
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

uint64_t lzd_fingerprint(const void *page)
{
    const uint8_t *pg = page;
    uint64_t h = 0x243F6A8885A308D3ULL;
    int i;
    for (i = 0; i < 4096 / 64; i++) {
        uint64_t w;
        memcpy(&w, pg + i * 64, 8);
        h = lzd_mix64(h ^ w);
        memcpy(&w, pg + i * 64 + 56, 8);
        h = lzd_mix64(h ^ w);
    }
    return lzd_mix64(lzd_mix64(h));
}

uint32_t lzd_simhash(const void *page)
{
    const uint8_t *pg = page;
    int votes[LZD_LSH_BITS];
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
        for (j = 0; j < LZD_LSH_BITS; j++) {
            votes[j] += (h >> j) & 1 ? 1 : -1;
        }
    }
    for (j = 0; j < LZD_LSH_BITS; j++) {
        if (votes[j] > 0) {
            digest |= 1u << j;
        }
    }
    return digest;
}

uint32_t lzd_group_of(uint32_t lsh)
{
    uint32_t bucket = lsh >> (LZD_LSH_BITS - LZD_ROUTE_BITS);
    return bucket >> (LZD_ROUTE_BITS - 5);
}

/* ------------------------------------------------------------------ */
/* zone state cache                                                    */

static uint32_t zst_of(const lzd_zone_desc *d)
{
    return d->zs >> 4;
}

int lzd_zone_report(struct lzd_ctx *c, lzd_zone_desc *zones, uint32_t *nr)
{
    struct spdk_nvme_cmd cmd;
    uint32_t max = *nr;
    uint32_t bytes = 8 + 64 * max;
    uint8_t *buf = spdk_dma_zmalloc(bytes, 4096, NULL);
    uint32_t i, reported;
    int rc;

    if (!buf) {
        return -ENOMEM;
    }
    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = NVME_OPC_ZONE_MGMT_RECV;
    cmd.nsid = 1;
    cmd.cdw10 = 0;               /* slba = 0 */
    cmd.cdw11 = 0;
    cmd.cdw12 = (bytes / 4) - 1; /* dwords-1 */
    cmd.cdw13 = 0;               /* ZRA=report, ZRASF=all, partial=0 */
    rc = lzd_io_raw(c, &cmd, buf, bytes);
    if (rc < 0) {
        spdk_dma_free(buf);
        return rc;
    }
    memcpy(&reported, buf, 4);
    if (reported > max) {
        reported = max;
    }
    for (i = 0; i < reported; i++) {
        memcpy(&zones[i], buf + 64 + 64 * i, 64);   /* 64 B report header */
    }
    *nr = reported;
    spdk_dma_free(buf);
    return 0;
}

static int lzd_refresh_zones(struct lzd_ctx *c)
{
    uint32_t n = LZD_MAX_ZONES;
    int rc = lzd_zone_report(c, c->zdesc, &n);
    if (rc == 0) {
        c->nzdesc = n;
    }
    return rc;
}

static int lzd_zone_appendable(struct lzd_ctx *c, int zid)
{
    lzd_zone_desc *d;
    if (zid < 0 || (uint32_t)zid >= c->nzdesc) {
        return 0;
    }
    d = &c->zdesc[zid];
    if (d->wp >= d->zslba + d->zcap) {
        return 0;
    }
    switch (zst_of(d)) {
    case ZST_EMPTY:
    case ZST_IMP_OPEN:
    case ZST_EXP_OPEN:
    case ZST_CLOSED:
        return 1;
    default:
        return 0;
    }
}

/* host-side mirror of lz_pick_zone (same policy, same scan order) */
static int lzd_pick_zone(struct lzd_ctx *c, uint32_t lsh)
{
    uint32_t g = lzd_group_of(lsh);
    int zid = c->primary[g];
    int nz = -1, i;

    if (zid >= 0 && lzd_zone_appendable(c, zid)) {
        return zid;
    }
    if (zid >= 0) {
        /* retire the old primary into the lineage, exactly like the
         * device's lz_pick_zone does for a non-appendable primary */
        memmove(&c->lineage[g][1], &c->lineage[g][0],
                sizeof(int) * (LZD_LINEAGE_DEPTH - 1));
        c->lineage[g][0] = zid;
        if (c->nlin[g] < LZD_LINEAGE_DEPTH) {
            c->nlin[g]++;
        }
    }
    for (i = 0; (uint32_t)i < c->num_zones; i++) {
        if ((uint32_t)i < c->nzdesc && zst_of(&c->zdesc[i]) == ZST_EMPTY) {
            nz = i;
            break;
        }
    }
    if (nz < 0) {
        for (i = 0; (uint32_t)i < c->num_zones; i++) {
            if (lzd_zone_appendable(c, i)) {
                nz = i;
                break;
            }
        }
    }
    if (nz < 0) {
        return zid;
    }
    c->primary[g] = nz;
    c->n_route_repick++;
    return nz;
}

/* exported routing decision (no I/O): pick the zone for a route digest */
int lzd_route_pick(struct lzd_ctx *c, uint32_t lsh)
{
    int zid;

    if (c->nzdesc == 0 && lzd_refresh_zones(c) != 0) {
        return -1;
    }
    zid = lzd_pick_zone(c, lsh);
    if (zid < 0 || (uint32_t)zid >= c->num_zones) {
        return -1;
    }
    return zid;
}

/* ------------------------------------------------------------------ */
/* write paths                                                         */

int lzd_dup_append(struct lzd_ctx *c, void *page, uint64_t *lba)
{
    struct spdk_nvme_cmd cmd;
    uint64_t t0, t1;
    int rc;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = LZD_CMD_DUP_APPEND;
    cmd.nsid = 1;
    cmd.cdw10 = 0;               /* nlb-1 = 0: one page */

    t0 = spdk_get_ticks();
    rc = lzd_io_raw(c, &cmd, page, LZD_PAGE);
    t1 = spdk_get_ticks();
    c->write_ns += t1 - t0;
    if (rc < 0) {
        return rc;
    }
    *lba = (uint64_t)(uint32_t)rc;
    return 0;
}

int lzd_zone_append_to(struct lzd_ctx *c, uint32_t zid, void *page,
                       uint64_t *lba)
{
    struct spdk_nvme_cmd cmd;
    int rc;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = NVME_OPC_ZONE_APPEND;
    cmd.nsid = 1;
    cmd.cdw10 = (uint32_t)((uint64_t)zid * c->zone_size);
    cmd.cdw11 = (uint32_t)(((uint64_t)zid * c->zone_size) >> 32);
    cmd.cdw12 = (LZD_PAGE / 512) - 1;   /* one 4 KiB page */
    rc = lzd_io_raw(c, &cmd, page, LZD_PAGE);
    if (rc < 0) {
        return rc;
    }
    *lba = (uint64_t)(uint32_t)rc;
    return 0;
}

int lzd_zone_append_routed(struct lzd_ctx *c, void *page, uint64_t *lba)
{
    struct spdk_nvme_cmd cmd;
    uint32_t lsh;
    int zid, rc, try;
    uint64_t t0, t1;

    if (c->nzdesc == 0) {
        rc = lzd_refresh_zones(c);
        if (rc) {
            return rc;
        }
    }

    for (try = 0; try < LZD_RETRIES; try++) {
        lsh = lzd_simhash(page);
        zid = lzd_pick_zone(c, lsh);
        if (zid < 0 || (uint32_t)zid >= c->num_zones) {
            lzd_refresh_zones(c);
            continue;
        }

        memset(&cmd, 0, sizeof(cmd));
        cmd.opc = NVME_OPC_ZONE_APPEND;
        cmd.nsid = 1;
        cmd.cdw10 = (uint32_t)((uint64_t)zid * c->zone_size);
        cmd.cdw11 = (uint32_t)(((uint64_t)zid * c->zone_size) >> 32);
        cmd.cdw12 = (LZD_PAGE / 512) - 1;   /* one 4 KiB page */

        t0 = spdk_get_ticks();
        rc = lzd_io_raw(c, &cmd, page, LZD_PAGE);
        t1 = spdk_get_ticks();
        c->write_ns += t1 - t0;
        if (rc >= 0) {
            *lba = (uint64_t)(uint32_t)rc;
            /* optimistic WP bump so we rarely need a report; the device
             * is authoritative on the next refresh                       */
            if ((uint32_t)zid < c->nzdesc && c->zdesc[zid].wp == *lba) {
                c->zdesc[zid].wp = *lba + 1;
            }
            return 0;
        }
        if (rc == LZD_E_ZONE_FULL || rc == LZD_E_ZONE_READ_ONLY ||
            rc == LZD_E_TOO_MANY_OPEN || rc == LZD_E_TOO_MANY_ACTIVE) {
            lzd_refresh_zones(c);
            /* device logically full: no zone appendable after the
             * refresh -> fail fast instead of burning the retry budget
             * on every subsequent write */
            int any = 0;
            uint32_t zi;
            for (zi = 0; zi < c->nzdesc; zi++) {
                if (lzd_zone_appendable(c, (int)zi)) { any = 1; break; }
            }
            if (!any) {
                return -ENOSPC;
            }
            continue;
        }
        return rc;
    }
    return -EBUSY;
}

/* ------------------------------------------------------------------ */
/* standard paths                                                      */

int lzd_read(struct lzd_ctx *c, uint64_t lba, void *page)
{
    struct spdk_nvme_cmd cmd;
    uint64_t t0, t1;
    int rc;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = NVME_OPC_READ;
    cmd.nsid = 1;
    cmd.cdw10 = (uint32_t)lba;
    cmd.cdw11 = (uint32_t)(lba >> 32);
    cmd.cdw12 = (LZD_PAGE / 512) - 1;  /* one 4 KiB page = 8 x 512 B LBAs */

    t0 = spdk_get_ticks();
    rc = lzd_io_raw(c, &cmd, page, LZD_PAGE);
    t1 = spdk_get_ticks();
    c->read_ns += t1 - t0;
    return rc < 0 ? rc : 0;
}

int lzd_discard(struct lzd_ctx *c, uint64_t lba, uint32_t nlb)
{
    struct spdk_nvme_cmd cmd;
    struct {
        uint32_t cattr;
        uint32_t nlb;
        uint64_t slba;
    } __attribute__((packed)) *range;
    int rc;

    range = spdk_dma_zmalloc(16, 16, NULL);
    if (!range) {
        return -ENOMEM;
    }
    memset(range, 0, 16);
    range->nlb = nlb;
    range->slba = lba;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = NVME_OPC_DSM;
    cmd.nsid = 1;
    cmd.cdw10 = 0;               /* nr-1 = 0 */
    cmd.cdw11 = 0x4;             /* attribute: Deallocate (AD, bit 2) */
    rc = lzd_io_raw(c, &cmd, range, 16);
    spdk_dma_free(range);
    return rc < 0 ? rc : 0;
}

static int lzd_zone_mgmt(struct lzd_ctx *c, uint64_t zslba, uint8_t action)
{
    struct spdk_nvme_cmd cmd;

    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = NVME_OPC_ZONE_MGMT_SEND;
    cmd.nsid = 1;
    cmd.cdw10 = (uint32_t)zslba;
    cmd.cdw11 = (uint32_t)(zslba >> 32);
    cmd.cdw13 = action;
    return lzd_io_raw(c, &cmd, NULL, 0);
}

int lzd_zone_reset(struct lzd_ctx *c, uint64_t zslba, int *deferred)
{
    int rc = lzd_zone_mgmt(c, zslba, ZM_RESET);

    *deferred = 0;
    if (rc == 0) {
        return 0;
    }
    if (rc == (int32_t)LZD_RESET_DEFERRED) {
        *deferred = 1;
        return 0;
    }
    return rc;
}

int lzd_zone_close(struct lzd_ctx *c, uint64_t zslba)
{
    return lzd_zone_mgmt(c, zslba, ZM_CLOSE);
}

/* ------------------------------------------------------------------ */
/* introspection                                                       */

int lzd_get_stats(struct lzd_ctx *c, lzd_stats *st)
{
    struct spdk_nvme_cmd cmd;
    uint8_t *buf = spdk_dma_zmalloc(sizeof(lzd_stats), 4096, NULL);
    int rc;

    if (!buf) {
        return -ENOMEM;
    }
    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = LZD_CMD_STATS;
    cmd.nsid = 1;
    rc = lzd_io_raw(c, &cmd, buf, sizeof(lzd_stats));
    if (rc == 0) {
        memcpy(st, buf, sizeof(*st));
    } else {
        memset(st, 0, sizeof(*st));
    }
    spdk_dma_free(buf);
    return rc < 0 ? rc : 0;
}

int lzd_zone_query(struct lzd_ctx *c, uint32_t zid, lzd_zinfo *info)
{
    struct spdk_nvme_cmd cmd;
    uint8_t *buf = spdk_dma_zmalloc(sizeof(lzd_zinfo), 4096, NULL);
    int rc;

    if (!buf) {
        return -ENOMEM;
    }
    memset(&cmd, 0, sizeof(cmd));
    cmd.opc = LZD_CMD_ZONE_QUERY;
    cmd.nsid = 1;
    cmd.cdw10 = zid;
    rc = lzd_io_raw(c, &cmd, buf, sizeof(lzd_zinfo));
    if (rc == 0) {
        memcpy(info, buf, sizeof(*info));
    } else {
        memset(info, 0, sizeof(*info));
    }
    spdk_dma_free(buf);
    return rc < 0 ? rc : 0;
}
