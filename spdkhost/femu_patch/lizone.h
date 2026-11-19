/*
 * lizone.h -- LiZoneDedup in-storage deduplication for FEMU ZNS mode.
 *
 * Implements C1 (content-derived zone routing), C2 (dup_append with
 * descriptor-backed slots) and C3 (per-zone identity index + membership
 * filters + append-only metadata) inside the FEMU ZNS controller, plus the
 * evaluation baselines (oracle / global index / ablation layers /
 * compression).  Physical write suppression is applied at the NAND timing
 * model level (plane clocks); the memory backend still stores every
 * host-visible LBA so zone reads return correct data.
 *
 * spdkhost extensions (validated by the SPDK host-side prototype):
 *  - fallback mode: lz_decide_write_hint() probes the host-chosen zone
 *    first (the "hint"), then the device route table candidates;
 *  - reset-pending protocol: lz_reset_zone_begin() defers a reset while
 *    the zone has outstanding incoming references; lz_on_discard()
 *    releases references (UREF) on host deallocates and finalizes pending
 *    resets whose incoming count reached zero;
 *  - introspection: LZ_CMD_STATS can DMA a stats blob to the host and
 *    LZ_CMD_ZONE_QUERY returns per-zone reference/lifecycle state.
 */
#ifndef FEMU_LIZONE_H
#define FEMU_LIZONE_H

#include "../nvme.h"

struct zns_ssd;    /* forward decl (defined in zns.h) */

/* evaluation modes */
enum {
    LZ_MODE_NONE = 0,       /* plain ZNS                              */
    LZ_MODE_ORACLE = 1,     /* global exact index, no metadata cost   */
    LZ_MODE_GLOBALINDEX = 2,/* device-wide flash index (layer 1)      */
    LZ_MODE_DATAPATH = 3,   /* C1+C2, naive per-zone table (layer 2)  */
    LZ_MODE_SHARD = 4,      /* +identity shards & filters (layer 3)   */
    LZ_MODE_SPLIT = 5,      /* +identity/reference split (layer 4)    */
    LZ_MODE_FULL = 6,       /* + WI/shard/filter caches (layer 5)     */
    LZ_MODE_BALLOON = 7,    /* slot-aligned LZ4 compression baseline  */
    LZ_MODE_COMBO = 8,      /* dedup then compress                    */
};

/* vendor commands */
#define LZ_CMD_DUP_APPEND   0xB0    /* cdw10 = nlb-1, result = assigned LBA */
#define LZ_CMD_STATS        0xB1    /* optional dptr: stats blob DMA'd back  */
#define LZ_CMD_ZONE_QUERY   0xB2    /* cdw10 = zid, dptr: LZZoneInfo         */
#define LZ_CMD_CHECKPOINT   0xB3    /* cdw10: 0=save 1=restore; dptr: blob    */

/* zone-reset result marker returned in cdw0 (NvmeZonedResult.slba):
 * low 32 bits (0x5AD5D5D5) are what the host reads from cdw0 */
#define LZ_RESET_DEFERRED   0x000000005AD5D5D5ULL

/* LZZone.state values */
enum {
    LZ_ZSTATE_ACTIVE  = 0,
    LZ_ZSTATE_SEALED  = 1,
    LZ_ZSTATE_DEAD    = 2,
    LZ_ZSTATE_PENDING = 3,  /* reset deferred: incoming refs outstanding */
};

#define LZ_MAX_GROUPS       32
#define LZ_LINEAGE_DEPTH    8
#define LZ_LSH_BITS         24
#define LZ_ROUTE_BITS       16
#define LZ_WI_ENTRIES       8192    /* per-zone working index (full mode)  */
#define LZ_SHARD_CACHE_PAGES 8192   /* 32 MB of 4 KB shard pages           */
#define LZ_FILTER_CACHE_ZONES 680   /* ~16 MB of sealed-zone xor filters   */

/* stats blob returned by LZ_CMD_STATS (fixed layout, host-visible) */
#define LZ_STATS_MAGIC      0x4C5A5450u  /* "LZTP" */
#define LZ_STATS_VERSION    1
typedef struct LZStatsBlob {
    uint32_t magic;
    uint32_t version;
    uint32_t mode;
    uint32_t r;
    uint32_t fallback;
    uint32_t pad0;
    uint64_t n_writes;              /* pages through the dedup decision    */
    uint64_t n_hits;
    uint64_t n_dup_append;          /* LZ_CMD_DUP_APPEND commands          */
    uint64_t data_pages_programmed;
    uint64_t data_bytes_programmed;
    uint64_t desc_total;            /* descriptors committed (hits)        */
    uint64_t cross_zone_descs;
    uint64_t hit_src_sealed;
    uint64_t routing_fn;
    uint64_t md_write_bytes;
    uint64_t md_read_pages;
    uint64_t md_rmw_pages;
    uint64_t n_resets;
    uint64_t n_pending_resets;      /* resets deferred (incoming>0)        */
    uint64_t n_pending_now;         /* currently in reset-pending state    */
    uint64_t n_pending_finalized;   /* deferred resets completed           */
    uint64_t n_uref;                /* UREF records emitted                */
    uint64_t n_discard_lbas;        /* LBAs covered by host deallocates    */
    uint64_t n_discard_descs;       /* descriptors deleted by discards     */
    uint64_t n_appends_rejected_pending; /* writes to pending zones blocked */
    uint64_t n_seals;
    uint64_t incoming_peak;
} LZStatsBlob;

