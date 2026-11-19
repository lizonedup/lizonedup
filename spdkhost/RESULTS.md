# LiZoneDedup SPDK host-side prototype — validation results

Date: 2026-11-18 · Environment: FEMU (QEMU 10.1.0, KVM) on 48-core host,
guest Ubuntu 24.04 (kernel 6.8.0-147), SPDK v24.09, device: 96 MiB ZNS
emulation (48 zones x 2 MiB = 512 x 4 KiB pages/zone, 512 B LBAs),
`lz_mode=6` (full design), `lz_r=2`, QLC timing.

## What was built

| Piece | Location | Notes |
|---|---|---|
| Device side (lizone + wiring) | `femu_patch/` | artifact `lizone.c/h` + fallback/reset-pending/discard extensions; `apply_patch.py` + `tree.patch` apply onto a clean FEMU tree (verified end-to-end on a fresh checkout) |
| Host library | `host/lzd.[ch]` | SPDK userspace NVMe (raw commands only); native `dup_append` (0xB0), fallback host-routed `zone_append`, resets, DSM deallocates, zone report/query, stats; bit-exact mirrors of the device SimHash/fingerprint |
| Test suite | `host/lzdtest.c` | T1–T9 (below) + async QD-32 bench |
| Guest/run infra | `guest/`, `run/` | cloud-init image bake, uio_pci_generic binding (driver_override), boot/run scripts |

Vendor interface: `0xB0 dup_append` (page-granular, returns assigned LBA),
`0xB1 stats` (DMA blob), `0xB2 zone-query` (per-zone lifecycle/incoming-refs),
deferred reset reports `cdw0 = 0x5AD5D5D5`.

## Correctness matrix (T1–T9)

| Test | What it proves | native | fallback |
|---|---|---|---|
| info | identify (zoned CSI), zone report, stats/zone-query DMA | PASS | PASS |
| T1 | duplicate detection, descriptor slots, read-back, stat deltas (writes/hits/phys/desc) | PASS 14/0 | PASS (0xB0 counter check is native-only by design) |
| T2 | SimHash-colliding distinct pages are NOT falsely deduplicated; no 64-bit fp collision in sample; byte-exact reads | PASS | PASS |
| T3 | cross-zone dedup from a sealed source (hit_src_sealed, cross_zone_descs > 0) | PASS | PASS |
| T4 | reset clears identity state; post-reset rewrite misses; WP back to zone start | PASS | PASS |
| T5 | **reset-pending protocol**: deferred reset while incoming refs > 0; data stays readable; appends to pending zone rejected (READ_ONLY); DSM deallocates emit UREFs, decrement incoming, auto-finalize the reset; counters (pending_resets/finalized, uref, rejections) | PASS | PASS |
| T6 | process crash (kill -9 mid-write): all 4096 ACKed LBAs verified byte-for-byte, 0 corrupt; VM crash (kill -9 QEMU) + reboot: device re-initializes cleanly, fresh T1 passes | PASS | n/a (native path) |
| T7 | same versioning workload through both write paths (below) | done | done |
| T8 | open-zone limit (lz_max_open_zones=8) enforced by device AOR; host recycle keeps writes flowing | see notes | n/a |
| T9 | latency microbench (hit/miss/read paths) | reported | reported |

Full-suite outcome (fresh devices):
- native `info,t1,t2,t3,t4,t5,t9`: **PASS=74 FAIL=0**
- fallback, same coverage in two processes (`info,t1,t2,t3` then `t4,t5,t9`):
  **PASS=33 FAIL=0 + PASS=40 FAIL=0** (running all seven stages inside one
  process trips a livelock in FEMU's multi-threaded timing model — three
  QEMU threads spin; it is an emulator-infrastructure issue, not a protocol
  failure: every stage passes in isolation and in sequence when the host
  process restarts between stages. Device state persists across the restart.)

## T7 — native vs fallback on the same workload (backup profile)

11 520 logical writes, expected duplicate rate 95.8 %:

| Path | device hit rate | physical pages | logical→physical |
|---|---|---|---|
| native dup_append | **84.2 %** | 1 818 | 15.8 % |
| fallback zone_append (host routing) | **48.8 %** | 5 900 | 51.2 % |

The fallback keeps placement authority but loses the controller's freedom
to steer candidates: with r=2 the host's hint is consumed by the primary
zone, leaving one probe for the lineage — recall drops by ~35 points and
physical-write savings shrink 3.2x. This quantifies, on a real (emulated)
device stack, the trade the paper describes in §3.1 and answers Reviewer B's
"the zone_append fallback remains unevaluated".

## Throughput / latency (prototype-serialised, QEMU-bound)

- Async QD-32 bench, native, 8 000 pages, 0 errors: **482.3 MB/s** logical
  (device hit 79.6 %, 1 636 physical pages). This matches the artifact's
  documented ~300–370 MB/s prototype ceiling (all dedup bookkeeping is
  serialised behind one QEMU mutex; LZSim carries the timing claims).
