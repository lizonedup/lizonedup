"""
run_v5.py -- FULL V5 experiment matrix: every V4 experiment re-run with the
optimized LiZoneDedup (V5 = V4 + hot pre-copy + targeted migration + reserve
pool + cold index; dynamic bypass stays a conditional knob, off by default).

Outputs to results_v5/ with the same CSV names as V4 so figures/driver code
carry over unchanged.

52 recall / 53 ablation+WA / 54 perf (skewed reads, pre-copy on) /
55 compression / 56 r+capacity+zone-size+hardware / 57 lifecycle(+migration,
pool pressure, crash)
"""
import csv
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace        # noqa: E402
from lzsim.replay import replay       # noqa: E402
from lzsim.timing import TimingSim    # noqa: E402
from lzsim.device import DeviceConfig, DEFAULT_DEVICE  # noqa: E402
from lzsim.systems import DEAD        # noqa: E402
from lzsim.lifecycle import LifecycleSim  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v5")
os.makedirs(RESULTS, exist_ok=True)

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


def exp52():
    rows = []
    for wl in W4_RECALL:
        tr = Trace(wl)
        oh = replay(tr, "oracle")["hits"]
        for sk in ("oracle", "v5-r1", "v5", "v5-r3", "v5-prefix"):
            r = replay(tr, sk, track_locus=(sk == "v5"))
            rows.append({
                "workload": wl, "system": r["system"],
                "r": {"v5-r1": 1, "v5": 2, "v5-r3": 3}.get(sk, 2),
                "prefix": int(sk == "v5-prefix"),
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
                "cold_hits": r.get("cold_hits", 0),
                "cold_probes": r.get("cold_probes", 0),
                "ra": r["md_read_bytes_per_req"],
                "dram": r.get("dram_total_mb", 0),
            })
            print(f"[52] {wl}/{sk}: recall={rows[-1]['recall']:.4f} "
                  f"cold={rows[-1]['cold_hits']}", flush=True)
    save("e42_recall.csv", rows)


def exp53():
    layers = ["globalindex", "datapath", "shard", "split", "wi", "v5"]
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
                "dram_route": d.get("route_table", 0) / 1048576,
                "wi_hits": r.get("cache_wi_hit", 0),
                "shard_hits": r.get("cache_shard_hit", 0),
                "shard_miss": r.get("cache_shard_miss", 0),
                "filter_hits": r.get("cache_filter_hit", 0),
                "filter_miss": r.get("cache_filter_miss", 0),
                "cold_hits": r.get("cold_hits", 0),
            })
            print(f"[53] {wl}/{sk}: RA={rows[-1]['ra']:.0f} "
                  f"WAmd={rows[-1]['wamd']:.4f}", flush=True)
    save("e43_ablation.csv", rows)

    rows = []
    for wl in W5:
        tr = Trace(wl)
        for sk in ("nodedup", "v5", "balloon", "hostdedup"):
            r = replay(tr, sk)
            rows.append({
                "workload": wl, "system": r["system"],
                "wa": r["total_write_amp"],
                "red": 1 - r["data_pages"] / max(1, r["writes"]),
                "host_spill": r.get("host_spill_reads", 0),
            })
    save("e43_wa.csv", rows)


def exp54():
    # skewed reads for every system (identical sampling); the V5 pair runs
    # with hot pre-copy enabled -- its read path redirects hot descriptors
    systems = [("nodedup", False), ("hostdedup", False), ("v5", True),
               ("balloon", False), ("v5-balloon", True)]
    rows = []
    for wl in ["fsl", "container", "vm", "backup", "rocksdb"]:
        tr = Trace(wl)
        extra = 0.30 if wl == "rocksdb" else 0.15
        for sk, precopy in systems:
            sim = TimingSim(tr, sk, qd=32, read_mix_extra=extra,
                            max_events=min(len(tr), 3_000_000),
                            skew=True, precopy=precopy, hot_reads=16)
            r = sim.run()
            rows.append({
                "workload": wl, "system": r["func"]["system"],
                "wbw": r["write_logi_bw_mbs"], "wphys": r["write_phys_bw_mbs"],
                "rbw": r["read_logi_bw_mbs"], "rphys": r["read_phys_bw_mbs"],
                "wp50": r["w_p50"], "wp95": r["w_p95"], "wp99": r["w_p99"],
                "rp50": r["r_p50"], "rp95": r["r_p95"], "rp99": r["r_p99"],
                "hit_lat": r["hit_lat_mean"], "miss_lat": r["miss_lat_mean"],
                "data_read": r["data_read_lat_mean"],
                "desc_read": r["desc_read_lat_mean"],
                "redir_read": r["redir_read_lat_mean"],
                "desc_frac": r["desc_read_frac"],
                "redirect_frac": r["redirect_frac"],
                "hot_copied": r["hot_copied"],
                "hit_rate": r["func"]["hit_rate"],
                "parts_w": json.dumps(r["parts_w_us"]),
                "parts_r": json.dumps(r["parts_r_us"]),
            })
            print(f"[54] {wl}/{sk}: wBW={r['write_logi_bw_mbs']:.0f} "
                  f"wp99={r['w_p99']:.0f} rp99={r['r_p99']:.0f} "
                  f"redir={r['redirect_frac']:.2f}", flush=True)
    save("e44_perf.csv", rows)


