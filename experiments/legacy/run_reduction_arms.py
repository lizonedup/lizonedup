"""run_reduction_arms.py -- full-trace functional replay for the three
arms of Fig 7 (Physical write reduction): LiZoneDedup, Balloon-ZNS,
LiZone+Balloon.  Backfilled because only LiZone had been run at this
calibre (e1_recall/e1b_recall); Balloon/Combo existed only as timing runs."""
import csv, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lzsim.traces import Trace
from lzsim.replay import replay

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results_v11")
WORKLOADS = ["container", "fsl", "rocksdb", "vm", "backup"]
SYSTEMS = ["lizone", "balloon", "lizone-balloon"]

rows = []
for wl in WORKLOADS:
    tr = Trace(wl)
    for sk in SYSTEMS:
        t0 = time.time()
        r = replay(tr, sk)
        row = {
            "workload": wl, "system_key": sk, "system": r["system"],
            "writes": r["writes"], "hits": r["hits"],
            "dedup_ratio": r["dedup_ratio"],
            "phys_red": 1.0 - r["data_pages"] / max(1, r["writes"]),
            "sec": round(time.time() - t0, 1),
        }
        rows.append(row)
        print(f"[arms] {wl:10s} {sk:14s} phys_red={row['phys_red']*100:5.2f}% "
              f"dedup={row['dedup_ratio']:.3f}x ({row['sec']}s)", flush=True)

out = os.path.join(RESULTS, "e1c_reduction_arms.csv")
with open(out, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)
print(f"saved {out} ({len(rows)} rows)")