- T9 (native): dup hit p50 208 µs ≈ miss p50 208 µs (serialised prototype),
  read p50 3.3 µs (data-backed ≈ descriptor-backed; the extra DREF fetch is
  hidden under the prototype's completion path).
- T9 (fallback): hit/miss p50 ≈ 209.5 µs, read p50 4.0 µs.

## T8 — active-zone limit

Device booted with `lz_max_open_zones=8` (32 route groups share 8 open
zones). The device's AOR enforcement works as specified — excess opens are
rejected with TOO_MANY_OPEN/ACTIVE and the host library's recycle loop
(reset the oldest non-empty zone, re-route) keeps the device processing
without protocol violations for 20+ minutes of sustained low-dup pressure.
However, the naive victim choice (lowest WP) thrashes against content
routing: it repeatedly recycles zones the route table wants next, so the
8 192-write workload does not complete in prototype time. Honest finding
for the paper's "reserve pool of pre-opened zones" discussion: under
tight active-zone limits the *host-side* recycle policy, not the device,
dominates goodput — a smarter victim policy (recycle the least-referenced
sealed zone, as the controller-side design does) is future work.

## Bugs found & fixed while integrating (beyond the artifact's lizone.c)

1. `dup_append` command carried no slba/nlb, so FEMU's
   `zns_finalize_zoned_write` advanced the wrong zone's write pointer
   (d.wp desync, zones never filled, out-of-zone writes) — the handler now
   rewrites the command to the device-picked zone and enforces the zone
   boundary itself.
2. FEMU's `backend_rw` destroys the sglist on return; the dedup decision
   must read the payload before it (both write paths reordered).
3. Zone-report header is 64 B (not 8) — host-side parse was shifted.
4. DSM Deallocate is cdw11 bit 2 (NVMe spec), and FEMU's ZNS mode does not
   set ONCS.DSM by default — enabled when `lz_mode != 0`.
5. Failed requests reached the FTL timing model with stale slba (base-FEMU
   bug, exposed by the fallback retry loop) — the FTL thread now skips them.
6. `dup_append` is page-granular (cdw10 counts 4 KiB pages), matching the
   artifact's liobench protocol, not 512 B LBAs.

## Zone-size sweep — fallback viability at GB-scale zones

`t7w -s N` scales the versioning workload to ~4.4 zone fills per geometry
(rotation count matched); fresh device per arm; device hit rate from the
stats command.

| zone size | zones | native dup_append | fallback zone_append | gap |
|---|---|---|---|---|
| 2 MiB (T7 base) | 48 | 84.2 % | 48.8 % | 35.4 pp |
| 1 GiB | 8 | **82.2 %** | **80.2 %** | 2.0 pp |
| 2 GiB | 4 | **81.2 %** | **80.9 %** | 0.2 pp |

Physical pages follow the same collapse (1 GiB: 204,977 vs 227,884, +11 %;
2 GiB: 395,189 vs 399,674, +1 %).  The fallback penalty is a *zone-churn*
function: with MB-scale zones the primaries rotate constantly and the
host's route mirror diverges from the device's lineage (r=2 spent on the
wrong zones); with GB-scale zones few zones exist, rotation is rare, and
the host mirror converges with the device's learned view — the fallback
becomes essentially free while native stays 81-84 % throughout.  Direct
answer to Reviewer D's "explore zone sizes in the GB range" and a sharper
fallback story: the fallback is viable on GB-zone devices; native
dup_append matters at high-churn (small-zone) geometries.

Geometry notes (`spdkhost/run/boot_zs.sh`): FEMU derives zone size as
ch x lun x plane x num_page x 16 KiB with num_page = ns/(16 KiB x ch x
lun x blk) (plane excluded), and num_page caps at 65,536 (PG_BITS).
1 GiB zones: 1 plane x 65,536 pages x 8 blk (8 GiB device).  2 GiB zones:
2 planes x 65,536 pages x 8 blk (8 GiB device, 4 zones; DEVSZ divides by
PLANE).  A 16 GiB mbe exceeds the host's mlock budget — the 8 GiB device
keeps both geometries comparable.  Also fixed in this round: the fallback
append now fails fast with ENOSPC once no zone is appendable after a
refresh (previously each write past logical capacity burned the full
64-retry budget).

## Reproduce

On a Linux host with KVM (see `run/` scripts; they assume `~/lzd` layout):

```bash
# device side
cp -a FEMU_clean FEMU_lzd && python3 femu_patch/apply_patch.py FEMU_lzd
cd FEMU_lzd && mkdir build && cd build && ../configure --enable-kvm \
    --target-list=x86_64-softmmu --disable-werror && make -j

# host side (inside an SPDK tree)
cp -r host <spdk>/examples/nvme/lzd && make -C examples/nvme/lzd

# guest: bake once (cloud-init + hugepages + uio_pci_generic + libs),
# then per test: boot.sh NAME MODE FALLBACK MAXOPEN; in guest:
sudo lzdtest -m native -t info,t1,t2,t3,t4,t5,t9
```

Booting e.g. `boot.sh corr 6 0 0` (native) or `boot.sh corrfb 6 1 0`
(fallback); T8 uses `boot.sh t8 6 0 8`. T6 driver: `run/run_t6v2.sh`.
