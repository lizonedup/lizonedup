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
#define LZ_CMD_STATS        0xB1    /* dumps stats to femu log               */

#define LZ_MAX_GROUPS       32
#define LZ_LINEAGE_DEPTH    8
#define LZ_LSH_BITS         24
#define LZ_ROUTE_BITS       16
#define LZ_WI_ENTRIES       8192    /* per-zone working index (full mode)  */
#define LZ_SHARD_CACHE_PAGES 8192   /* 32 MB of 4 KB shard pages           */
#define LZ_FILTER_CACHE_ZONES 680   /* ~16 MB of sealed-zone xor filters   */

typedef struct lz_ent {
    uint64_t fp;            /* truncated SHA-256 (64-bit key)          */
    uint32_t lba;           /* LBA (in 4 KiB pages) of the source copy */
    uint32_t tag;           /* WI recency tag / gen                    */
} lz_ent;

typedef struct LZZone {
    int zid;
    int state;              /* 0 active, 1 sealed, 2 dead              */
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
} LZCtrl;

/* ---- API used by zns.c / zftl.c ---- */
void lz_init(FemuCtrl *n);
uint32_t lz_route_digest(FemuCtrl *n, NvmeRequest *req);
void lz_exit(FemuCtrl *n);
int  lz_pick_zone(FemuCtrl *n, uint32_t lsh);       /* C1 routing        */
uint64_t lz_decide_write(FemuCtrl *n, NvmeRequest *req, uint32_t zone_idx);
    /* hashes req data, probes candidates, fills req->lz_*, returns the
     * controller-compute + metadata-probe latency in ns (DRAM-class).  */
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
