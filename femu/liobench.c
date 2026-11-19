/*
 * liobench.c -- LiZoneDedup guest benchmark for FEMU ZNS (final).
 *
 * Producer-consumer: the generator emits version-interleaved blocks and
 * publishes each block to persistent workers, so every job observes its
 * own version's content before the pool advances.  Re-pulls reference the
 * previous-version snapshot.  FSL mode replays the real fingerprint
 * sequence (content derived deterministically per chunk hash).
 *
 * Build: gcc -O2 -static -o liobench liobench.c -lpthread
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <pthread.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <linux/nvme_ioctl.h>

#define PAGE_SIZE         4096
#define LZ_CMD_DUP_APPEND 0xB0
#define LZ_CMD_STATS      0xB1
#define ZM_RESET          0x04
#define MAX_FAMS          16
#define RING_LBAS         65536
#define MAXZ              4096

/* ---------------- PRNG & content ---------------- */
static uint64_t xs_state = 88172645463325252ULL;
static inline uint64_t xs64(void)
{
    uint64_t x = xs_state;
    x ^= x >> 12; x ^= x << 25; x ^= x >> 27;
    xs_state = x;
    return x * 0x2545F4914F6CDD1DULL;
}

static inline uint64_t mix_seed(uint64_t x)
{
    /* bijective mix: distinct seeds -> distinct states (the old `seed | 1`
     * collapsed every even/odd pair, since pages are seeded with
     * consecutive integers -- a systematic 50% false-duplicate floor). */
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

static void fill_page(uint64_t seed, uint8_t *pg)
{
    uint64_t *w = (uint64_t *)pg;
    uint64_t s = mix_seed(seed);
    if (!s) s = 1;
    int i;
    for (i = 0; i < PAGE_SIZE / 8; i++) {
        s ^= s >> 12; s ^= s << 25; s ^= s >> 27;
        w[i] = s * 0x2545F4914F6CDD1DULL;
    }
}

static void fill_block64(uint64_t seed, uint8_t *p)
{
    uint64_t *w = (uint64_t *)p;
    uint64_t s = mix_seed(seed);
    if (!s) s = 1;
    int i;
    for (i = 0; i < 8; i++) {
        s ^= s >> 12; s ^= s << 25; s ^= s >> 27;
        w[i] = s * 0x2545F4914F6CDD1DULL;
    }
}

static uint64_t page_hash(const uint8_t *pg)
{
    uint64_t h = 0x243F6A8885A308D3ULL;
    const uint64_t *w = (const uint64_t *)pg;
    int i;
    for (i = 0; i < PAGE_SIZE / 8; i += 8) {
        h ^= w[i];
        h *= 0x9E3779B97F4A7C15ULL;
        h ^= h >> 29;
    }
    return h;
}

/* ---------------- histogram ---------------- */
typedef struct { double *v; int n, cap; } hist_t;
static void hist_init(hist_t *h)
{ h->cap = 1024; h->v = malloc(sizeof(double) * h->cap); h->n = 0; }
static void hist_add(hist_t *h, double us)
{
    if (h->n == h->cap) { h->cap *= 4; h->v = realloc(h->v, sizeof(double) * h->cap); }
    h->v[h->n++] = us;
}
static int dbl_cmp(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y ? 1 : 0;
}
static double hist_pct(hist_t *h, double p)
{
    if (!h->n) return 0;
    qsort(h->v, h->n, sizeof(double), dbl_cmp);
    int k = (int)(p / 100.0 * h->n);
    if (k >= h->n) k = h->n - 1;
    return h->v[k];
}

/* ---------------- NVMe ---------------- */
static int dev_fd = -1;

static int nvme_dup_append(uint8_t *buf, uint32_t *out_lba)
{
    struct nvme_passthru_cmd cmd;
    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = LZ_CMD_DUP_APPEND;
    cmd.nsid = 1;
    cmd.addr = (uint64_t)(uintptr_t)buf;
    cmd.data_len = PAGE_SIZE;
    cmd.cdw10 = 0;                    /* one 4 KiB LBA */
    cmd.timeout_ms = 60000;
    if (ioctl(dev_fd, NVME_IOCTL_IO_CMD, &cmd) < 0) return -errno;
    *out_lba = cmd.result;
    return 0;
}

static int nvme_read_page(uint64_t slba, uint8_t *buf)
{
    struct nvme_passthru_cmd cmd;
    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = 0x02;
    cmd.nsid = 1;
    cmd.addr = (uint64_t)(uintptr_t)buf;
    cmd.data_len = PAGE_SIZE;
    cmd.cdw10 = (uint32_t)(slba & 0xffffffff);
    cmd.cdw11 = (uint32_t)(slba >> 32);
    cmd.cdw12 = 0;                    /* one 4 KiB LBA */
    cmd.timeout_ms = 60000;
    if (ioctl(dev_fd, NVME_IOCTL_IO_CMD, &cmd) < 0) return -errno;
    return 0;
}

static int nvme_zone_reset(uint64_t slba)
{
    struct nvme_passthru_cmd cmd;
    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = 0x79;
    cmd.nsid = 1;
    cmd.cdw10 = (uint32_t)(slba & 0xffffffff);
    cmd.cdw11 = (uint32_t)(slba >> 32);
    cmd.cdw13 = ZM_RESET;
    cmd.timeout_ms = 60000;
    if (ioctl(dev_fd, NVME_IOCTL_IO_CMD, &cmd) < 0) return -errno;
    return 0;
}

static void nvme_stats(void)
{
    struct nvme_passthru_cmd cmd;
    memset(&cmd, 0, sizeof(cmd));
    cmd.opcode = LZ_CMD_STATS;
    cmd.nsid = 1;
    cmd.timeout_ms = 60000;
    ioctl(dev_fd, NVME_IOCTL_IO_CMD, &cmd);
}

static double now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1e6 + ts.tv_nsec / 1e3;
}

