"""run_v8b.py -- append the symmetric combo arm (SHA engine + compression
engine) to results_v8/e44_perf.csv."""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace        # noqa: E402
from lzsim.timing import TimingSim    # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v8")
V7F = dict(skew=True, precopy=True, hot_reads=16, dref_prefetch=True,
           redirect_cap=2048, stage1_prefetch=True, zone_locks=True)
DEV = DeviceConfig(n_cores=4, wi_entries_per_zone=128, sha_hw=True,
                   lz4_hw=True)

rows = []
for wl in ["fsl", "container", "vm", "backup", "rocksdb"]:
    tr = Trace(wl)
    extra = 0.30 if wl == "rocksdb" else 0.15
    sim = TimingSim(tr, "v7-balloon", dev=DEV, qd=32, read_mix_extra=extra,
                    max_events=min(len(tr), 3_000_000), **V7F)
    r = sim.run()
    rows.append({
        "workload": wl, "system": r["func"]["system"], "label": "v7-balloon-hw",
        "sha": "hw",
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
    print(f"[8b] {wl}/combo-hw: wBW={r['write_logi_bw_mbs']:.0f} "
          f"rBW={r['read_logi_bw_mbs']:.0f} wp99={r['w_p99']:.0f}",
          flush=True)

out = os.path.join(RESULTS, "e44_perf.csv")
with open(out, newline="") as fh:
    old = list(csv.DictReader(fh))
    fields = list(old[0].keys())
with open(out, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=fields)
    w.writeheader()
    w.writerows(old + rows)
print(f"appended {len(rows)} combo-hw rows -> e44_perf.csv", flush=True)
