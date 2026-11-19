import os, sys, csv, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import lzsim.optimizations
from run_v10 import run_timing, perf_row, DEV, V7F
from lzsim.traces import Trace
rows = []
for wl in ("fsl", "container", "vm", "backup", "rocksdb"):
    r = run_timing(Trace(wl), "hostdedup", DEV(), wl, dict(skew=True))
    rows.append({"workload": wl, **perf_row(r, "hostdedup-1g", "sw")})
r = run_timing(Trace("container"), "v9", DEV(sha_hw=True), "container", V7F)
rows.append({"workload": "container", **perf_row(r, "lizone-hw", "hw")})
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results_v11", "rerun_parts.csv")
with open(out, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
print("saved rerun_parts.csv", len(rows))