/* ---------------- shared state ---------------- */
typedef struct { uint8_t op; int32_t page_off; int16_t fam; int8_t pool_sel; } jjob_t;

static jjob_t *jobs;
static uint64_t njobs, job_head, job_done, gen_done;
static pthread_mutex_t job_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t job_cv = PTHREAD_COND_INITIALIZER;

static uint8_t *pool_cur[MAX_FAMS], *pool_prev[MAX_FAMS];
static int g_ppv;

static uint32_t ring[RING_LBAS];
static uint64_t ring_n;
static pthread_mutex_t wmu = PTHREAD_MUTEX_INITIALIZER;

static uint64_t zone_last[MAXZ];
static int g_zone_pages = 2048;
static int g_max_live_zones = 96;
static pthread_mutex_t zmu = PTHREAD_MUTEX_INITIALIZER;

static int g_hostdedup = 0;
static int g_verbose = 0;
static uint64_t host_uniq_writes;
static uint64_t *hset;
static uint32_t hset_cap = 1 << 21;
static uint64_t *fsl_fps;
static uint8_t rbuf2[4096] __attribute__((aligned(4096)));

static inline uint64_t xs64_r(void *p)
{
    static __thread uint64_t s = 0;
    if (!s) s = (uint64_t)(uintptr_t)p * 0x9E3779B97F4A7C15ULL + 7;
    s ^= s >> 12; s ^= s << 25; s ^= s >> 27;
    return s * 0x2545F4914F6CDD1DULL;
}

static int hset_lookup_add(uint64_t h)
{
    uint32_t i = (uint32_t)(h ^ (h >> 32)) & (hset_cap - 1);
    while (hset[i]) {
        if (hset[i] == h) return 1;
        i = (i + 1) & (hset_cap - 1);
    }
    hset[i] = h;
    return 0;
}

