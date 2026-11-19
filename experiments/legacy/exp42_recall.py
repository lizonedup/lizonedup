"""
exp42_recall.py -- Sec 4.2 dedup effectiveness.

Recall of bounded candidate-zone lookup (r=1,2,3) vs GlobalDedup-Oracle on
FSL / Container / VM; physical write reduction; absolute dedup ratio;
routing false-negative rate and its distribution by source-zone state.
Outputs: results/exp42_recall.csv
"""

import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lzsim.traces import Trace                     # noqa: E402
from lzsim.replay import replay                    # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results")
WORKLOADS = ["fsl", "container", "vm"]
SYSTEMS = ["oracle", "lizone-r1", "lizone", "lizone-r3"]


def main():
    os.makedirs(RESULTS, exist_ok=True)
    rows = []
    for wl in WORKLOADS:
        tr = Trace(wl)
        oracle_hits = None
        for sk in SYSTEMS:
            t0 = time.time()
            r = replay(tr, sk, track_locus=(sk == "lizone"))
            if sk == "oracle":
                oracle_hits = r["hits"]
            recall = r["hits"] / max(1, oracle_hits)
            row = {
                "workload": wl, "system": r["system"], "r": 0,
                "writes": r["writes"], "hits": r["hits"],
                "recall": recall,
                "dedup_ratio": r["dedup_ratio"],
                "phys_reduction": 1.0 - r["data_pages"] / max(1, r["writes"]),
                "phys_reduction_vs_oracle": (
                    (oracle_hits - (r["writes"] - r["hits"])) /
                    max(1, oracle_hits)),
                "routing_fn_rate": r.get("routing_fn_rate", 0.0),
                "fn_sealed_src": r.get("fn_sealed_src", 0),
                "fn_active_src": r.get("fn_active_src", 0),
                "hit_source_sealed_frac": r.get("hit_source_sealed_frac", 0),
                "cross_zone_desc_frac": r.get("cross_zone_desc_frac", 0),
                "zones": r.get("zones_used", 0),
                "oracle_hits": oracle_hits,
                "sec": round(time.time() - t0, 1),
            }
            rmap = {"LiZoneDedup-r1": 1, "LiZoneDedup": 2, "LiZoneDedup-r3": 3}
            row["r"] = rmap.get(r["system"], 0)
            rows.append(row)
            print(f"[exp42] {wl}/{sk}: recall={recall:.4f} "
                  f"dedup={row['dedup_ratio']:.2f}x "
                  f"fn={row['routing_fn_rate']:.4f} ({row['sec']}s)",
                  flush=True)
    cols = list(rows[0].keys())
    with open(os.path.join(RESULTS, "exp42_recall.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print("saved exp42_recall.csv")


if __name__ == "__main__":
    main()
