"""Full timing rerun under the corrected cost model (SHA hw2/sw8,
SimHash 4 always; LZ4 c hw1.5/sw8, d hw1/sw2).  One unified V7F batch;
e7b/e8b cover all points directly (no cross-batch e4 borrowing)."""
import os, sys
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import lzsim.optimizations
from run_v11 import _timing_one, RESULTS, save

W5 = ("fsl", "container", "vm", "backup", "rocksdb")
SYSTEMS = (
    ("nodedup", {}, "nodedup"),
    ("hostdedup", {}, "hostdedup-1g"),
    ("v9", {}, "lizone-sw"),
    ("v9", dict(sha_hw=True), "lizone-hw"),
    ("balloon", {}, "balloon-sw"),
    ("balloon", dict(lz4_hw=True), "balloon-hw"),
    ("v9-balloon", {}, "combo-sw"),
    ("v9-balloon", dict(sha_hw=True, lz4_hw=True), "combo-hw"),
    ("oracle", dict(sha_hw=True), "oracle"),
)
jobs = {"e4": [], "e4b": [], "e5": [], "e7b": [], "e8b": [], "cloud": []}
for wl in W5:
    for sk, kw, lab in SYSTEMS:
        jobs["e4"].append((wl, sk, kw, None, None, None, lab))
for lab in ("nodedup", "v9-default", "v9-bypass5(default)", "v9-bypass15"):
    kw = dict(sha_hw=True) if lab != "nodedup" else {}
    sk = "v9" if lab != "nodedup" else "nodedup"
    jobs["e4b"].append(("rocksdb", sk, kw, None, None, None, lab))
for t in (60.0, 100.0, 200.0):
    for sk, kw, lab in (("nodedup", {}, "nodedup"),
                        ("hostdedup", {}, "hostdedup-1g"),
                        ("v9", dict(sha_hw=True), "lizone-hw"),
                        ("oracle", dict(sha_hw=True), "oracle")):
        jobs["e5"].append(("container", sk, kw, t, None, None, lab))
        jobs["e5"].append(("rocksdb", sk, kw, t, None, None, lab))
for cap in (100, 200, 500, 1024, 2048, 4096):
    for sk, kw, lab in (("nodedup", {}, "nodedup"),
                        ("v9", dict(sha_hw=True), "lizone-hw"),
                        ("hostdedup", {}, "hostdedup-1g"),
                        ("oracle", dict(sha_hw=True), "oracle")):
        for wl in ("container", "rocksdb"):
            jobs["e7b"].append((wl, sk, kw, None, cap, None, f"{lab}-{cap}g"))
for z in (128, 256, 512):
    for sk, kw, lab in (("nodedup", {}, "nodedup"),
                        ("v9", dict(sha_hw=True), "lizone-hw"),
                        ("hostdedup", {}, "hostdedup-1g"),
                        ("oracle", dict(sha_hw=True), "oracle")):
        for wl in ("container", "rocksdb"):
            jobs["e8b"].append((wl, sk, kw, None, None, z, f"{lab}-z{z}"))
for sk, kw, lab in (("nodedup", {}, "nodedup"),
                    ("v9", dict(sha_hw=True), "lizone-hw"),
                    ("balloon", dict(lz4_hw=True), "balloon-hw"),
                    ("v9-balloon", dict(sha_hw=True, lz4_hw=True), "combo-hw")):
    jobs["cloud"].append(("cloud", sk, kw, None, None, None, lab))

total = sum(len(v) for v in jobs.values())
print("total jobs:", total, flush=True)
with ProcessPoolExecutor(max_workers=24) as ex:
    for name, batch in jobs.items():
        rows = list(ex.map(_timing_one, batch))
        save(f"rerun_{name}.csv", rows)
print("ALL DONE", flush=True)
