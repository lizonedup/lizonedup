/*
 * lzd.h -- LiZoneDedup host-side library over SPDK.
 *
 * Talks to the FEMU ZNS device (with the lizonedup controller extension)
 * through the SPDK userspace NVMe driver.  Two write paths:
 *
 *  - native:    LZ_CMD_DUP_APPEND (0xB0); the controller routes, looks
 *               up, and returns the assigned LBA.
 *  - fallback:  the host computes the route digest (SimHash) itself,
 *               mirrors the C1 route table, and issues a standard
 *               zone_append to the zone it picked; the controller still
 *               performs the dedup lookup with the host zone as hint.
 *
 * All NVMe traffic goes through spdk_nvme_ctrlr_cmd_io_raw/_admin_raw,
 * so no SPDK-internal ZNS handling is required.
 */
#ifndef LZD_H
#define LZD_H

#include <stdint.h>
#include <stddef.h>

#define LZD_PAGE             4096
#define LZD_MAX_GROUPS       32
#define LZD_LINEAGE_DEPTH    8
#define LZD_LSH_BITS         24
#define LZD_ROUTE_BITS       16

/* vendor opcodes (must match hw/femu/zns/lizone.h) */
#define LZD_CMD_DUP_APPEND   0xB0
#define LZD_CMD_STATS        0xB1
#define LZD_CMD_ZONE_QUERY   0xB2
#define LZD_RESET_DEFERRED   0x5AD5D5D5u   /* cdw0 marker */

/* write path selector */
enum lzd_mode {
    LZD_M_NATIVE = 0,      /* 0xB0 dup_append                         */
    LZD_M_FALLBACK = 1,    /* host-routed zone_append (device hints)   */
};

/* ---- device blobs (must match lizone.h byte-for-byte) ---- */
typedef struct lzd_stats {
    uint32_t magic;          /* 0x4C5A5450 */
    uint32_t version;
    uint32_t mode;
    uint32_t r;
    uint32_t fallback;
    uint32_t pad0;
    uint64_t n_writes;
    uint64_t n_hits;
    uint64_t n_dup_append;
    uint64_t data_pages_programmed;
    uint64_t data_bytes_programmed;
    uint64_t desc_total;
    uint64_t cross_zone_descs;
    uint64_t hit_src_sealed;
    uint64_t routing_fn;
    uint64_t md_write_bytes;
    uint64_t md_read_pages;
    uint64_t md_rmw_pages;
    uint64_t n_resets;
    uint64_t n_pending_resets;
    uint64_t n_pending_now;
    uint64_t n_pending_finalized;
    uint64_t n_uref;
    uint64_t n_discard_lbas;
    uint64_t n_discard_descs;
    uint64_t n_appends_rejected_pending;
    uint64_t n_seals;
    uint64_t incoming_peak;
} lzd_stats;

typedef struct lzd_zinfo {
    uint32_t magic;          /* 0x4C5A5A49 */
    uint32_t zid;
    uint32_t lz_state;       /* 0 active 1 sealed 2 dead 3 pending */
    uint32_t zns_state;      /* NVME_ZONE_STATE_* */
    uint32_t incoming_refs;
    uint32_t pad0;
    uint64_t wp_lba;
    uint64_t wr_boundary;
    uint64_t written_at;
    uint64_t sealed_at;
} lzd_zinfo;

/* zone report descriptor (64B, NVMe ZNS spec layout) */
typedef struct lzd_zone_desc {
    uint8_t  zt;
    uint8_t  zs;             /* state << 4 */
    uint8_t  za;
    uint8_t  rsvd3[5];
    uint64_t zcap;
    uint64_t zslba;
    uint64_t wp;
    uint8_t  rsvd32[32];
} __attribute__((packed)) lzd_zone_desc;

/* ---- context ---- */
struct lzd_ctx;

int  lzd_open(struct lzd_ctx **out, enum lzd_mode mode);
void lzd_close(struct lzd_ctx *c);

/* geometry */
uint64_t lzd_zone_size(struct lzd_ctx *c);     /* LBAs per zone   */
uint32_t lzd_num_zones(struct lzd_ctx *c);
uint64_t lzd_ns_size(struct lzd_ctx *c);       /* LBAs total      */

/* ---- write paths ---- */
/* native: dup_append; returns 0 and the assigned LBA */
int lzd_dup_append(struct lzd_ctx *c, void *page, uint64_t *lba);
/* fallback: host route + zone_append (the mirror of C1); retries on
 * zone-full / pending-reject / too-many-open by re-routing            */
int lzd_zone_append_routed(struct lzd_ctx *c, void *page, uint64_t *lba);
/* plain zone_append to a specific zone (no routing, no retry); used to
 * verify that reset-pending zones reject appends                      */
int lzd_zone_append_to(struct lzd_ctx *c, uint32_t zid, void *page,
                       uint64_t *lba);

/* ---- standard paths ---- */
int lzd_read(struct lzd_ctx *c, uint64_t lba, void *page);
int lzd_discard(struct lzd_ctx *c, uint64_t lba, uint32_t nlb);
/* zone reset; *deferred=1 when the device returned LZ_RESET_DEFERRED */
int lzd_zone_reset(struct lzd_ctx *c, uint64_t zslba, int *deferred);
int lzd_zone_close(struct lzd_ctx *c, uint64_t zslba);

/* ---- introspection ---- */
int lzd_zone_report(struct lzd_ctx *c, lzd_zone_desc *zones, uint32_t *nr);
int lzd_get_stats(struct lzd_ctx *c, lzd_stats *st);
int lzd_zone_query(struct lzd_ctx *c, uint32_t zid, lzd_zinfo *info);

/* ---- content hashing (bit-exact mirrors of the device) ---- */
uint64_t lzd_fingerprint(const void *page);
uint32_t lzd_simhash(const void *page);
uint32_t lzd_group_of(uint32_t lsh);

/* host route-table inspection (fallback) */
int  lzd_route_primary(struct lzd_ctx *c, uint32_t group);

/* latency accumulators for the micro-benchmarks */
uint64_t lzd_stat_write_ns(struct lzd_ctx *c);
uint64_t lzd_stat_read_ns(struct lzd_ctx *c);

/* raw access for the async benchmark loop */
struct spdk_nvme_ctrlr *lzd_ctrlr(struct lzd_ctx *c);
struct spdk_nvme_qpair *lzd_qpair(struct lzd_ctx *c);

/* host routing decision only (no I/O): zone id for a route digest, or
 * -1 when no zone is appendable (caller refreshes + retries)          */
int lzd_route_pick(struct lzd_ctx *c, uint32_t lsh);

#endif /* LZD_H */
