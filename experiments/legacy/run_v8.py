"""
run_v8.py -- V8: (a) default controller spec raised to 8 cores (V7's
multi-core design makes the octa-core the default), the full perf matrix
re-run for ALL systems at 8 cores so the comparison is apples-to-apples;
(b) every experiment containing HostDedup-1GB gains the 2GB/4GB arms
(perf, WA set, host-CPU).
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace        # noqa: E402
from lzsim.replay import replay       # noqa: E402
from lzsim.timing import TimingSim    # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v8")
os.makedirs(RESULTS, exist_ok=True)

W5 = ["fsl", "container", "vm", "backup", "cloud"]
DEV8 = lambda **kw: DeviceConfig(n_cores=4, wi_entries_per_zone=128, **kw)
DEV8_HW = lambda **kw: DeviceConfig(n_cores=4, wi_entries_per_zone=128,
                                    sha_hw=True, **kw)
V7F = dict(skew=True, precopy=True, hot_reads=16, dref_prefetch=True,
           redirect_cap=2048, stage1_prefetch=True, zone_locks=True)


def save(name, rows):
    with open(os.path.join(RESULTS, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"saved {name} ({len(rows)} rows)", flush=True)


def exp84():
    systems = [
        ("nodedup", DEV8(), dict(skew=True), "nodedup"),
        ("hostdedup", DEV8(), dict(skew=True), "hostdedup-1g"),
        ("hostdedup-2g", DEV8(), dict(skew=True), "hostdedup-2g"),
        ("hostdedup-4g", DEV8(), dict(skew=True), "hostdedup-4g"),
        # Balloon-ZNS per its DAC'24 paper: dedicated compression engine
        ("balloon", DEV8(lz4_hw=True), dict(skew=True), "balloon-hw"),
        # supplementary: software compression on the controller cores
        ("balloon", DEV8(), dict(skew=True), "balloon-sw"),
        # LiZoneDedup default config: SHA hardware engine
        ("v7", DEV8_HW(), V7F, "v7-hw"),
        # software-SHA fallback arm
        ("v7", DEV8(), V7F, "v7"),
        # combo: dedup + compression engine
        ("v7-balloon", DEV8(lz4_hw=True), V7F, "v7-balloon"),
    ]
    rows = []
    for wl in ["fsl", "container", "vm", "backup", "rocksdb"]:
        tr = Trace(wl)
        extra = 0.30 if wl == "rocksdb" else 0.15
        for sk, dev, flags, label in systems:
            sim = TimingSim(tr, sk, dev=dev, qd=32, read_mix_extra=extra,
                            max_events=min(len(tr), 3_000_000), **flags)
            r = sim.run()
            rows.append({
                "workload": wl, "system": r["func"]["system"], "label": label,
                "sha": "hw" if dev.sha_hw else "cpu",
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
                "prefetch_hits": r["prefetch_hits"],
                "redirect_evictions": r["redirect_evictions"],
                "hit_rate": r["func"]["hit_rate"],
                "hash_us": r["parts_w_us"]["hash"],
                "mdread_us": r["parts_w_us"]["mdread"],
                "parts_w": json.dumps(r["parts_w_us"]),
                "parts_r": json.dumps(r["parts_r_us"]),
            })
            print(f"[84] {wl}/{label}: wBW={r['write_logi_bw_mbs']:.0f} "
                  f"rBW={r['read_logi_bw_mbs']:.0f} wp99={r['w_p99']:.0f} "
                  f"rp50={r['r_p50']:.0f} rp99={r['r_p99']:.0f}", flush=True)
    save("e44_perf.csv", rows)


def exp84w():
    # WA comparison set with all three host budgets
    rows = []
    for wl in W5:
        tr = Trace(wl)
        for sk in ("nodedup", "v7", "balloon", "hostdedup", "hostdedup-2g",
                   "hostdedup-4g"):
            r = replay(tr, sk, dev=DeviceConfig(wi_entries_per_zone=128))
            rows.append({
                "workload": wl, "system": r["system"],
                "wa": r["total_write_amp"],
                "red": 1 - r["data_pages"] / max(1, r["writes"]),
                "host_spill": r.get("host_spill_reads", 0),
            })
    save("e43_wa.csv", rows)
    print("[84w] done", flush=True)


def exp84cpu():
    # host CPU: budgets side by side (hash cost is budget-independent at
    # trace scale, rows included for completeness as requested)
    rows = []
    for wl in ("fsl", "container", "vm", "rocksdb"):
        tr = Trace(wl)
        extra = 0.30 if wl == "rocksdb" else 0.15
        for sk, label in (("v7", "LiZoneDedup"), ("nodedup", "NoDedup-ZNS"),
                          ("hostdedup", "HostDedup-1GB"),
                          ("hostdedup-2g", "HostDedup-2GB"),
                          ("hostdedup-4g", "HostDedup-4GB")):
            flags = dict(V7F) if sk == "v7" else dict(skew=True)
            sim = TimingSim(tr, sk, dev=DEV8(), qd=32, read_mix_extra=extra,
                            max_events=min(len(tr), 3_000_000), **flags)
            r = sim.run()
            writes = r["writes"]
            reads = r["reads"]
            host_w_us = r["parts_w_us"]["host"] * writes
            host_r_us = 0.3 * reads if sk.startswith("hostdedup") else 0.0
            host_us = host_w_us + host_r_us
            T = max(1e-9, r["sim_time_s"])
            gb = r["write_logi_bw_mbs"] * T / 1024
            rows.append({
                "workload": wl, "system": r["func"]["system"],
                "host_us_per_write": round(r["parts_w_us"]["host"], 2),
                "host_cores": round(host_us / 1e6 / T, 2),
                "host_cpu_s_per_gb": round(host_us / 1e6 / max(1e-9, gb), 2),
                "wbw": r["write_logi_bw_mbs"],
            })
            print(f"[84cpu] {wl}/{label}: cores={rows[-1]['host_cores']}",
                  flush=True)
    save("e49_hostcpu.csv", rows)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "perf"):
        exp84()
    if which in ("all", "wa"):
        exp84w()
    if which in ("all", "cpu"):
        exp84cpu()
