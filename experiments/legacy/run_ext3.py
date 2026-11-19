import os, sys
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import lzsim.optimizations
from run_v11 import _timing_one, RESULTS, save
jobs = []
for wl in ("container", "rocksdb"):
    jobs.append((wl, "hostdedup", {}, None, None, 128, "hostdedup-1g-z128"))
    jobs.append((wl, "oracle", dict(sha_hw=True), None, None, 128, "oracle-z128"))
rows = []
with ProcessPoolExecutor(max_workers=4) as ex:
    for row in ex.map(_timing_one, jobs):
        rows.append(row)
save("ext3_zone128.csv", rows)
