import os, sys
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import lzsim.optimizations
from run_v11 import _timing_one, RESULTS, save
jobs = []
for wl in ("container", "rocksdb"):
    for cap in (1024, 2048, 4096):
        jobs.append((wl, "hostdedup", {}, None, cap, None, f"hostdedup-1g-{cap}g"))
        jobs.append((wl, "oracle", dict(sha_hw=True), None, cap, None, f"oracle-{cap}g"))
rows = []
with ProcessPoolExecutor(max_workers=12) as ex:
    for row in ex.map(_timing_one, jobs):
        rows.append(row)
save("ext4_captiming.csv", rows)
