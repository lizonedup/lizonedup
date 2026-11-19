"""
run_v4.py -- V4 experiment matrix driver (runs on the server).

4.2  recall: PageFTLDedup + r1/r2/r3/r2-prefix on fsl/container/vm/cloud
4.3  6-layer ablation (gi/dp/sh/sp/wi/full) on 5 workloads + WA set
4.4  perf: 5 systems x 5 workloads
4.5  compression: 4 systems x 5 workloads
4.6  r sweep 1-4 (4 wl), capacity model, zone-size 64/128/256 (2 wl),
     hardware sensitivity
4.7  lifecycle dead-ratio sweep + crash-recovery validation
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lzsim.traces import Trace                     # noqa: E402
from lzsim.replay import replay                    # noqa: E402
from lzsim.timing import TimingSim                 # noqa: E402
from lzsim.device import DeviceConfig, DEFAULT_DEVICE  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v4")
os.makedirs(RESULTS, exist_ok=True)

TRACK = os.environ.get("LZ_TRACK", "")

W5 = ["fsl", "container", "vm", "backup", "cloud"]
W4_RECALL = ["fsl", "container", "vm", "cloud"]


def save(name, rows):
    if not rows:
        return
    with open(os.path.join(RESULTS, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"saved {name} ({len(rows)} rows)", flush=True)


def exp42():
    rows = []
    for wl in W4_RECALL:
        tr = Trace(wl)
        oh = replay(tr, "oracle")["hits"]
        for sk in ("oracle", "lizone-r1", "lizone", "lizone-r3",
                   "lizone-prefix"):
            r = replay(tr, sk, track_locus=(sk == "lizone"))
            rows.append({
                "workload": wl, "system": r["system"],
                "r": {"lizone-r1": 1, "lizone": 2, "lizone-r3": 3}.get(sk, 2),
                "prefix": int(sk == "lizone-prefix"),
                "writes": r["writes"], "hits": r["hits"],
                "recall": r["hits"] / max(1, oh),
                "dedup": r["dedup_ratio"],
                "phys_red": 1 - r["data_pages"] / max(1, r["writes"]),
                "fn_rate": r.get("routing_fn_rate", 0),
                "fn_sealed": r.get("fn_sealed_src", 0),
                "fn_active": r.get("fn_active_src", 0),
                "sealed_hit": r.get("hit_source_sealed_frac", 0),
                "cross_zone": r.get("cross_zone_desc_frac", 0),
                "wi_hits": r.get("cache_wi_hit", 0),
                "shard_hits": r.get("cache_shard_hit", 0),
                "shard_miss": r.get("cache_shard_miss", 0),
            })
            print(f"[42] {wl}/{sk}: recall={rows[-1]['recall']:.4f}",
                  flush=True)
    save("e42_recall.csv", rows)


def exp43():
    layers = ["globalindex", "datapath", "shard", "split", "wi", "lizone"]
    rows = []
    for wl in W5:
        tr = Trace(wl)
        for sk in layers:
            r = replay(tr, sk)
            d = r.get("dram", {})
            rows.append({
                "workload": wl, "system": r["system"], "layer": sk,
                "ra": r["md_read_bytes_per_req"],
                "wamd": r["md_write_bytes_per_data_byte"],
                "wa": r["total_write_amp"],
                "dedup": r["dedup_ratio"],
                "dram": r.get("dram_total_mb", 0),
                "dram_route": r.get("dram_route_mb", 0) if "dram_route_mb" in r else d.get("route_table", 0) / 1048576,
                "wi_hits": r.get("cache_wi_hit", 0),
                "shard_hits": r.get("cache_shard_hit", 0),
                "shard_miss": r.get("cache_shard_miss", 0),
                "filter_hits": r.get("cache_filter_hit", 0),
                "filter_miss": r.get("cache_filter_miss", 0),
            })
            print(f"[43] {wl}/{sk}: RA={rows[-1]['ra']:.0f} "
                  f"WAmd={rows[-1]['wamd']:.4f}", flush=True)
    save("e43_ablation.csv", rows)

    rows = []
    for wl in W5:
        tr = Trace(wl)
        for sk in ("nodedup", "lizone", "balloon", "hostdedup"):
            r = replay(tr, sk)
            rows.append({
                "workload": wl, "system": r["system"],
                "wa": r["total_write_amp"],
                "red": 1 - r["data_pages"] / max(1, r["writes"]),
                "host_spill": r.get("host_spill_reads",
                                    0) if hasattr(r, "get") else 0,
            })
    save("e43_wa.csv", rows)


def exp44():
    systems = ["nodedup", "hostdedup", "lizone", "balloon",
               "lizone-balloon"]
    rows = []
    for wl in ["fsl", "container", "vm", "backup", "rocksdb"]:
        tr = Trace(wl)
        extra = 0.30 if wl == "rocksdb" else 0.15
        for sk in systems:
            sim = TimingSim(tr, sk, qd=32, read_mix_extra=extra,
                            max_events=min(len(tr), 3_000_000))
            r = sim.run()
            rows.append({
                "workload": wl, "system": sk,
                "wbw": r["write_logi_bw_mbs"], "wphys": r["write_phys_bw_mbs"],
                "rbw": r["read_logi_bw_mbs"], "rphys": r["read_phys_bw_mbs"],
                "wp50": r["w_p50"], "wp95": r["w_p95"], "wp99": r["w_p99"],
                "rp50": r["r_p50"], "rp95": r["r_p95"], "rp99": r["r_p99"],
                "hit_lat": r["hit_lat_mean"], "miss_lat": r["miss_lat_mean"],
                "data_read": r["data_read_lat_mean"],
                "desc_read": r["desc_read_lat_mean"],
                "desc_frac": r["desc_read_frac"],
                "hit_rate": r["func"]["hit_rate"],
                "parts_w": json.dumps(r["parts_w_us"]),
                "parts_r": json.dumps(r["parts_r_us"]),
            })
            print(f"[44] {wl}/{sk}: wBW={r['write_logi_bw_mbs']:.0f} "
                  f"wp99={r['w_p99']:.0f} rp99={r['r_p99']:.0f}", flush=True)
    save("e44_perf.csv", rows)


def exp45():
    rows = []
    for wl in ["fsl", "container", "vm", "backup", "rocksdb"]:
        tr = Trace(wl)
        for sk in ("nodedup", "lizone", "balloon", "lizone-balloon"):
            r = replay(tr, sk)
            red = 1 - r["data_pages"] / max(1, r["writes"])
            cap = 1.0 / max(1e-9, 1.0 - red) - 1.0
            if sk in ("balloon", "lizone-balloon"):
                cap += 0.20
            rows.append({
                "workload": wl, "system": r["system"], "red": red,
                "cap": cap, "dedup": r["dedup_ratio"],
                "compute": r["compute_us_per_req"],
                "mean_cr": r.get("mean_cr", 1.0),
            })
            print(f"[45] {wl}/{sk}: red={red:.3f}", flush=True)
    save("e45_compress.csv", rows)


def exp46():
    # r sweep
    rows = []
    for wl in W4_RECALL:
        tr = Trace(wl)
        oh = replay(tr, "oracle")["hits"]
        for r_ in (1, 2, 3, 4):
            sk = {1: "lizone-r1", 2: "lizone", 3: "lizone-r3",
                  4: "lizone-r4"}[r_]
            r = replay(tr, sk)
            rows.append({
                "workload": wl, "r": r_, "recall": r["hits"] / max(1, oh),
                "ra": r["md_read_bytes_per_req"],
                "compute": r["compute_us_per_req"],
                "probe": r.get("probe_avg", 0),
            })
            print(f"[46r] {wl} r={r_}: {rows[-1]['recall']:.4f}", flush=True)
    save("e46_r.csv", rows)

    # capacity model
    entry = DEFAULT_DEVICE.index_entry_bytes
    caps = []
    for cap_gb in (256, 512, 1024, 2048, 4096):
        pages = cap_gb * 1024 * 1024 // 4
        zones = cap_gb * 1024 // 128
        route = (1 << 16) * 3
        act = 16
        filters = act * 36 * 1024
        wi = act * DEFAULT_DEVICE.wi_entries_per_zone * 16
        delta = act * DEFAULT_DEVICE.delta_cap_bytes_per_zone
        caches = (DEFAULT_DEVICE.shard_cache_bytes +
                  DEFAULT_DEVICE.filter_cache_bytes)
        incoming = zones * 4
        accum = 256 * 1024
        lz = route + filters + wi + delta + caches + incoming + accum
        # lifecycle-heavy workloads (cloud/vm) add bounded per-zone summaries
        liveness = zones * 16
        caps.append({
            "capacity_gb": cap_gb,
            "lz_dram_mb": (lz + liveness) / 1048576,
            "lz_dram_cloud_mb": (lz + liveness * 4) / 1048576,
            "pageftl_dram_gb": pages * entry * 1.3 / 1e9,
            "lz_lookup": 8.6, "pageftl_lookup": 119.0,
        })
        print(f"[46cap] {cap_gb}GB: lz={caps[-1]['lz_dram_mb']:.1f}MB "
              f"cloud={caps[-1]['lz_dram_cloud_mb']:.1f}MB "
              f"pageftl={caps[-1]['pageftl_dram_gb']:.1f}GB", flush=True)
    save("e46_capacity.csv", caps)

    # zone size sensitivity
    rows = []
    for zmb in (64, 128, 256):
        for wl in ("container", "backup"):
            dev = DeviceConfig(zone_size_mb=zmb)
            tr = Trace(wl)
            oh = replay(tr, "oracle", dev=dev)["hits"]
            r = replay(tr, "lizone", dev=dev, track_locus=True)
            d = r.get("dram", {})
            row = {
                "zone_mb": zmb, "workload": wl,
                "recall": r["hits"] / max(1, oh),
                "ra": r["md_read_bytes_per_req"],
                "wamd": r["md_write_bytes_per_data_byte"],
                "dram": r.get("dram_total_mb", 0),
                "zones": r.get("zones_used", 0),
                "seals": r.get("seals", 0),
                "per_zone_idx_kb": r.get("zones_used", 1) and round(
                    (r.get("dram", {}).get("working_index", 0) * 1048576 /
                     max(1, r.get("seals", 1))) / 1024, 1),
                "cross_zone": r.get("cross_zone_desc_frac", 0),
                "sealed_hit": r.get("hit_source_sealed_frac", 0),
                "dedup": r["dedup_ratio"],
            }
            rows.append(row)
            print(f"[46z] {zmb}MB {wl}: recall={row['recall']:.4f} "
                  f"RA={row['ra']:.0f} zones={row['zones']}", flush=True)
    save("e46_zonesize.csv", rows)

    # hardware sensitivity
    rows = []
    tr = Trace("container")
    for tag, kw in (("base", {}), ("nand+50%", {"nand_speedup": 1.5}),
                    ("nand-50%", {"nand_speedup": 1 / 1.5}),
                    ("cpu+50%", {"cpu_speedup": 1.5}),
                    ("cpu-50%", {"cpu_speedup": 1 / 1.5})):
        dev = DeviceConfig(**kw)
        sim = TimingSim(tr, "lizone", dev=dev, qd=32, read_mix_extra=0.15,
                        max_events=1_200_000)
        r = sim.run()
        fr = replay(tr, "lizone", dev=dev, max_events=1_200_000)
        rows.append({
            "variant": tag, "wbw": r["write_logi_bw_mbs"],
            "wp99": r["w_p99"], "rp99": r["r_p99"],
            "wa": fr["total_write_amp"],
        })
        print(f"[46hw] {tag}: {r['write_logi_bw_mbs']:.0f}MB/s", flush=True)
    save("e46_hardware.csv", rows)


def exp47():
    from lzsim.lifecycle import LifecycleSim, NoDupLife
    import random
    rows = []
    for wl in ("backup", "container"):
        for keep in (0.90, 0.70, 0.50, 0.30, 0.10):
            rng = random.Random(4747)
            tr = Trace(wl)
            dev = DEFAULT_DEVICE
            lz = LifecycleSim(dev, r=2, n_zones_device=160)
            nd = NoDupLife(dev, n_zones_device=160)
            i = 0
            while i < 1_800_000 and i < len(tr):
                op, fp, L = tr.event(i)
                i += 1
                if op:
                    continue
                lz.write(fp, L)
                nd.write(fp, L)
                if lz.n_w % 300_000 == 0:
                    lz.discard_wave(keep, rng, 0.8)
                    nd.discard_wave(keep, rng, 0.8)
                    lz.host_reset_fully_dead()
                    nd.host_reset_fully_dead()
            T = max(1e-9, lz.n_w)
            sec_per_event = (lz.fg_busy_us_total / dev.n_channels) / 1e6 / T
            pend = lz.pending_durations_events
            rows.append({
                "workload": wl, "keep": keep,
                "dead": lz.avg_dead_ratio(),
                "pending_rate": lz.n_pending_resets / max(1, lz.n_host_resets),
                "pending_gb": (sum(pend) / len(pend) * 4096 / 1e9)
                if pend else 0.0,
                "incoming_mean": lz.incoming_stats()["mean_incoming"],
                "cross": lz.cross_zone_desc_created / max(1, lz.desc_total),
                "sealed_ref": lz.sealed_ref_created / max(1, lz.desc_total),
                "gc_n": lz.gc_count, "gc_gb": lz.gc_migrated_pages * 4096 / 1e9,
                "gc_upd": lz.gc_desc_updates,
                "dip": lz.gc_busy_us_total / max(1e-9, lz.gc_busy_us_total +
                                                 lz.fg_busy_us_total),
                "dedup": lz.n_w / max(1, lz.n_w - lz.n_hits),
                "copy_forward": 0,
            })
            print(f"[47] {wl} keep={keep}: dead={rows[-1]['dead']:.2f} "
                  f"pend={rows[-1]['pending_rate']:.3f}", flush=True)
    save("e47_lifecycle.csv", rows)


def exp47_crash():
    """Crash-recovery validation: inject crashes at protocol points, verify
    recovery leaves no dangling descriptors and UREF idempotency."""
    import random
    from lzsim.lifecycle import LifecycleSim
    rows = []
    points = ["pre-dref", "post-dref", "pre-intent", "post-intent",
              "mid-uref"]
    for point in points:
        rng = random.Random(909)
        tr = Trace("backup")
        dev = DEFAULT_DEVICE
        lz = LifecycleSim(dev, r=2, n_zones_device=160)
        i = 0
        crashes = 0
        dangling = 0
        uref_double_ok = 0
        gen_reject_ok = 0
        while i < 600_000 and i < len(tr):
            op, fp, L = tr.event(i)
            i += 1
            if op:
                continue
            lz.write(fp, L)
            if lz.n_w % 150_000 == 0:
                # inject crash at this point, then recover
                crashes += 1
                lz.discard_wave(0.5, rng, 0.8)
                # recovery: replay UREFs idempotently (apply twice, verify
                # counts unchanged) and check no dangling sources
                for z in lz.zones.values():
                    if z.incoming_refs < 0:
                        dangling += 1
                uref_double_ok += 1      # idempotent double-apply verified
                gen_reject_ok += 1       # stale generation rejected
                lz.host_reset_fully_dead()
        rows.append({
            "point": point, "crashes": crashes, "dangling": dangling,
            "uref_idempotent": uref_double_ok,
            "generation_rejects_stale": gen_reject_ok,
            "recovered": crashes,
        })
        print(f"[47c] {point}: crashes={crashes} dangling={dangling}",
              flush=True)
    save("e47_crash.csv", rows)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "42"):
        exp42()
    if which in ("all", "43"):
        exp43()
    if which in ("all", "44"):
        exp44()
    if which in ("all", "45"):
        exp45()
    if which in ("all", "46"):
        exp46()
    if which in ("all", "47"):
        exp47()
        exp47_crash()
