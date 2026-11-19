"""
run_e7d_dram.py -- Fig 9a redo: LiZone-only index DRAM for ALL SIX workloads.

One functional v9 replay per workload at the default 1 TB device (lz DRAM is
capacity-invariant, cf. e7_capacity.csv), emitting the same breakdown fields
as exp117f so the panel can swap data sources without touching semantics.

Output: results_v11/e7d_dram_bywl.csv
"""
import csv
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace        # noqa: E402
from lzsim.replay import replay       # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v11")
MEV = 3_000_000

DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)


def one(wl):
    tr = Trace(wl)
    r = replay(tr, "v9", dev=DEV(), max_events=MEV)
    dr = r["dram"]
    row = {
        "workload": wl,
        "lz_dram_mb": r["dram_total_mb"],
        "lz_active_zone_dram_mb": (dr["active_zone_filters"] +
                                   dr["delta_logs"] +
                                   dr["working_index"]) / 1048576,
        "lz_cache_mb": dr["caches"] / 1048576,
        "zones_touched": r["zones_used"],
        "dedup": r["dedup_ratio"],
    }
    print(f"[e7d] {wl}: dram={row['lz_dram_mb']:.1f}MB "
          f"(az={row['lz_active_zone_dram_mb']:.1f} cache="
          f"{row['lz_cache_mb']:.1f}) zones={row['zones_touched']}",
          flush=True)
    return row


if __name__ == "__main__":
    os.makedirs(RESULTS, exist_ok=True)
    wls = sys.argv[1:] or ["container", "fsl", "rocksdb", "vm", "backup",
                           "cloud"]
    with ProcessPoolExecutor(max_workers=min(6, len(wls))) as ex:
        rows = list(ex.map(one, wls))
    out = os.path.join(RESULTS, "e7d_dram_bywl.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"saved {out} ({len(rows)} rows)", flush=True)
