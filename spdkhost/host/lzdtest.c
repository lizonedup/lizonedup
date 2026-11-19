/*
 * lzdtest.c -- SPDK host-side correctness suite for LiZoneDedup.
 *
 * Tests (run inside the FEMU guest against the lizonedup ZNS device):
 *   info   probe + identify + zone report                      (smoke)
 *   t1     duplicate detection & descriptor reads              (B1)
 *   t2     adversarial content: SimHash-colliding pages must not be
 *          falsely deduplicated; every read-back is byte-exact  (B4)
 *   t3     cross-zone dedup from a sealed source zone
 *   t4     zone reset clears identity state (post-reset rewrite misses)
 *   t5     reset-pending protocol: deferred reset, reads stay valid,
 *          appends rejected, discard (UREF) finalizes the reset
 *   t7w    T7 phase 1: run the versioning workload, print device stats
 *   t6p1   T6 phase 1: write + incrementally record ACKed (lba,seed)
 *   t6p2   T6 phase 2: verify every ACKed LBA (after crash/restart)
 *   t8     zone-limit pressure (device booted with lz_max_open_zones)
 *   t9     latency microbenchmarks (hit / miss / read paths)
 *   bench  async QD-N throughput run (backup profile)
 *
 * Build: make (inside an SPDK tree, examples/nvme/lzd style).
 */
#include "spdk/stdinc.h"
#include "spdk/env.h"
#include "spdk/nvme.h"

#include "lzd.h"

#include <inttypes.h>
#include <stdarg.h>

/* ---------------- PRNG & content (self-contained) ---------------- */
static uint64_t xs_state = 88172645463325252ULL;
static inline uint64_t xs64(void)
{
    uint64_t x = xs_state;
    x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
    xs_state = x;
    return x * 0x2545F4914F6CDD1DULL;
}

