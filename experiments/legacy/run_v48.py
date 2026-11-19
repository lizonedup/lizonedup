"""
run_v48.py -- optimization closing-the-loop experiments (paper Sec 4.8).

  48a bypass   : dynamic pipeline bypass on low-dup workloads (rocksdb/cloud)
  48b precopy  : hot descriptor pre-copy + redirect (fsl/container, skew)
  48c cold     : bounded cold fingerprint index recall (fsl/cloud)
  48d lifecyc  : targeted pinned-zone migration (backup/container, keep sweep)
  48e pool     : reserve open-zone pool pressure analysis (opt 4)
"""
import csv
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401  (registers new systems)
from lzsim.traces import Trace        # noqa: E402
from lzsim.replay import replay       # noqa: E402
from lzsim.timing import TimingSim     # noqa: E402
from lzsim.device import DEFAULT_DEVICE  # noqa: E402
from lzsim.lifecycle import LifecycleSim  # noqa: E402
from lzsim.systems import DEAD  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v4")
os.makedirs(RESULTS, exist_ok=True)


def save(name, rows):
    if not rows:
        return
    with open(os.path.join(RESULTS, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"saved {name} ({len(rows)} rows)", flush=True)


def exp48a():
    # timing comparison: full pipeline vs adaptive bypass
    rows = []
    for wl in ("rocksdb", "cloud"):
        tr = Trace(wl)
        extra = 0.30 if wl == "rocksdb" else 0.15
        for sk, tag in (("lizone", "off"), ("lizone-bypass", "on")):
            sim = TimingSim(tr, sk, qd=32, read_mix_extra=extra,
                            max_events=min(len(tr), 3_000_000))
            r = sim.run()
            fn = r["func"]
            sysobj = sim.sys
            rows.append({
                "workload": wl, "bypass": tag, "system": fn["system"],
                "wbw": r["write_logi_bw_mbs"], "wphys": r["write_phys_bw_mbs"],
                "wp50": r["w_p50"], "wp99": r["w_p99"],
                "hash_us": r["parts_w_us"]["hash"],
                "hit_rate": fn["hit_rate"], "dedup": fn["dedup_ratio"],
                "wa": fn["total_write_amp"],
                "bypass_frac": getattr(sysobj, "n_bypassed", 0) /
                max(1, fn["writes"]),
                "probes": getattr(sysobj, "n_probes", 0),
                "switches": getattr(sysobj, "n_switches", 0),
            })
            print(f"[48a] {wl}/{tag}: wBW={r['write_logi_bw_mbs']:.0f} "
                  f"wp99={r['w_p99']:.0f} hit={fn['hit_rate']:.3f}",
                  flush=True)
    save("e48_bypass.csv", rows)

    # window dynamics on rocksdb (for the switching trace figure)
    tr = Trace("rocksdb")
    r = replay(tr, "lizone-bypass")
    rows = [{"window": w, "rate": rate, "probe_rate": prate, "bypass": st}
            for w, rate, prate, st in r.get("window_log", [])]
    save("e48_bypass_windows.csv", rows)


def exp48b():
    # read-path pre-copy: baseline vs precopy under identical skewed reads
    # (read-heavy phase, reads issued on top of writes, Zipf-like hot set)
    rows = []
    for wl in ("fsl", "container"):
        tr = Trace(wl)
        for precopy in (False, True):
            sim = TimingSim(tr, "lizone", qd=32, read_mix_extra=1.0,
                            max_events=min(len(tr), 3_000_000),
                            skew=True, precopy=precopy, hot_reads=16,
                            additive_reads=True)
            r = sim.run()
            rows.append({
                "workload": wl, "precopy": int(precopy),
                "rp50": r["r_p50"], "rp95": r["r_p95"], "rp99": r["r_p99"],
                "desc_read": r["desc_read_lat_mean"],
                "data_read": r["data_read_lat_mean"],
                "redir_read": r["redir_read_lat_mean"],
                "desc_frac": r["desc_read_frac"],
                "redirect_frac": r["redirect_frac"],
                "hot_copied": r["hot_copied"],
                "bg_util": r["bg_chan_util"],
                "wbw": r["write_logi_bw_mbs"],
            })
            print(f"[48b] {wl}/precopy={precopy}: rp99={r['r_p99']:.0f} "
                  f"redir={r['redirect_frac']:.3f}", flush=True)
    save("e48_precopy.csv", rows)


def exp48c():
    rows = []
    for wl in ("fsl", "cloud", "container"):
        tr = Trace(wl)
        oh = replay(tr, "oracle")["hits"]
        for sk in ("lizone", "lizone-cold"):
            r = replay(tr, sk)
            row = {
                "workload": wl, "system": r["system"],
                "recall": r["hits"] / max(1, oh),
                "dedup": r["dedup_ratio"],
                "ra": r["md_read_bytes_per_req"],
                "dram": r.get("dram_total_mb", 0),
                "dram_cold_mb": r.get("dram", {}).get("cold_index", 0) /
                1048576,
                "cold_hits": r.get("cold_hits", 0),
                "cold_probes": r.get("cold_probes", 0),
                "cold_md_pages": r.get("cold_md_pages", 0),
                "cold_entries": r.get("cold_entries", 0),
                "cold_evicted": r.get("cold_evicted", 0),
            }
            rows.append(row)
            print(f"[48c] {wl}/{sk}: recall={rows[-1]['recall']:.4f} "
                  f"RA={rows[-1]['ra']:.1f}", flush=True)
    save("e48_cold.csv", rows)


def _run_life(wl, keep, migrate):
    rng = random.Random(4747)
    tr = Trace(wl)
    # n_zones_device tightened to create real GC pressure (alive zones
    # approach the device capacity during the 1.8 M event replay)
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


def exp48d():
    rows = []
    for wl in ("backup", "container"):
        for keep in (0.9, 0.5, 0.1):
            for mig in (False, True):
                lz = _run_life(wl, keep, mig)
                alive = [z for z in lz.zones.values() if z.state != DEAD]
                pinned = [z for z in alive if z.incoming_refs > 0]
                cap = sum(z.data_pages for z in alive)
                pcap = sum(z.data_pages for z in pinned)
                trapped = sum(z.dead_data for z in pinned)
                pend = lz.pending_durations_events
                rows.append({
                    "workload": wl, "keep": keep, "migrate": int(mig),
                    "dead": lz.avg_dead_ratio(),
                    "pinned_zone_frac": len(pinned) / max(1, len(alive)),
                    "pinned_cap_frac": pcap / max(1, cap),
                    "trapped_dead_gb": trapped * 4096 / 1e9,
                    "reclaimed_gb": lz.gc_reclaimed_dead * 4096 / 1e9,
                    "free_zones": lz.free_zones(),
                    "targeted_migs": lz.n_targeted_migs,
                    "targeted_pages": lz.targeted_pages,
                    "gc_n": lz.gc_count,
                    "gc_blocked": lz.gc_blocked_pinned,
                    "mig_gb": lz.gc_migrated_pages * 4096 / 1e9,
                    "gc_upd": lz.gc_desc_updates,
                    "dip": lz.gc_busy_us_total / max(1e-9,
                        lz.gc_busy_us_total + lz.fg_busy_us_total),
                    "pending_rate": lz.n_pending_resets /
                    max(1, lz.n_host_resets),
                    "pending_mean_ev": (sum(pend) / len(pend)) if pend else 0,
                    "dedup": lz.n_w / max(1, lz.n_w - lz.n_hits),
                })
                print(f"[48d] {wl} keep={keep} mig={mig}: "
                      f"pinnedcap={rows[-1]['pinned_cap_frac']:.3f} "
                      f"tmigs={lz.n_targeted_migs}", flush=True)
    save("e48_migration.csv", rows)


def exp48e():
    # reserve open-zone pool: concurrent reset-pending zones each hold one
    # readable-open slot that cannot take appends; a pool of P pre-opened
    # zones absorbs P concurrent pendings without allocation stalls
    rows = []
    for wl in ("backup", "container"):
        for keep in (0.9, 0.5, 0.1):
            lz = _run_life(wl, keep, False)
            s = sorted(lz.pressure_samples)
            n = max(1, len(s))
            rows.append({
                "workload": wl, "keep": keep,
                "samples": len(s),
                "pend_mean": sum(s) / n,
                "pend_p50": s[n // 2],
                "pend_p95": s[int(0.95 * n)],
                "pend_max": s[-1],
                "fit_pool4": sum(1 for x in s if x <= 4) / n,
                "fit_pool8": sum(1 for x in s if x <= 8) / n,
            })
            print(f"[48e] {wl} keep={keep}: mean={rows[-1]['pend_mean']:.2f} "
                  f"p95={rows[-1]['pend_p95']} max={rows[-1]['pend_max']} "
                  f"fit4={rows[-1]['fit_pool4']:.3f}", flush=True)
    save("e48_pool.csv", rows)




def exp48f():
    # bypass under a compute-constrained controller (hash no longer fully
    # overlaps NAND): rocksdb, cpu 100% vs cpu -50%
    from lzsim.device import DeviceConfig
    rows = []
    tr = Trace("rocksdb")
    for tag, kw in (("cpu100", {}), ("cpu50", {"cpu_speedup": 0.5})):
        dev = DeviceConfig(**kw)
        for sk, bt in (("nodedup", "nodedup"), ("lizone", "off"),
                       ("lizone-bypass", "on")):
            sim = TimingSim(tr, sk, dev=dev, qd=32, read_mix_extra=0.30,
                            max_events=min(len(tr), 3_000_000))
            r = sim.run()
            fn = r["func"]
            rows.append({
                "variant": tag, "bypass": bt, "system": fn["system"],
                "wbw": r["write_logi_bw_mbs"], "wphys": r["write_phys_bw_mbs"],
                "wp50": r["w_p50"], "wp99": r["w_p99"],
                "hash_us": r["parts_w_us"]["hash"],
                "hit_rate": fn["hit_rate"], "wa": fn["total_write_amp"],
            })
            print(f"[48f] {tag}/{bt}: wBW={r['write_logi_bw_mbs']:.0f} "
                  f"wp99={r['w_p99']:.0f}", flush=True)
    save("e48_bypass_cpu.csv", rows)

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "a"):
        exp48a()
    if which in ("all", "b"):
        exp48b()
    if which in ("all", "c"):
        exp48c()
    if which in ("all", "d"):
        exp48d()
    if which in ("all", "e"):
        exp48e()
    if which in ("all", "f"):
        exp48f()