/* checkpoint result blob (LZ_CMD_CHECKPOINT) */
#define LZ_CKPT_MAGIC       0x4C5A434Bu  /* "LZCK" */
typedef struct LZCkptInfo {
    uint32_t magic;
    uint32_t op;             /* 0 saved, 1 restored */
    uint32_t ok;
    uint32_t ckpt_bytes;     /* serialized checkpoint size            */
    uint32_t ckpt_pages;     /* ceil(bytes/4096) NAND pages           */
    uint64_t wall_ns;        /* measured serialize/restore wall time  */
    uint64_t model_ns;       /* timing-model cost (pages x NAND lat)  */
    uint64_t n_writes;       /* device writes covered by the ckpt     */
    uint64_t md_delta_bytes; /* metadata log bytes since last ckpt    */
    uint64_t replay_pages;   /* md_delta_bytes / 4096 (recovery scan) */
} LZCkptInfo;

int lz_checkpoint(FemuCtrl *n, int op, LZCkptInfo *info);

/* per-zone info returned by LZ_CMD_ZONE_QUERY */
#define LZ_ZINFO_MAGIC     0x4C5A5A49u  /* "LZZI" */
typedef struct LZZoneInfo {
    uint32_t magic;
    uint32_t zid;
    uint32_t lz_state;      /* LZ_ZSTATE_*                            */
    uint32_t zns_state;     /* NVME_ZONE_STATE_*                     */
    uint32_t incoming_refs;
    uint32_t pad0;
    uint64_t wp_lba;        /* host-visible write pointer (LBAs)     */
    uint64_t wr_boundary;
    uint64_t written_at;
    uint64_t sealed_at;
} LZZoneInfo;

typedef struct lz_ent {
    uint64_t fp;            /* truncated SHA-256 (64-bit key)          */
    uint32_t lba;           /* LBA (in 4 KiB pages) of the source copy */
    uint32_t tag;           /* WI recency tag / gen                    */
} lz_ent;

typedef struct LZZone {
    int zid;
    int state;              /* 0 active, 1 sealed, 2 dead, 3 pending   */
    uint64_t written_at;    /* global write counter at creation        */
    uint64_t sealed_at;
    /* identity index (models per-zone DRAM structures; flash residency
     * cost is accounted as metadata page I/O)                          */
    lz_ent *tab;
    uint32_t cap;           /* power of two                            */
    uint32_t used;
    /* insertion-only Bloom filter (active) / Xor filter stand-in (sealed)*/
    uint64_t *bloom;
    uint32_t bloom_bits;    /* m                                       */
    uint32_t bloom_keys;
    /* lifecycle / reference plane */
    uint32_t incoming_refs;
    uint64_t md_wbytes;
    uint64_t md_rpages;
    uint64_t rmw_pages;
} LZZone;

typedef struct LZCtrl {
    int mode;
    int r;
    int enabled;            /* mode != NONE                            */
    int fallback;           /* 1 = host-routed zone_append dedup path  */
    struct zns_ssd *zns;
    FemuCtrl *n;

    /* route table (C1): LZ_MAX_GROUPS route groups over the 16-bit bucket
     * space; each group has a primary zone and a lineage of retired
     * primaries that remain queryable within the r-candidate budget.   */
    int primary[LZ_MAX_GROUPS];
    int lineage[LZ_MAX_GROUPS][LZ_LINEAGE_DEPTH];
    int nlin[LZ_MAX_GROUPS];
    int group_bits;         /* bucket -> group shift                   */

    /* zones (grown on demand) */
    LZZone *zones;
    int n_zones, cap_zones;

    /* global structures for oracle / globalindex modes and FN analysis */
    GHashTable *global;     /* fp -> lba                               */
    GHashTable *seen;       /* fp -> 1 (everything ever written)       */

    /* descriptor table: owner lpn -> source lpn (live descriptors)     */
    GHashTable *desc;
    /* per-owner-zone descriptor lists (for reset/UREF scans)          */
    GHashTable *zone_descs; /* zone_id -> GQueue of owner lpns          */

    /* caches (full mode) */
    uint64_t shard_cache[LZ_SHARD_CACHE_PAGES]; /* (zid<<32)|prefix keys */
    int shard_cache_next;
    uint8_t shard_cache_init;
    uint8_t filter_cache[LZ_FILTER_CACHE_ZONES];
    int filter_cache_next;
    uint8_t filter_cache_init;

    /* metadata log batching (append-only records, packed into pages)  */
    uint64_t md_pending_bytes;
    uint64_t md_pages_written;

    /* balloon compressed-subpage credits */
    float cr_pending;

    /* stats */
    uint64_t n_writes;      /* 4 KiB pages submitted (dup_append etc.) */
    uint64_t n_hits;
    uint64_t n_dup_append;
    uint64_t data_pages_programmed;   /* 4 KiB data pages to NAND       */
    uint64_t data_bytes_programmed;
    uint64_t md_read_pages;
    uint64_t md_write_bytes;
    uint64_t md_rmw_pages;
    uint64_t compute_ns;
    uint64_t dram_bytes;
    uint64_t n_seals;
    uint64_t hit_src_sealed;
    uint64_t cross_zone_descs;
    uint64_t desc_total;
    uint64_t routing_fn;
    uint64_t wi_hits, shard_hits, shard_misses, filter_hits, filter_misses;
    uint64_t desc_read_extra, desc_read_cached;
    /* lifecycle */
    uint64_t n_resets, n_pending_resets;
    uint64_t incoming_peak;
    /* spdkhost extension counters */
    uint64_t n_pending_now;
    uint64_t n_pending_finalized;
    uint64_t n_uref;
    uint64_t n_discard_lbas;
    uint64_t n_discard_descs;
    uint64_t n_appends_rejected_pending;
} LZCtrl;