static void maybe_recycle(uint32_t lba, int force)
{
    uint32_t z = lba / ((uint32_t)g_zone_pages * 8);   /* 512B LBAs */
    pthread_mutex_lock(&zmu);
    if (z < MAXZ) zone_last[z] = ring_n;
    int live = 0, i;
    for (i = 0; i < MAXZ; i++) if (zone_last[i]) live++;
    if (live > g_max_live_zones || force) {
        uint64_t best = ~0ULL; int bz = -1;
        for (i = 0; i < MAXZ; i++) {
            if (zone_last[i] && zone_last[i] < best && i != (int)z) {
                best = zone_last[i]; bz = i;
            }
        }
        if (bz >= 0 && best + 8192 < ring_n) {
            pthread_mutex_unlock(&zmu);
            nvme_zone_reset((uint64_t)bz * g_zone_pages * 8);
            pthread_mutex_lock(&zmu);
            zone_last[bz] = 0;
        }
    }
    pthread_mutex_unlock(&zmu);
}

/* publish a filled job batch and wait for completion */
static void jobs_publish(void)
{
    pthread_mutex_lock(&job_mu);
    job_head = 0;
    job_done = 0;
    gen_done = 0;
    pthread_cond_broadcast(&job_cv);
    pthread_mutex_unlock(&job_mu);
    for (;;) {
        pthread_mutex_lock(&job_mu);
        uint64_t d = job_done;
        pthread_mutex_unlock(&job_mu);
        if (d >= njobs) break;
        usleep(100);
    }
}

/* ---------------- worker ---------------- */
typedef struct { hist_t w, r; uint64_t nw, nr, nerr; } tstat_t;

static void *worker(void *arg)
{
    tstat_t *st = arg;
    uint8_t *rbuf = aligned_alloc(4096, PAGE_SIZE);
    /* per-thread content buffer: rbuf2 is a single global, so with
     * -t N every worker would race on it and the pages actually
     * submitted would be a mangled mix (false duplicates). */
    uint8_t *fbuf = aligned_alloc(4096, PAGE_SIZE);
    for (;;) {
        uint64_t j;
        pthread_mutex_lock(&job_mu);
        while (job_head >= njobs && !gen_done) {
            pthread_cond_wait(&job_cv, &job_mu);
        }
        if (job_head >= njobs && gen_done) {
            pthread_mutex_unlock(&job_mu);
            break;
        }
        j = job_head++;
        pthread_mutex_unlock(&job_mu);
        if (j >= njobs) continue;
        jjob_t *jb = &jobs[j];
        __sync_fetch_and_add(&job_done, 1);
        if (jb->op == 1) {
            pthread_mutex_lock(&wmu);
            uint64_t n = ring_n;
            uint32_t lba = n ? ring[xs64_r(&st->nw) % RING_LBAS] : 0;
            pthread_mutex_unlock(&wmu);
            if (n < 100) continue;
            double t0 = now_us();
            int rc = nvme_read_page(lba, rbuf);
            double dt = now_us() - t0;
            if (rc == 0) { hist_add(&st->r, dt); st->nr++; }
            else st->nerr++;
        } else {
            uint8_t *buf;
            if (jb->pool_sel == 2) {
                buf = fbuf;
                fill_page(fsl_fps[jb->page_off], buf);
            } else {
                buf = (jb->pool_sel ? pool_prev[jb->fam] : pool_cur[jb->fam]) +
                      (uint64_t)jb->page_off * PAGE_SIZE;
            }
            if (g_hostdedup) {
                double t0 = now_us();
                uint64_t h = page_hash(buf);
                int dup = hset_lookup_add(h);
                while (now_us() - t0 < 7.1) { }
                if (dup) continue;
                __sync_fetch_and_add(&host_uniq_writes, 1);
            }
            uint32_t lba = 0;
            double t0 = now_us();
            int rc = nvme_dup_append(buf, &lba);
            double dt = now_us() - t0;
            if (rc == 0) {
                hist_add(&st->w, dt);
                st->nw++;
                pthread_mutex_lock(&wmu);
                ring[ring_n % RING_LBAS] = lba;
                ring_n++;
                pthread_mutex_unlock(&wmu);
                if ((st->nw & 1023) == 0) maybe_recycle(lba, 0);
            } else {
                st->nerr++;
                if (rc == -ENOSPC) maybe_recycle(lba, 1);
            }
        }
    }
    free(rbuf);
    free(fbuf);
    return NULL;
}