static uint64_t mix_seed(uint64_t x)
{
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

static void fill_page(uint64_t seed, uint8_t *pg)
{
    uint64_t *w = (uint64_t *)pg;
    uint64_t s = mix_seed(seed);
    int i;
    if (!s) s = 1;
    for (i = 0; i < LZD_PAGE / 8; i++) {
        s ^= s >> 12; s ^= s << 25; s ^= s >> 27;
        w[i] = s * 0x2545F4914F6CDD1DULL;
    }
}

/* ---------------- global state ---------------- */
static struct lzd_ctx *g_ctx;
static enum lzd_mode g_mode = LZD_M_NATIVE;
static int g_pass, g_fail;

static void check(int cond, const char *fmt, ...)
{
    va_list ap;
    if (cond) {
        g_pass++;
        return;
    }
    g_fail++;
    printf("  FAIL: ");
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    printf("\n");
}

static double now_us(void)
{
    return (double)spdk_get_ticks() * 1e6 / (double)spdk_get_ticks_hz();
}

/* latency histogram */
typedef struct { double v[65536]; int n; } hist_t;
static void hadd(hist_t *h, double us)
{
    if (h->n < 65536) h->v[h->n++] = us;
}
static int dbl_cmp(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}
static double hpct(hist_t *h, double p)
{
    if (!h->n) return 0;
    qsort(h->v, h->n, sizeof(double), dbl_cmp);
    int k = (int)(p / 100.0 * h->n);
    if (k >= h->n) k = h->n - 1;
    return h->v[k];
}

/* ACKed-write journal for T6 (appended + flushed per record) */
static const char *ACK_FILE = "/tmp/lzd_ack.txt";
static void ack_record(uint64_t lba, uint64_t seed)
{
    FILE *f = fopen(ACK_FILE, "a");
    if (!f) return;
    fprintf(f, "%" PRIu64 " %" PRIu64 "\n", lba, seed);
    fclose(f);   /* open/append/close per record: durable on process kill */
}

/* pages per zone (4 KiB pages, 512 B LBAs -> 8 LBAs per page) */
static uint64_t zone_pages(struct lzd_ctx *c)
{
    return lzd_zone_size(c) / 8;
}

/* groups whose content has been written this run (test isolation) */
static uint8_t g_used_groups[LZD_MAX_GROUPS];

/* write helper following the configured mode */
static int wr(struct lzd_ctx *c, void *page, uint64_t *lba)
{
    g_used_groups[lzd_group_of(lzd_simhash(page))] = 1;
    if (g_mode == LZD_M_NATIVE) {
        return lzd_dup_append(c, page, lba);
    }
    return lzd_zone_append_routed(c, page, lba);
}

static int verify_read(struct lzd_ctx *c, uint64_t lba, uint64_t seed)
{
    uint8_t *buf = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint8_t *exp = malloc(LZD_PAGE);
    int rc = -1;
    int rrc;
    if (!buf || !exp) goto out;
    rrc = lzd_read(c, lba, buf);
    if (rrc != 0) {
        printf("  vrdbg read-err lba=%" PRIu64 " rc=%d\n", lba, rrc);
        rc = -1; goto out;
    }
    fill_page(seed, exp);
    rc = memcmp(buf, exp, LZD_PAGE) == 0 ? 0 : -2;
    if (rc == -2) {
        printf("  vrdbg mismatch lba=%" PRIu64 " seed=%" PRIu64
               " got=%02x%02x%02x%02x want=%02x%02x%02x%02x\n",
               lba, seed,
               buf[0], buf[1], buf[2], buf[3],
               exp[0], exp[1], exp[2], exp[3]);
    }
out:
    spdk_dma_free(buf);
    free(exp);
    return rc;
}

/* ---------------- info ---------------- */
static int do_info(void)
{
    lzd_stats st;
    lzd_zinfo zi;
    lzd_zone_desc *z;
    uint32_t i, n = 64;
    int rc;

    printf("info: nsze=%" PRIu64 " zones=%u zone_size_lbas=%" PRIu64
           " (%" PRIu64 " pages/zone) mode=%s\n",
           lzd_ns_size(g_ctx), lzd_num_zones(g_ctx), lzd_zone_size(g_ctx),
           zone_pages(g_ctx), g_mode == LZD_M_NATIVE ? "native" : "fallback");

    z = calloc(64, sizeof(*z));
    rc = lzd_zone_report(g_ctx, z, &n);
    check(rc == 0, "zone report rc=%d", rc);
    printf("info: %u zones reported\n", n);
    for (i = 0; i < n && i < 6; i++) {
        printf("  zone %u: state=0x%x zslba=%" PRIu64 " wp=%" PRIu64
               " zcap=%" PRIu64 "\n", i, z[i].zs >> 4, z[i].zslba, z[i].wp,
               z[i].zcap);
    }
    free(z);

    rc = lzd_get_stats(g_ctx, &st);
    check(rc == 0, "stats rc=%d", rc);
    check(st.magic == 0x4C5A5450u, "stats magic=%x", st.magic);
    printf("info: device mode=%u r=%u fallback=%u\n", st.mode, st.r,
           st.fallback);

    rc = lzd_zone_query(g_ctx, 0, &zi);
    check(rc == 0, "zone query rc=%d", rc);
    check(zi.magic == 0x4C5A5A49u, "zinfo magic=%x", zi.magic);
    printf("info: zone0 lz_state=%u zns_state=%u incoming=%u\n",
           zi.lz_state, zi.zns_state, zi.incoming_refs);
    return 0;
}

/* ---------------- t1: duplicate detection ---------------- */
static int do_t1(void)
{
    uint8_t *p1 = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint8_t *p2 = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint8_t *rb = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint64_t l1 = 0, l2 = 0, l3 = 0, lu = 0;
    lzd_stats s0, s1;
    int rc;

    fill_page(0xAAAA, p1);
    fill_page(0xBBBB, p2);

    lzd_get_stats(g_ctx, &s0);

    rc = wr(g_ctx, p1, &l1);
    check(rc == 0, "dup_append#1 rc=%d", rc);
    rc = wr(g_ctx, p1, &l2);
    check(rc == 0, "dup_append#2 rc=%d", rc);
    rc = wr(g_ctx, p1, &l3);
    check(rc == 0, "dup_append#3 rc=%d", rc);
    rc = wr(g_ctx, p2, &lu);
    check(rc == 0, "dup_append unique rc=%d", rc);

    check(l1 != l2 && l2 != l3, "logical slots must differ (%" PRIu64
          " %" PRIu64 " %" PRIu64 ")", l1, l2, l3);

    {
        int rrc = lzd_read(g_ctx, l1, rb);
        int off = -1, i;
        if (rrc == 0) {
            for (i = 0; i < LZD_PAGE; i++) {
                if (rb[i] != ((uint8_t *)p1)[i]) { off = i; break; }
            }
        }
        check(rrc == 0 && off < 0,
              "read l1 (rrc=%d first-diff-off=%d got=%02x want=%02x)", rrc,
              off, off >= 0 ? rb[off] : 0, off >= 0 ? ((uint8_t *)p1)[off] : 0);
        rrc = lzd_read(g_ctx, l2, rb);
        check(rrc == 0 && memcmp(rb, p1, LZD_PAGE) == 0, "read l2 (rrc=%d)",
              rrc);
        rrc = lzd_read(g_ctx, l3, rb);
        check(rrc == 0 && memcmp(rb, p1, LZD_PAGE) == 0, "read l3 (rrc=%d)",
              rrc);
        rrc = lzd_read(g_ctx, lu, rb);
        check(rrc == 0 && memcmp(rb, p2, LZD_PAGE) == 0, "read unique (rrc=%d)",
              rrc);
    }

    lzd_get_stats(g_ctx, &s1);
    if (g_mode == LZD_M_NATIVE) {
        check(s1.n_dup_append - s0.n_dup_append == 4,
              "n_dup_append delta=%" PRIu64,
              s1.n_dup_append - s0.n_dup_append);
    }
    /* fallback mode issues zone_append, so the 0xB0 counter stays put */
    check(s1.n_writes - s0.n_writes == 4, "n_writes delta=%" PRIu64,
          s1.n_writes - s0.n_writes);
    check(s1.n_hits - s0.n_hits == 2, "n_hits delta=%" PRIu64,
          s1.n_hits - s0.n_hits);
    check(s1.data_pages_programmed - s0.data_pages_programmed == 2,
          "physical pages delta=%" PRIu64,
          s1.data_pages_programmed - s0.data_pages_programmed);
    check(s1.desc_total - s0.desc_total == 2, "desc_total delta=%" PRIu64,
          s1.desc_total - s0.desc_total);

    spdk_dma_free(p1); spdk_dma_free(p2); spdk_dma_free(rb);
    return 0;
}

/* ---------------- t2: adversarial content ---------------- */
static int do_t2(void)
{
    enum { N = 20000 };           /* hashed (not all written) */
    uint8_t *pg = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint32_t *sh = malloc(N * 4);
    uint64_t *fp = malloc(N * 8);
    uint64_t seeds[N];
    int i, j, found = 0;
    uint64_t l1 = 0, l2 = 0;

    /* find two distinct pages whose SimHash collides (same route group
     * candidate space) but whose fingerprints differ */
    for (i = 0; i < N; i++) {
        seeds[i] = 0xC0FFEE + i;
        fill_page(seeds[i], pg);
        sh[i] = lzd_simhash(pg);
        fp[i] = lzd_fingerprint(pg);
    }
    for (i = 0; i < N && !found; i++) {
        for (j = i + 1; j < N; j++) {
            if (sh[i] == sh[j] && fp[i] != fp[j]) {
                found = 1;
                printf("t2: simhash collision pair seeds %#" PRIx64
                       "/%#" PRIx64 " lsh=%08x\n", seeds[i], seeds[j], sh[i]);
                fill_page(seeds[i], pg);
                check(wr(g_ctx, pg, &l1) == 0, "write A");
                fill_page(seeds[j], pg);
                check(wr(g_ctx, pg, &l2) == 0, "write B");
                check(l1 != l2, "A/B must get distinct slots");
                check(verify_read(g_ctx, l1, seeds[i]) == 0,
                      "A must read back as A (no false dedup)");
                check(verify_read(g_ctx, l2, seeds[j]) == 0,
                      "B must read back as B (no false dedup)");
                break;
            }
        }
    }
    check(found, "no simhash collision found in %d pages (unexpected)", N);
    if (!found) {
        goto out;
    }

    /* distinct pages with colliding 64-bit fingerprints: search a
     * birthday window; finding none in 2e4 is expected (p ~ 2e-11);
     * the global read-back verification covers the guarantee         */
    {
        int fpc = 0;
        for (i = 0; i < N && !fpc; i++) {
            for (j = i + 1; j < N; j++) {
                if (fp[i] == fp[j] && sh[i] != sh[j]) {
                    fpc = 1;
                    break;
                }
            }
        }
        printf("t2: 64-bit fingerprint collision in sample: %s\n",
               fpc ? "FOUND (unexpected!)" : "none (expected)");
        check(!fpc, "unexpected fingerprint collision");
    }
out:
    spdk_dma_free(pg);
    free(sh); free(fp);
    return 0;
}

/* ---------------- t3: sealed-source cross-zone dedup ---------------- */
static int do_t3(void)
{
    uint8_t *pg = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint64_t lba_p = 0, lba_d = 0;
    uint64_t zp = zone_pages(g_ctx);
    uint32_t g, target_group;
    uint64_t s0_writes, s1_writes;
    lzd_stats s0, s1;
    int wrote = 0, tries;
    uint64_t seed_p = 0x7EE4;

    /* choose a page, learn its group; fill that group's zone with
     * group-mates until the primary rotates, then dedup the early page
     * from the sealed zone                    */
    fill_page(seed_p, pg);
    do { seed_p += 1; fill_page(seed_p, pg);
     target_group = lzd_group_of(lzd_simhash(pg));
 } while (g_used_groups[target_group]);
    printf("t3: target group=%u zone_pages=%" PRIu64 "\n", target_group, zp);

    check(wr(g_ctx, pg, &lba_p) == 0, "write seed page");
    lzd_get_stats(g_ctx, &s0);

    /* fill exactly one zone (zp pages) + one rollover write so the
     * group rotates to a fresh zone; the dup then hits via lineage[0]
     * within the r=2 probe budget                                        */
    for (tries = 0; tries < (int)zp * 128 && wrote < (int)zp; tries++) {
        uint64_t seed = 0x900000 + tries;
        fill_page(seed, pg);
        g = lzd_group_of(lzd_simhash(pg));
        if (g != target_group) {
            continue;
        }
        uint64_t l;
        if (wr(g_ctx, pg, &l) != 0) {
            break;
        }
        wrote++;
    }
    printf("t3: filled %d group-mate pages\n", wrote);
    check(wrote >= (int)zp - 1, "zone filled incl. seed (wrote=%d zp=%" PRIu64 ")",
          wrote, zp);

    /* duplicate of the early page: must hit from the sealed zone */
    fill_page(seed_p, pg);
    { int drc = wr(g_ctx, pg, &lba_d);
      check(drc == 0, "write dup of seed page (rc=%d)", drc); }
    check(lba_d != lba_p, "dup must get a new logical slot");
    check(verify_read(g_ctx, lba_d, seed_p) == 0, "dup read-back");
    check(verify_read(g_ctx, lba_p, seed_p) == 0, "source read-back");

    lzd_get_stats(g_ctx, &s1);
    s0_writes = s1.n_writes;
    (void)s0_writes;
    check(s1.hit_src_sealed - s0.hit_src_sealed >= 1,
          "hit_src_sealed delta=%" PRIu64,
          s1.hit_src_sealed - s0.hit_src_sealed);
    check(s1.cross_zone_descs - s0.cross_zone_descs >= 1,
          "cross_zone_descs delta=%" PRIu64,
          s1.cross_zone_descs - s0.cross_zone_descs);

    spdk_dma_free(pg);
    return 0;
}

/* ---------------- t4: reset clears identity state ---------------- */
static int do_t4(void)
{
    uint8_t *pg = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint64_t l1 = 0, l2 = 0, zs;
    lzd_stats s0, s1;
    lzd_zone_desc *zd = calloc(64, sizeof(*zd));
    uint32_t n = 64;
    int deferred = 1, rc;

    fill_page(0x4E4E, pg);
    check(wr(g_ctx, pg, &l1) == 0, "t4 write");
    zs = (l1 / lzd_zone_size(g_ctx)) * lzd_zone_size(g_ctx);

    rc = lzd_zone_reset(g_ctx, zs, &deferred);
    check(rc == 0 && !deferred, "reset rc=%d deferred=%d", rc, deferred);

    rc = lzd_zone_report(g_ctx, zd, &n);
    check(rc == 0, "report after reset");
    {
        uint32_t zid = (uint32_t)(l1 / lzd_zone_size(g_ctx));
        check(zid < n && (zd[zid].zs >> 4) == 0x01, "zone %u state=%u",
              zid, zid < n ? zd[zid].zs >> 4 : 0);
        check(zid < n && zd[zid].wp == zd[zid].zslba, "wp back to start");
    }

    /* same content after reset: must be a MISS (identity cleared) */
    lzd_get_stats(g_ctx, &s0);
    fill_page(0x4E4E, pg);
    check(wr(g_ctx, pg, &l2) == 0, "rewrite after reset");
    lzd_get_stats(g_ctx, &s1);
    check(s1.n_writes - s0.n_writes == 1, "one write");
    check(s1.n_hits == s0.n_hits, "must be a miss (hits %" PRIu64 "->%"
          PRIu64 ")", s0.n_hits, s1.n_hits);
    check(verify_read(g_ctx, l2, 0x4E4E) == 0, "read back rewrite");

    spdk_dma_free(pg);
    free(zd);
    return 0;
}

/* ---------------- t5: reset-pending protocol ---------------- */
static int do_t5(void)
{
    uint8_t *pg = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint64_t zp = zone_pages(g_ctx);
    uint32_t target_group = 0, g;
    uint64_t zs_a = 0, seed_p = 0x5EED;
    uint64_t l_p = 0, l_d = 0, l_extra = 0, l_plain = 0;
    lzd_zinfo zi;
    lzd_stats s0, s1;
    int wrote = 0, tries, deferred = -1, rc;
    lzd_zone_desc *zd = calloc(64, sizeof(*zd));
    uint32_t n = 64;

    /* step 1: seed page in zone A + fill A with group-mates until the
     * group's primary rotates to A2                                  */
    fill_page(seed_p, pg);
    do { seed_p += 1; fill_page(seed_p, pg);
     target_group = lzd_group_of(lzd_simhash(pg));
 } while (g_used_groups[target_group]);
    check(wr(g_ctx, pg, &l_p) == 0, "t5 seed write");
    uint32_t zid_a = (uint32_t)(l_p / lzd_zone_size(g_ctx));
    zs_a = (uint64_t)zid_a * lzd_zone_size(g_ctx);
    printf("t5: zone A = %u (group %u), zone_pages=%" PRIu64 "\n", zid_a,
           target_group, zp);

    for (tries = 0; tries < (int)zp * 128 && wrote < (int)zp; tries++) {
        uint64_t seed = 0x500000 + tries;
        fill_page(seed, pg);
        g = lzd_group_of(lzd_simhash(pg));
        if (g != target_group) continue;
        uint64_t l;
        if (wr(g_ctx, pg, &l) != 0) break;
        wrote++;
    }
    check(wrote >= (int)zp - 1, "zone A filled incl. seed (wrote=%d)", wrote);

    /* step 2: duplicate of the seed page -> descriptor in A2 references
     * A: incoming[A] >= 1                                              */
    fill_page(seed_p, pg);
    { int drc = wr(g_ctx, pg, &l_d);
      check(drc == 0, "dup write (rc=%d)", drc); }
    uint32_t zid_d = (uint32_t)(l_d / lzd_zone_size(g_ctx));
    printf("t5: dup LBA %" PRIu64 " in zone %u\n", l_d, zid_d);
    check(zid_d != zid_a, "dup must land in a different zone");
    check(verify_read(g_ctx, l_d, seed_p) == 0, "dup read-back");

    rc = lzd_zone_query(g_ctx, zid_a, &zi);
    check(rc == 0, "query A");
    check(zi.incoming_refs >= 1, "incoming[A]=%u", zi.incoming_refs);

    /* one more referencing descriptor for the multi-ref case */
    fill_page(seed_p, pg);
    check(wr(g_ctx, pg, &l_extra) == 0, "second dup");
    check(verify_read(g_ctx, l_extra, seed_p) == 0, "second dup read-back");

    lzd_get_stats(g_ctx, &s0);

    /* step 3: reset A -> deferred */
    rc = lzd_zone_reset(g_ctx, zs_a, &deferred);
    check(rc == 0 && deferred == 1, "reset A must defer (rc=%d deferred=%d)",
          rc, deferred);

    rc = lzd_zone_query(g_ctx, zid_a, &zi);
    check(rc == 0, "query A after deferred reset");
    check(zi.lz_state == 3, "A lz_state=%u (want 3=pending)", zi.lz_state);
    check(verify_read(g_ctx, l_d, seed_p) == 0,
          "referencing read must stay correct after defer");
    check(verify_read(g_ctx, l_extra, seed_p) == 0, "second ref stays correct");

    /* step 4: appends to the pending zone are rejected */
    fill_page(0xDEAD1, pg);
    rc = lzd_zone_append_to(g_ctx, zid_a, pg, &l_plain);
    check(rc != 0, "append to pending zone must fail (rc=%d)", rc);
    check(rc == -(0x10000 | (0xba << 1) | 1),
          "expected ZONE_READ_ONLY, rc=%d", rc);

    /* step 5: discard the descriptors (UREF) -> reset finalizes */
    check(lzd_discard(g_ctx, l_d, 8) == 0, "discard dup lba");
    rc = lzd_zone_query(g_ctx, zid_a, &zi);
    check(rc == 0 && zi.incoming_refs >= 1,
          "one incoming ref must remain (incoming=%u)", zi.incoming_refs);
    check(lzd_discard(g_ctx, l_extra, 8) == 0, "discard second dup lba");

    rc = lzd_zone_query(g_ctx, zid_a, &zi);
    check(rc == 0, "query A after discards");
    check(zi.incoming_refs == 0, "incoming must be 0 (got %u)",
          zi.incoming_refs);
    check(zi.lz_state != 3, "pending state must be finalized (lz=%u)",
          zi.lz_state);

    n = 64;
    rc = lzd_zone_report(g_ctx, zd, &n);
    check(rc == 0, "report after finalize");
    check(zid_a < n && (zd[zid_a].zs >> 4) == 0x01,
          "zone A physically EMPTY after finalize (state=%u)",
          zid_a < n ? zd[zid_a].zs >> 4 : 0);

    lzd_get_stats(g_ctx, &s1);
    check(s1.n_pending_resets - s0.n_pending_resets >= 1, "pending_resets");
    check(s1.n_pending_finalized - s0.n_pending_finalized >= 1,
          "pending_finalized");
    check(s1.n_uref - s0.n_uref >= 2, "uref delta=%" PRIu64,
          s1.n_uref - s0.n_uref);
    check(s1.n_discard_descs - s0.n_discard_descs >= 2, "discard descs");
    check(s1.n_appends_rejected_pending - s0.n_appends_rejected_pending >= 1,
          "append rejections counted");

    /* step 6: a fresh reset of the (now empty) zone completes at once */
    deferred = -1;
    rc = lzd_zone_reset(g_ctx, zs_a, &deferred);
    check(rc == 0 && !deferred, "post-finalize reset rc=%d deferred=%d",
          rc, deferred);

    spdk_dma_free(pg);
    free(zd);
    return 0;
}

/* ---------------- t7w: versioning workload (stats probe) ---------------- */
struct fam_page { uint64_t seed; int fresh; };

static int g_t7_scale = 1;    /* -s N: pages-per-family-version x N */
static int g_t7_versions = 24; /* -V N: version count */

static int do_t7w(const char *tag)
{
    /* backup profile: fams=5, shared=0.88, modf=0.02; PPV scales with the
     * device zone size so the workload fills ~4.5 zones regardless of
     * zone geometry (rotation count matched across zone-size arms) */
    enum { FAMS = 5 };
    const int PPV = 96 * g_t7_scale;
    int versions = g_t7_versions;
    uint8_t *pg = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    struct fam_page (*content)[PPV] = malloc(sizeof(struct fam_page) *
                                             FAMS * (size_t)PPV);
    uint64_t uniq = 0, writes = 0, seed_ctr = 1;
    int f, v, k;
    lzd_stats st;

    for (f = 0; f < FAMS; f++) {
        for (k = 0; k < PPV; k++) {
            content[f][k].seed = seed_ctr++;
            content[f][k].fresh = 1;
        }
    }
    for (v = 0; v < versions; v++) {
        for (f = 0; f < FAMS; f++) {
            for (k = 0; k < PPV; k++) {
                double u = (double)(xs64() & 0xffffffff) / 4294967296.0;
                if (v > 0 && u < 0.88) {
                    /* identical to previous version */
                } else if (v > 0 && u < 0.88 + 0.02) {
                    content[f][k].seed = seed_ctr++;   /* modified: unique */
                } else if (v > 0) {
                    content[f][k].seed = seed_ctr++;
                }
                fill_page(content[f][k].seed, pg);
                uint64_t l;
                if (wr(g_ctx, pg, &l) == 0) {
                    writes++;
                    if (content[f][k].fresh) {
                        content[f][k].fresh = 0;
                        uniq++;
                    }
                }
            }
        }
    }
    lzd_get_stats(g_ctx, &st);
    double exp_rate = v ? (double)(writes - uniq) / writes : 0;
    printf("T7 %s writes=%" PRIu64 " uniq=%" PRIu64 " exp_dup=%.4f "
           "dev_hit=%.4f phys=%" PRIu64 "\n",
           tag, writes, uniq, exp_rate,
           st.n_writes ? (double)st.n_hits / st.n_writes : 0,
           st.data_pages_programmed);
    fflush(stdout);

    spdk_dma_free(pg);
    free(content);
    return 0;
}

/* ---------------- t6p1/t6p2: crash journal ---------------- */
static int do_t6p1(void)
{
    enum { N = 4096 };
    uint8_t *pg = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint64_t i, l;
    FILE *f = fopen(ACK_FILE, "w");
    fclose(f);

    for (i = 0; i < N; i++) {
        uint64_t seed = (i % 3 == 0) ? 0x6666 : 0x600000 + i;  /* 1/3 dups */
        fill_page(seed, pg);
        if (wr(g_ctx, pg, &l) == 0) {
            ack_record(l, seed);
        }
        if (i % 512 == 511) {
            usleep(2000);   /* window for the runner to kill us */
        }
    }
    printf("T6P1 done: %d ACKed\n", N);
    spdk_dma_free(pg);
    return 0;
}

static int do_t6p2(void)
{
    FILE *f = fopen(ACK_FILE, "r");
    uint64_t lba, seed, n = 0, bad = 0;
    int rc;

    if (!f) {
        check(0, "no ack journal");
        return 1;
    }
    while (fscanf(f, "%" SCNu64 " %" SCNu64, &lba, &seed) == 2) {
        n++;
        rc = verify_read(g_ctx, lba, seed);
        if (rc != 0) {
            bad++;
            if (bad < 5) {
                printf("  t6p2 BAD lba=%" PRIu64 " seed=%" PRIu64 " rc=%d\n",
                       lba, seed, rc);
            }
        }
    }
    fclose(f);
    check(n > 0, "empty journal");
    check(bad == 0, "%" PRIu64 "/%" PRIu64 " ACKed LBAs corrupted", bad, n);
    printf("T6P2 verified=%" PRIu64 " bad=%" PRIu64 "\n", n, bad);
    return 0;
}

/* ---------------- t8: zone-limit pressure ---------------- */
static int do_t8(void)
{
    /* low-dedup cloud-like profile across many groups; the device may
     * reject appends at the AOR limit -- the loop recycles zones       */
    enum { N = 8192 };
    uint8_t *pg = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint64_t ok = 0, retries = 0, l;
    int i;
    lzd_stats s0, s1;
    lzd_zone_desc *zd = calloc(256, sizeof(*zd));
    uint32_t n = 256, open_now;

    lzd_get_stats(g_ctx, &s0);
    for (i = 0; i < N; i++) {
        fill_page(0x800000 + i, pg);
        int rc = wr(g_ctx, pg, &l);
        int guard = 0;
        while (rc != 0 && guard++ < 200) {
            retries++;
            /* recycle: reset the oldest non-empty zone */
            uint32_t zn = 256;
            uint32_t j, victim = UINT32_MAX;
            uint64_t best_wp = ~0ULL;
            if (lzd_zone_report(g_ctx, zd, &zn) == 0) {
                for (j = 0; j < zn; j++) {
                    uint32_t st = zd[j].zs >> 4;
                    if ((st == 0x0E || st == 0x04 || st == 0x02) &&
                        zd[j].wp < best_wp) {
                        best_wp = zd[j].wp;
                        victim = j;
                    }
                }
            }
            if (victim == UINT32_MAX) {
                break;
            }
            int deferred;
            lzd_zone_reset(g_ctx, (uint64_t)victim * lzd_zone_size(g_ctx),
                           &deferred);
            rc = wr(g_ctx, pg, &l);
        }
        if (rc == 0) {
            ok++;
        }
    }
    lzd_get_stats(g_ctx, &s1);

    n = 256;
    check(lzd_zone_report(g_ctx, zd, &n) == 0, "t8 report");
    open_now = 0;
    for (i = 0; (uint32_t)i < n; i++) {
        uint32_t st = zd[i].zs >> 4;
        if (st == 0x02 || st == 0x03) open_now++;
    }
    printf("T8 ok=%" PRIu64 "/%d retries=%" PRIu64 " open_now=%u "
           "rejected_pending=%" PRIu64 "\n", ok, N, retries, open_now,
           s1.n_appends_rejected_pending);
    check(ok == N, "all writes completed (ok=%" PRIu64 ")", ok);
    printf("T8 device limit check: report open=%u (limit enforced by "
           "device AOR)\n", open_now);

    spdk_dma_free(pg);
    free(zd);
    return 0;
}

/* ---------------- t9: latency microbench ---------------- */
static int do_t9(void)
{
    enum { N = 512 };
    uint8_t *pg = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    uint8_t *rb = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
    hist_t h_hit, h_miss, h_rd_data, h_rd_desc;
    uint64_t l0 = 0, l = 0, t0;
    int i;

    memset(&h_hit, 0, sizeof(h_hit));
    memset(&h_miss, 0, sizeof(h_miss));
    memset(&h_rd_data, 0, sizeof(h_rd_data));
    memset(&h_rd_desc, 0, sizeof(h_rd_desc));

    fill_page(0x9999, pg);
    wr(g_ctx, pg, &l0);            /* data-backed slot */

    /* hits: same page repeatedly (first of these creates a descriptor) */
    for (i = 0; i < N; i++) {
        t0 = now_us();
        wr(g_ctx, pg, &l);
        hadd(&h_hit, (now_us() - t0));
    }
    /* misses: unique pages */
    for (i = 0; i < N; i++) {
        fill_page(0xA00000 + i, pg);
        t0 = now_us();
        wr(g_ctx, pg, &l);
        hadd(&h_miss, (now_us() - t0));
        if (i == 0) {
            check(lzd_read(g_ctx, l, rb) == 0, "t9 miss read");
        }
    }
    /* read data-backed vs descriptor-backed */
    for (i = 0; i < N; i++) {
        t0 = now_us();
        lzd_read(g_ctx, l0, rb);
        hadd(&h_rd_data, (now_us() - t0));
        t0 = now_us();
        lzd_read(g_ctx, l, rb);
        hadd(&h_rd_desc, (now_us() - t0));
    }
    printf("T9 mode=%s hit_us p50=%.1f p99=%.1f | miss_us p50=%.1f p99=%.1f | "
           "rd_data p50=%.1f p99=%.1f | rd_desc p50=%.1f p99=%.1f\n",
           g_mode == LZD_M_NATIVE ? "native" : "fallback",
           hpct(&h_hit, 50), hpct(&h_hit, 99),
           hpct(&h_miss, 50), hpct(&h_miss, 99),
           hpct(&h_rd_data, 50), hpct(&h_rd_data, 99),
           hpct(&h_rd_desc, 50), hpct(&h_rd_desc, 99));
    fflush(stdout);
    /* NOTE: the FEMU prototype serializes all dedup bookkeeping under
     * one lock, so hit and miss converge to the same critical section;
     * the split is reported, not asserted (LZSim carries the timing). */
    printf("T9 hit-vs-miss p50 delta: %+.1f us (prototype-serialized)\n",
           hpct(&h_hit, 50) - hpct(&h_miss, 50));

    spdk_dma_free(pg);
    spdk_dma_free(rb);
    return 0;
}

/* ---------------- bench: async QD-N throughput ---------------- */
static int g_bench_slot_free[64];
static uint64_t g_bench_done, g_bench_err;

static void bench_cb(void *arg, const struct spdk_nvme_cpl *cpl)
{
    int s = (int)(intptr_t)arg;
    g_bench_done++;
    if (spdk_nvme_cpl_is_error(cpl)) {
        g_bench_err++;
    }
    g_bench_slot_free[s] = 1;
}

static int do_bench(int qd, int npages)
{
    struct spdk_nvme_cmd cmd;
    struct spdk_nvme_qpair *qp = lzd_qpair(g_ctx);
    struct spdk_nvme_ctrlr *ctrlr = lzd_ctrlr(g_ctx);
    uint8_t *bufs[64];
    uint64_t uniq = 0, dupseed = 0xBEEF;
    uint64_t submitted = 0; double t0, el;
    int i;
    lzd_stats st;

    if (qd > 64) qd = 64;
    for (i = 0; i < qd; i++) {
        bufs[i] = spdk_dma_zmalloc(LZD_PAGE, 4096, NULL);
        g_bench_slot_free[i] = 1;
    }
    g_bench_done = g_bench_err = 0;

    t0 = now_us();
    while (submitted < (uint64_t)npages) {
        for (i = 0; i < qd && submitted < (uint64_t)npages; i++) {
            if (!g_bench_slot_free[i] ||
                submitted - g_bench_done >= (uint64_t)qd) {
                continue;
            }
            /* backup-like: 8/10 duplicate pages */
            if ((xs64() % 10) < 8) {
                fill_page(dupseed, bufs[i]);
            } else {
                fill_page(0xB00000 + uniq++, bufs[i]);
            }
            memset(&cmd, 0, sizeof(cmd));
            if (g_mode == LZD_M_NATIVE) {
                cmd.opc = LZD_CMD_DUP_APPEND;
                cmd.nsid = 1;
                cmd.cdw10 = 0;
            } else {
                uint32_t lsh = lzd_simhash(bufs[i]);
                int zid = lzd_route_pick(g_ctx, lsh);
                if (zid < 0) {
                    lzd_get_stats(g_ctx, &st);
                    printf("BENCH fallback: out of routable zones\n");
                    goto drain;
                }
                cmd.opc = 0x7D;   /* zone append to the routed zone */
                cmd.nsid = 1;
                {
                    uint64_t zslba = (uint64_t)zid * lzd_zone_size(g_ctx);
                    cmd.cdw10 = (uint32_t)zslba;
                    cmd.cdw11 = (uint32_t)(zslba >> 32);
                    cmd.cdw12 = 0;
                }
            }
            if (spdk_nvme_ctrlr_cmd_io_raw(ctrlr, qp, &cmd, bufs[i],
                                           LZD_PAGE, bench_cb,
                                           (void *)(intptr_t)i) == 0) {
                g_bench_slot_free[i] = 0;
                submitted++;
            }
        }
        spdk_nvme_qpair_process_completions(qp, 0);
    }
drain:
    while (g_bench_done < submitted) {
        spdk_nvme_qpair_process_completions(qp, 0);
    }
    el = now_us() - t0;

    lzd_get_stats(g_ctx, &st);
    printf("BENCH mode=%s qd=%d pages=%" PRIu64 " MBps=%.1f err=%" PRIu64 " "
           "dev_hit=%.4f phys=%" PRIu64 "\n",
           g_mode == LZD_M_NATIVE ? "native" : "fallback", qd, submitted,
           el > 0 ? (double)submitted * 4096.0 / (el / 1e6) / 1e6 : 0.0,
           g_bench_err,
           st.n_writes ? (double)st.n_hits / st.n_writes : 0.0,
           st.data_pages_programmed);
    fflush(stdout);

    for (i = 0; i < qd; i++) {
        spdk_dma_free(bufs[i]);
    }
    return 0;
}

/* ---------------- main ---------------- */
static void usage(void)
{
    fprintf(stderr,
        "usage: lzdtest [-m native|fallback] [-t test[,test..]] "
        "[-n npages] [-q qd]\n"
        "  tests: info t1 t2 t3 t4 t5 t7w t6p1 t6p2 t8 t9\n");
}

int main(int argc, char **argv)
{
    struct spdk_env_opts opts;
    const char *tests = "info";
    const char *mode = "native";
    int rc, ch, npages = 20000, qd = 32;
    char *tok, *save;

    while ((ch = getopt(argc, argv, "m:t:n:q:s:V:h")) != -1) {
        switch (ch) {
        case 'm': mode = optarg; break;
        case 't': tests = optarg; break;
        case 'n': npages = atoi(optarg); break;
        case 'q': qd = atoi(optarg); break;
        case 's': g_t7_scale = atoi(optarg); break;
        case 'V': g_t7_versions = atoi(optarg); break;
        default: usage(); return 1;
        }
    }
    g_mode = !strcmp(mode, "fallback") ? LZD_M_FALLBACK : LZD_M_NATIVE;

    spdk_env_opts_init(&opts);
    opts.name = "lzdtest";
    opts.shm_id = -1;
    opts.mem_size = 512;          /* MB of hugepages */
    if (spdk_env_init(&opts) < 0) {
        fprintf(stderr, "spdk_env_init failed (hugepages?)\n");
        return 1;
    }

    rc = lzd_open(&g_ctx, g_mode);
    if (rc) {
        fprintf(stderr, "lzd_open failed: %d\n", rc);
        return 1;
    }
    printf("lzdtest: mode=%s\n", mode);

    for (tok = strtok_r((char *)tests, ",", &save); tok;
         tok = strtok_r(NULL, ",", &save)) {
        if (!strcmp(tok, "info")) do_info();
        else if (!strcmp(tok, "t1")) do_t1();
        else if (!strcmp(tok, "t2")) do_t2();
        else if (!strcmp(tok, "t3")) do_t3();
        else if (!strcmp(tok, "t4")) do_t4();
        else if (!strcmp(tok, "t5")) do_t5();
        else if (!strcmp(tok, "t7w")) do_t7w(g_mode == LZD_M_NATIVE ?
                                             "native" : "fallback");
        else if (!strcmp(tok, "t6p1")) do_t6p1();
        else if (!strcmp(tok, "t6p2")) do_t6p2();
        else if (!strcmp(tok, "t8")) do_t8();
        else if (!strcmp(tok, "t9")) do_t9();
        else if (!strcmp(tok, "bench")) do_bench(qd, npages);
        else { fprintf(stderr, "unknown test %s\n", tok); usage(); return 1; }
    }

    lzd_close(g_ctx);
    printf("LZDTEST PASS=%d FAIL=%d\n", g_pass, g_fail);
    return g_fail ? 1 : 0;
}