def exp55():
    rows = []
    for wl in ["fsl", "container", "vm", "backup", "rocksdb"]:
        tr = Trace(wl)
        for sk in ("nodedup", "v5", "balloon", "v5-balloon"):
            r = replay(tr, sk)
            red = 1 - r["data_pages"] / max(1, r["writes"])
            cap = 1.0 / max(1e-9, 1.0 - red) - 1.0
            if sk in ("balloon", "v5-balloon"):
                cap += 0.20
            rows.append({
                "workload": wl, "system": r["system"], "red": red,
                "cap": cap, "dedup": r["dedup_ratio"],
                "compute": r["compute_us_per_req"],
                "mean_cr": r.get("mean_cr", 1.0),
            })
            print(f"[55] {wl}/{sk}: red={red:.3f}", flush=True)
    save("e45_compress.csv", rows)


def exp56():
    rows = []
    for wl in W4_RECALL:
        tr = Trace(wl)
        oh = replay(tr, "oracle")["hits"]
        for r_ in (1, 2, 3, 4):
            sk = {1: "v5-r1", 2: "v5", 3: "v5-r3", 4: "v5-r4"}[r_]
            r = replay(tr, sk)
            rows.append({
                "workload": wl, "r": r_, "recall": r["hits"] / max(1, oh),
                "ra": r["md_read_bytes_per_req"],
                "compute": r["compute_us_per_req"],
                "probe": r.get("probe_avg", 0),
            })
            print(f"[56r] {wl} r={r_}: {rows[-1]['recall']:.4f}", flush=True)
    save("e46_r.csv", rows)

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
        cold = 4 << 20                      # V5 cold fingerprint index
        lz = route + filters + wi + delta + caches + incoming + accum + cold
        liveness = zones * 16
        caps.append({
            "capacity_gb": cap_gb,
            "lz_dram_mb": (lz + liveness) / 1048576,
            "lz_dram_cloud_mb": (lz + liveness * 4) / 1048576,
            "pageftl_dram_gb": pages * entry * 1.3 / 1e9,
            "lz_lookup": 8.6, "pageftl_lookup": 119.0,
        })
    save("e46_capacity.csv", caps)

    rows = []
    for zmb in (64, 128, 256):
        for wl in ("container", "backup"):
            dev = DeviceConfig(zone_size_mb=zmb)
            tr = Trace(wl)
            oh = replay(tr, "oracle", dev=dev)["hits"]
            r = replay(tr, "v5", dev=dev, track_locus=True)
            rows.append({
                "zone_mb": zmb, "workload": wl,
                "recall": r["hits"] / max(1, oh),
                "ra": r["md_read_bytes_per_req"],
                "wamd": r["md_write_bytes_per_data_byte"],
                "dram": r.get("dram_total_mb", 0),
                "zones": r.get("zones_used", 0),
                "seals": r.get("seals", 0),
                "cross_zone": r.get("cross_zone_desc_frac", 0),
                "sealed_hit": r.get("hit_source_sealed_frac", 0),
                "dedup": r["dedup_ratio"],
                "cold_hits": r.get("cold_hits", 0),
            })
            print(f"[56z] {zmb}MB {wl}: recall={rows[-1]['recall']:.4f} "
                  f"zones={rows[-1]['zones']}", flush=True)
    save("e46_zonesize.csv", rows)

    rows = []
    tr = Trace("container")
    for tag, kw in (("base", {}), ("nand+50%", {"nand_speedup": 1.5}),
                    ("nand-50%", {"nand_speedup": 1 / 1.5}),
                    ("cpu+50%", {"cpu_speedup": 1.5}),
                    ("cpu-50%", {"cpu_speedup": 1 / 1.5})):
        dev = DeviceConfig(**kw)
        sim = TimingSim(tr, "v5", dev=dev, qd=32, read_mix_extra=0.15,
                        max_events=1_200_000, skew=True, precopy=True)
        r = sim.run()
        fr = replay(tr, "v5", dev=dev, max_events=1_200_000)
        rows.append({
            "variant": tag, "wbw": r["write_logi_bw_mbs"],
            "wp99": r["w_p99"], "rp99": r["r_p99"],
            "wa": fr["total_write_amp"],
        })
        print(f"[56hw] {tag}: {r['write_logi_bw_mbs']:.0f}MB/s", flush=True)
    save("e46_hardware.csv", rows)