/* ---------------- version advance ---------------- */
static void vsrc_advance(int f, double shared, double modf, uint64_t *counter)
{
    uint8_t *tmp = malloc(PAGE_SIZE);
    uint8_t *cur = pool_cur[f];
    memcpy(pool_prev[f], cur, (uint64_t)g_ppv * PAGE_SIZE);
    int i;
    for (i = 0; i < g_ppv; i++) {
        double u = (double)(xs64() & 0xffffffff) / 4294967296.0;
        uint8_t *pg = cur + (uint64_t)i * PAGE_SIZE;
        if (u < shared) {
            /* identical */
        } else if (u < shared + modf) {
            memcpy(tmp, pg, PAGE_SIZE);
            int b1 = (int)(xs64() % 64), b2 = (int)(xs64() % 64);
            fill_block64((*counter)++ * 0x9E37 + f, tmp + b1 * 64);
            fill_block64((*counter)++ * 0x51ED + f, tmp + b2 * 64);
            memcpy(pg, tmp, PAGE_SIZE);
        } else {
            fill_page((*counter)++ * 0x1234 + f, pg);
        }
    }
    free(tmp);
}

/* ---------------- result ---------------- */
static void print_result(const char *wl, const char *tag, tstat_t *sts,
                         int nthreads, double el)
{
    hist_t W, R;
    hist_init(&W); hist_init(&R);
    uint64_t nw = 0, nr = 0, nerr = 0;
    int i;
    for (i = 0; i < nthreads; i++) {
        for (int k = 0; k < sts[i].w.n; k++) hist_add(&W, sts[i].w.v[k]);
        for (int k = 0; k < sts[i].r.n; k++) hist_add(&R, sts[i].r.v[k]);
        nw += sts[i].nw; nr += sts[i].nr; nerr += sts[i].nerr;
    }
    double wbw = (double)(g_hostdedup ? host_uniq_writes : nw) * PAGE_SIZE
                 / el / 1e6;
    nvme_stats();
    printf("BENCH tag=%s wl=%s dev_writes=%lu host_uniq=%lu reads=%lu "
           "err=%lu bw_dev_MBps=%.1f el_s=%.1f "
           "wp50=%.1f wp95=%.1f wp99=%.1f rp50=%.1f rp95=%.1f rp99=%.1f\n",
           tag, wl, nw, host_uniq_writes, nr, nerr, wbw, el,
           hist_pct(&W, 50), hist_pct(&W, 95), hist_pct(&W, 99),
           hist_pct(&R, 50), hist_pct(&R, 95), hist_pct(&R, 99));
    fflush(stdout);   /* BENCH must hit serial before poweroff -f */
}

