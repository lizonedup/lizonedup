"""
run_zs_sim.py -- simulator zone-size sweep: 128 MiB vs 1 GiB.

Reviewer D asked for GB-scale zones; the paper's own zone-size sweep
(Sec 4.6, Fig. 10b) stopped at 512 MiB.  This script replays the same
five workloads through the full design (v9, r=2) and the r2-prefix
routing arm at 128 MiB and 1 GiB zones on the default 1 TB device, and
reports recall, physical-write reduction, metadata read amplification,
probe cost and DRAM -- the same metrics as e1/e8 so the numbers are
directly comparable with the paper's tables.

Outputs: results_zs/zs_sim.csv (+ printed summary)
One command, fresh clone: python experiments/run_zs_sim.py
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.replay import replay       # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_zs")
os.makedirs(RESULTS, exist_ok=True)

SMOKE = os.environ.get("LZ_SMOKE") == "1"
MEV = 300_000 if SMOKE else 3_000_000
ZONE_SIZES = (128, 1024)
ARMS = ("v9", "v9-prefix")
WORKLOADS = ("container", "backup", "vm", "rocksdb") if not SMOKE else \
            ("container",)

DEV = lambda zs, **kw: DeviceConfig(wi_entries_per_zone=128,
                                    zone_size_mb=zs, **kw)


def ensure_trace(name):
    try:
        from lzsim.traces import Trace
        return Trace(name)
    except FileNotFoundError:
        from lzsim.traces import build_trace
        build_trace(name)
        from lzsim.traces import Trace
        return Trace(name)


def main():
    rows = []
    for wl in WORKLOADS:
        tr = ensure_trace(wl)
        n = min(MEV, len(tr))
        oh = replay(tr, "oracle", dev=DEV(128), max_events=n)["hits"]
        for zs in ZONE_SIZES:
            for sk in ARMS:
                r = replay(tr, sk, dev=DEV(zs), max_events=n)
                f = r  # replay() returns the summary flat
                rows.append({
                    "workload": wl, "zone_mb": zs, "arm": sk,
                    "writes": r["writes"],
                    "recall": round(r["hits"] / max(1, oh), 4),
                    "phys_red": round(1 - r["data_pages"] /
                                      max(1, r["writes"]), 4),
                    "ra_b_per_req": round(f["md_read_bytes_per_req"], 2),
                    "probe_avg": round(f["probe_avg"], 3),
                    "dram_mb": round(f["dram_total_mb"], 2),
                    "zones_used": f["zones_used"],
                    "seals": f["seals"],
                    "cross_zone": round(f["cross_zone_desc_frac"], 4),
                    "sealed_hit_frac": round(f["hit_source_sealed_frac"], 4),
                })
                print(f"[ZS] {wl}/{zs}MiB/{sk}: recall={rows[-1]['recall']:.4f}"
                      f" physred={rows[-1]['phys_red']:.4f}"
                      f" RA={rows[-1]['ra_b_per_req']:.1f}B"
                      f" DRAM={rows[-1]['dram_mb']:.1f}MB"
                      f" zones={rows[-1]['zones_used']}", flush=True)
    with open(os.path.join(RESULTS, "zs_sim.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"saved zs_sim.csv ({len(rows)} rows)")

    print("=" * 72)
    for wl in WORKLOADS:
        for arm in ARMS:
            a = next(r for r in rows if r["workload"] == wl and
                     r["zone_mb"] == 128 and r["arm"] == arm)
            b = next(r for r in rows if r["workload"] == wl and
                     r["zone_mb"] == 1024 and r["arm"] == arm)
            print(f"[ZS] {wl:9s} {arm:10s} "
                  f"128MiB: recall={a['recall']:.3f} "
                  f"phys-red={a['phys_red']*100:.1f}% "
                  f"RA={a['ra_b_per_req']:.0f}B "
                  f"| 1GiB: recall={b['recall']:.3f} "
                  f"phys-red={b['phys_red']*100:.1f}% "
                  f"RA={b['ra_b_per_req']:.0f}B "
                  f"(phys-red delta {(b['phys_red']-a['phys_red'])*100:+.1f}pp)")


if __name__ == "__main__":
    main()
