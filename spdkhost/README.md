# spdkhost — LiZoneDedup SPDK host-side prototype

Answers the FAST'27 reviews (A, B, E): a **host-side prototype over SPDK**
talking to the LiZoneDedup FEMU device, validating the design end-to-end —
dup_append semantics, the zone_append fallback, the deferred (reset-pending)
protocol, DREF durability across crashes, and zone limits — on a real
(userspace-driver) I/O stack rather than simulation only.

**Read [`RESULTS.md`](RESULTS.md) for the validation matrix and numbers.**

## Layout

```
femu_patch/  device side: lizone.c/h (artifact + spdkhost extensions),
             apply_patch.py + tree.patch (verified on a clean FEMU tree)
host/        lzd.[ch] (SPDK library), lzdtest.c (T1-T9 + bench), Makefile
guest/       cloud-init user-data, uio_pci_generic bind helper
run/         boot/bake/test drivers used on the gpu build host
```

## Vendor interface (FEMU ZNS + lizone)

| Opcode | Name | Semantics |
|---|---|---|
| 0xB0 | dup_append | page-granular (cdw10 = nr 4 KiB pages − 1); controller routes (C1), looks up, returns the assigned LBA in cdw0 |
| 0xB1 | stats | optional dptr: DMA of `LZStatsBlob` (magic `LZTP`) |
| 0xB2 | zone-query | cdw10 = zid; DMA of `LZZoneInfo` (lz state, ZNS state, incoming refs, WP) |
| reset | zone mgmt 0x04 | if incoming refs > 0: completes with `cdw0 = 0x5AD5D5D5` (deferred); data stays readable, appends rejected; DSM Deallocate releases refs (UREF) and auto-finalizes |
| DSM 0x09 | dealloc | cdw11 bit 2 (AD); deletes descriptors, decrements incoming refs |

Fallback mode (`lz_fallback=1`): the host computes the SimHash route digest,
mirrors the C1 route table, and issues standard `zone_append` to its chosen
zone; the device still performs the dedup lookup with the host zone as the
first-probed hint and learns the route from where the append lands.

## Test suite

```
info  identify/report/stats/query smoke
t1    duplicate detection + descriptor reads + stat deltas
t2    adversarial: SimHash-colliding distinct pages (no false dedup)
t3    cross-zone dedup from a sealed source
t4    reset clears identity state
t5    reset-pending: defer, reads stay valid, appends rejected,
      discard(UREF) -> incoming drops to 0 -> physical reset finalizes
t6p1/t6p2  crash journal: ACKed (lba,seed) verified after kill -9
t7w   versioning workload, native vs fallback hit-rate comparison
t8    open-zone-limit pressure (boot device with lz_max_open_zones=8)
t9    latency microbench (hit / miss / data-read / desc-read)
bench async QD-N throughput run
```

Everything is driven through raw SPDK commands
(`spdk_nvme_ctrlr_cmd_io_raw/_admin_raw`), so no SPDK-internal ZNS handling
is assumed of the device.