/* ---- API used by zns.c / zftl.c ---- */
void lz_init(FemuCtrl *n);
uint32_t lz_route_digest(FemuCtrl *n, NvmeRequest *req);
void lz_exit(FemuCtrl *n);
int  lz_is_enabled(FemuCtrl *n);
int  lz_pick_zone(FemuCtrl *n, uint32_t lsh);       /* C1 routing        */
uint64_t lz_decide_write(FemuCtrl *n, NvmeRequest *req, uint32_t zone_idx);
    /* hashes req data, probes candidates, fills req->lz_*, returns the
     * controller-compute + metadata-probe latency in ns (DRAM-class).  */
uint64_t lz_decide_write_hint(FemuCtrl *n, NvmeRequest *req, uint32_t zone_idx,
                              int hint_zone);
    /* fallback variant: hint_zone (the host-chosen zone) is probed before
     * the route-table candidates. hint_zone < 0 = native behaviour.    */
uint64_t lz_write_ftl(struct zns_ssd *zns, NvmeRequest *req);
    /* FTL-side: hit -> descriptor commit (no data program); returns
     * extra NAND-timeline latency ns (metadata page reads/writes).     */
int  lz_read_resolve(struct zns_ssd *zns, uint64_t lpn, uint64_t *lat_ns);
    /* returns source lpn (or -1), sets extra read latency (descriptor
     * page fetch + source read plane time)                            */
float lz_page_cr(FemuCtrl *n, NvmeRequest *req, uint32_t page_in_req);
    /* compression ratio of one 4 KiB page (balloon/combo)             */
void lz_on_zone_reset(FemuCtrl *n, uint32_t zone_idx);
void lz_on_zone_seal(FemuCtrl *n, uint32_t zone_idx);
void lz_dump_stats(FemuCtrl *n);

/* ---- spdkhost: reset-pending protocol ---- */
int  lz_reset_zone_begin(FemuCtrl *n, uint32_t zone_idx);
    /* called before the physical reset. returns 1 if the reset must be
     * deferred (outstanding incoming references): outgoing descriptors
     * are released (UREF) and the zone enters reset-pending; data stays
     * readable, appends are rejected. returns 0 if the reset may run. */
int  lz_zone_is_pending(FemuCtrl *n, uint32_t zone_idx);
void lz_on_discard(FemuCtrl *n, uint64_t slba, uint32_t nlb);
    /* host DSM deallocate: deletes descriptors covering [slba, +nlb),
     * emits UREFs, decrements the source zones' incoming counts.      */
int  lz_pop_finalize_zone(FemuCtrl *n);
    /* returns a zone id whose deferred reset may now complete (incoming
     * reached zero), or -1. zns.c performs the physical transition.   */

/* ---- spdkhost: introspection ---- */
void lz_stats_blob(FemuCtrl *n, LZStatsBlob *blob);
int  lz_zone_query(FemuCtrl *n, uint32_t zid, LZZoneInfo *info);

/* ---- spdkhost: note-taking helpers used by zns.c ---- */
void lz_note_dup_append(FemuCtrl *n);
void lz_note_append_rejected(FemuCtrl *n);
int  lz_fallback_active(FemuCtrl *n);   /* 1 = dedup on zone_append */

/* per-request dedup decision, stashed in NvmeRequest */
static inline void lz_req_init(NvmeRequest *req) {
    req->lz_hit = 0;
    req->lz_fp = 0;
    req->lz_src_lpn = 0;
    req->lz_md_lat_ns = 0;
    req->lz_zone = 0;
    req->lz_cr = 1.0f;
}

#endif /* FEMU_LIZONE_H */