/* ---------------- main ---------------- */
int main(int argc, char **argv)
{
    const char *dev = "/dev/nvme0n1";
    const char *wl = "container";
    int nthreads = 8, total_pages = 0, i, c;
    char tag[64] = "run";
    double read_ratio = 0.15;

    while ((c = getopt(argc, argv, "d:w:t:n:j:z:g:HL:v")) != -1) {
        switch (c) {
        case 'd': dev = optarg; break;
        case 'w': wl = optarg; break;
        case 't': nthreads = atoi(optarg); break;
        case 'n': total_pages = atoi(optarg); break;
        case 'j': read_ratio = atof(optarg); break;
        case 'z': g_zone_pages = atoi(optarg); break;
        case 'g': strncpy(tag, optarg, sizeof(tag) - 1); break;
        case 'H': g_hostdedup = 1; break;
        case 'L': g_max_live_zones = atoi(optarg); break;
        case 'v': g_verbose = 1; break;
        default: return 1;
        }
    }

    int fams = 12, versions = 36;
    double shared = 0.76, modf = 0.06;
    if (!strcmp(wl, "vm")) {
        fams = 6; versions = 40; shared = 0.65; modf = 0.15;
    } else if (!strcmp(wl, "backup")) {
        fams = 5; versions = 45; shared = 0.88; modf = 0.02;
    } else if (!strcmp(wl, "cloud")) {
        /* cloud primary-storage stream, calibrated to ~31% duplication
         * (matches lzsim/traces.py CLOUD_CFG). fams must stay <= MAX_FAMS(16):
         * we fold the 48 families into 8 with 240 versions each -- the
         * (shared, modf) duplication regime and g_ppv are unchanged, so
         * the dedup/reduction profile is the same. */
        fams = 8; versions = 240; shared = 0.25; modf = 0.15;
    } else if (!strcmp(wl, "rocksdb")) {
        fams = 1; versions = 1; shared = 0.0; modf = 0.0; read_ratio = 0.30;
    }
    if (!strcmp(wl, "fsl")) { fams = 1; versions = 1; read_ratio = 0.15; }
    g_ppv = total_pages > 0 ? total_pages / (fams * versions) : 1200;
    if (g_ppv < 32) g_ppv = 32;

    dev_fd = open(dev, O_RDWR);
    if (dev_fd < 0) { perror("open dev"); return 1; }
    if (g_hostdedup) hset = calloc(hset_cap, sizeof(uint64_t));

    fprintf(stderr, "liobench wl=%s fams=%d versions=%d ppv=%d zone_pages=%d "
            "threads=%d read_ratio=%.2f hostdedup=%d tag=%s\n",
            wl, fams, versions, g_ppv, g_zone_pages, nthreads, read_ratio,
            g_hostdedup, tag);

    size_t jobcap = (size_t)fams * versions * g_ppv * 2 + 65536;
    if (!strcmp(wl, "fsl")) {
        jobcap = 4200000 + 65536;
    }
    jobs = malloc(sizeof(jjob_t) * jobcap);

    pthread_t th[32];
    tstat_t sts[32];
    int iw;
    for (iw = 0; iw < nthreads; iw++) {
        hist_init(&sts[iw].w);
        hist_init(&sts[iw].r);
        sts[iw].nw = sts[iw].nr = sts[iw].nerr = 0;
        pthread_create(&th[iw], NULL, worker, &sts[iw]);
    }
    double t0 = now_us();

    if (!strcmp(wl, "fsl")) {
        FILE *f = fopen("/opt/fsl_fps.txt", "r");
        if (!f) { fprintf(stderr, "no /opt/fsl_fps.txt\n"); return 1; }
        char line[128];
        uint64_t cap2 = 4200000;
        fsl_fps = malloc(sizeof(uint64_t) * cap2);
        njobs = 0;
        uint64_t nlines = 0;
        while (fgets(line, sizeof(line), f) && njobs < cap2 - 64) {
            if (strlen(line) < 16) { continue; }
            uint64_t h = strtoull(line, NULL, 16);
            nlines++;
            /* Mirror lzsim/traces.py gen_fsl: each 8 KB chunk hash expands
             * to two correlated 4 KB pages (h, h^0x5DEECE66D), so the guest
             * replays the simulator's page-level duplicate structure. */
            fsl_fps[njobs] = h;
            jobs[njobs].op = 0; jobs[njobs].fam = -1;
            jobs[njobs].page_off = (int32_t)njobs; jobs[njobs].pool_sel = 2;
            njobs++;
            if (njobs < cap2 - 64) {
                fsl_fps[njobs] = h ^ 0x5DEECE66DULL;
                jobs[njobs].op = 0; jobs[njobs].fam = -1;
                jobs[njobs].page_off = (int32_t)njobs; jobs[njobs].pool_sel = 2;
                njobs++;
            }
            while ((xs64() % 100) < (uint64_t)(read_ratio * 100)
                   && njobs < cap2 - 64) {
                jobs[njobs].op = 1; jobs[njobs].fam = 0;
                jobs[njobs].page_off = 0; jobs[njobs].pool_sel = 0;
                njobs++;
            }
        }
        fclose(f);
        fprintf(stderr, "fsl replay: %lu pages from %lu chunk lines\n",
                (unsigned long)njobs, (unsigned long)nlines);
        jobs_publish();
    } else {
        for (i = 0; i < fams; i++) {
            pool_cur[i] = aligned_alloc(4096, (uint64_t)g_ppv * PAGE_SIZE);
            pool_prev[i] = aligned_alloc(4096, (uint64_t)g_ppv * PAGE_SIZE);
            int k;
            for (k = 0; k < g_ppv; k++) {
                fill_page((uint64_t)(i + 1) * 1000003 + k,
                          pool_cur[i] + (uint64_t)k * PAGE_SIZE);
            }
        }
        int *ptr = calloc(fams, sizeof(int));
        uint64_t counter = 1, emitted = 0;
        uint64_t total = (uint64_t)fams * versions * g_ppv;
        int blk = 512;
        while (emitted < total) {
            int f = (int)(xs64() % fams);
            if (ptr[f] >= versions * g_ppv) {
                int any = 0;
                for (iw = 0; iw < fams; iw++) {
                    if (ptr[iw] < versions * g_ppv) any = 1;
                }
                if (!any) break;
                continue;
            }
            int s = ptr[f];
            int ver_end = (ptr[f] / g_ppv + 1) * g_ppv;   /* version boundary */
            int e = s + blk;
            if (e > ver_end) e = ver_end;
            if (e > versions * g_ppv) e = versions * g_ppv;
            int lsm_dup = !strcmp(wl, "rocksdb");
            njobs = 0;
            for (; s < e; s++) {
                jobs[njobs].op = 0;
                jobs[njobs].fam = (int16_t)f;
                jobs[njobs].page_off = s % g_ppv;
                jobs[njobs].pool_sel = 0;
                njobs++;
                /* LSM duplicate mechanisms, mirroring lzsim/traces.py
                 * gen_rocksdb (same-value overwrite + compaction
                 * re-emission + rww reuse): re-write the content of an
                 * already written page.  p=0.189 gives a duplicate
                 * fraction p/(1+p)=15.9%, matching the simulator trace. */
                if (lsm_dup && s > 64 && (xs64() % 1000) < 189) {
                    jobs[njobs].op = 0;
                    jobs[njobs].fam = (int16_t)f;
                    jobs[njobs].page_off = (int32_t)(xs64() % (uint32_t)s);
                    jobs[njobs].pool_sel = 0;
                    njobs++;
                }
            }
            emitted += e - ptr[f];
            ptr[f] = e;
            jobs_publish();
            if (ptr[f] == ver_end && ptr[f] < versions * g_ppv) {
                vsrc_advance(f, shared, modf, &counter);
            }
            if ((xs64() & 3) == 0 && emitted > 4096) {
                int rf = (int)(xs64() % fams);
                int limit = ptr[rf] - g_ppv;
                if (limit > 64) {
                    int rs = (int)(xs64() % (uint32_t)limit);
                    int re = rs + 64;
                    if (re > limit) re = limit;
                    njobs = 0;
                    for (; rs < re; rs++) {
                        jobs[njobs].op = 0;
                        jobs[njobs].fam = (int16_t)rf;
                        jobs[njobs].page_off = rs % g_ppv;
                        jobs[njobs].pool_sel = 1;
                        njobs++;
                    }
                    jobs_publish();
                }
            }
            njobs = 0;
            uint32_t nr = (uint32_t)(read_ratio * blk / 4);
            for (uint32_t q = 0; q < nr && njobs < 1024; q++) {
                jobs[njobs].op = 1; jobs[njobs].fam = 0;
                jobs[njobs].page_off = 0; jobs[njobs].pool_sel = 0;
                njobs++;
            }
            if (njobs) jobs_publish();
        }
    }

    pthread_mutex_lock(&job_mu);
    gen_done = 1;
    pthread_cond_broadcast(&job_cv);
    pthread_mutex_unlock(&job_mu);
    for (iw = 0; iw < nthreads; iw++) pthread_join(th[iw], NULL);
    double el = (now_us() - t0) / 1e6;

    print_result(wl, tag, sts, nthreads, el);
    return 0;
}