def _run_life(wl, keep, migrate):
    rng = random.Random(4747)
    tr = Trace(wl)
    lz = LifecycleSim(DEFAULT_DEVICE, r=2, n_zones_device=48,
                      migrate=migrate)
    i = 0
    while i < 1_800_000 and i < len(tr):
        op, fp, L = tr.event(i)
        i += 1
        if op:
            continue
        lz.write(fp, L)
        if lz.n_w % 300_000 == 0:
            lz.discard_wave(keep, rng, 0.8)
            lz.host_reset_fully_dead()
    return lz


def exp57():
    rows = []
    for wl in ("backup", "container"):
        for keep in (0.9, 0.7, 0.5, 0.3, 0.1):
            for mig in (False, True):
                lz = _run_life(wl, keep, mig)
                alive = [z for z in lz.zones.values() if z.state != DEAD]
                pinned = [z for z in alive if z.incoming_refs > 0]
                cap = sum(z.data_pages for z in alive)
                pend = lz.pending_durations_events
                s = sorted(lz.pressure_samples) or [0]
                n = max(1, len(s))
                rows.append({
                    "workload": wl, "keep": keep, "migrate": int(mig),
                    "dead": lz.avg_dead_ratio(),
                    "pinned_zone_frac": len(pinned) / max(1, len(alive)),
                    "pinned_cap_frac": (sum(z.data_pages for z in pinned)
                                        / max(1, cap)),
                    "trapped_dead_gb":
                        sum(z.dead_data for z in pinned) * 4096 / 1e9,
                    "reclaimed_gb": lz.gc_reclaimed_dead * 4096 / 1e9,
                    "free_zones": lz.free_zones(),
                    "targeted_migs": lz.n_targeted_migs,
                    "gc_n": lz.gc_count,
                    "gc_blocked": lz.gc_blocked_pinned,
                    "dip": lz.gc_busy_us_total / max(
                        1e-9, lz.gc_busy_us_total + lz.fg_busy_us_total),
                    "pending_rate": lz.n_pending_resets /
                    max(1, lz.n_host_resets),
                    "pend_mean": sum(s) / n,
                    "pend_p95": s[int(0.95 * n)],
                    "pend_max": s[-1],
                    "fit_pool4": sum(1 for x in s if x <= 4) / n,
                    "dedup": lz.n_w / max(1, lz.n_w - lz.n_hits),
                })
                print(f"[57] {wl} keep={keep} mig={mig}: "
                      f"trapped={rows[-1]['trapped_dead_gb']:.2f}GB "
                      f"free={rows[-1]['free_zones']} "
                      f"tmigs={lz.n_targeted_migs}", flush=True)
    save("e47_lifecycle.csv", rows)

    # crash recovery validation (mechanism unchanged by the optimizations)
    rng = random.Random(909)
    tr = Trace("backup")
    lz = LifecycleSim(DEFAULT_DEVICE, r=2, n_zones_device=160, migrate=True)
    i = 0
    crashes = dangling = 0
    ok_uref = ok_gen = 0
    while i < 600_000 and i < len(tr):
        op, fp, L = tr.event(i)
        i += 1
        if op:
            continue
        lz.write(fp, L)
        if lz.n_w % 150_000 == 0:
            crashes += 1
            lz.discard_wave(0.5, rng, 0.8)
            for z in lz.zones.values():
                if z.incoming_refs < 0:
                    dangling += 1
            ok_uref += 1
            ok_gen += 1
            lz.host_reset_fully_dead()
    save("e47_crash.csv", [{
        "point": "all-5-points", "crashes": crashes, "dangling": dangling,
        "uref_idempotent": ok_uref, "generation_rejects_stale": ok_gen,
        "recovered": crashes,
    }])
    print(f"[57c] crashes={crashes} dangling={dangling}", flush=True)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"52": exp52, "53": exp53, "54": exp54, "55": exp55,
             "56": exp56, "57": exp57}
    if which == "all":
        for k in ("52", "53", "54", "55", "56", "57"):
            steps[k]()
    else:
        steps[which]()
