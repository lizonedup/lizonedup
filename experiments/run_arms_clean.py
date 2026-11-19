"""run_arms_clean.py -- ONE batch for Table 3 + Fig 7.

Config matches run_v11.py exactly: DEV(wi_entries_per_zone=128), FULL traces
(no max_events).  Systems: oracle, lizone, balloon, lizone-balloon.
Previous numbers came from two different runs (e1_recall: full / e1b_recall:
capped at 3M) plus a third config for the arms that had never been run.
"""
import csv, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lzsim.traces import Trace
from lzsim.replay import replay
from lzsim.device import DeviceConfig

DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)
RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results_v11")
WORKLOADS = ["container", "fsl", "rocksdb", "vm", "backup"]
SYSTEMS = ["oracle", "lizone", "balloon", "lizone-balloon"]

rows = []
for wl in WORKLOADS:
    tr = Trace(wl)
    for sk in SYSTEMS:
        t0 = time.time()
        r = replay(tr, sk, dev=DEV(), track_locus=(sk == "lizone"))
        row = {
            "workload": wl, "system_key": sk, "system": r["system"],
            "writes": r["writes"], "hits": r["hits"],
            "dedup": r["dedup_ratio"],
            "phys_red": 1 - r["data_pages"] / max(1, r["writes"]),
            "sec": round(time.time() - t0, 1),
        }
        rows.append(row)
        print(f"[clean] {wl:10s} {sk:14s} phys_red={row['phys_red']*100:6.2f}% "
              f"dedup={row['dedup']:.3f}x ({row['sec']}s)", flush=True)

out = os.path.join(RESULTS, "e1c_reduction_clean.csv")
with open(out, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)
print(f"saved {out} ({len(rows)} rows)")
