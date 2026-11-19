"""
exp45_compress.py -- Sec 4.5 dedup vs compression.

NoDedup / LiZoneDedup / Balloon-ZNS / combo on FSL/Container/VM/RocksDB:
physical write reduction, effective capacity gain (incl. OP elimination for
Balloon), per-content-type breakdown, controller compute cost per page.
Outputs: results/exp45_compress.csv
"""

import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lzsim.traces import Trace, fp_ctype, CTYPE_NAMES, fp_crc  # noqa: E402
from lzsim.replay import replay                    # noqa: E402
from lzsim.systems import balloon_bytes            # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results")
WORKLOADS = ["fsl", "container", "vm", "rocksdb"]
SYSTEMS = ["nodedup", "lizone", "balloon", "lizone-balloon"]

# Balloon-ZNS eliminates over-provisioning (7%-28% of raw); we use 20%
# (typical datacenter ZNS OP) as the capacity headroom it can expose.
OP_FRAC = 0.20


def main():
    os.makedirs(RESULTS, exist_ok=True)
    rows = []
    for wl in WORKLOADS:
        tr = Trace(wl)
        for sk in SYSTEMS:
            t0 = time.time()
            r = replay(tr, sk)
            phys_red = 1.0 - r["data_pages"] / max(1, r["writes"])
            if sk == "balloon":
                mean_cr = r.get("mean_cr", 1.0)
                cap_gain = (1.0 / mean_cr) - 1.0 + OP_FRAC
            elif sk == "lizone":
                cap_gain = r["dedup_ratio"] - 1.0
            elif sk == "lizone-balloon":
                cap_gain = r["dedup_ratio"] - 1.0 + OP_FRAC * 0.3
            else:
                cap_gain = 0.0
            # per-type dedup & compression
            byt = r.get("by_type_total", {})
            byh = r.get("by_type_hits", {})
            type_dedup = {t: byh.get(t, 0) / max(1, byt.get(t, 1))
                          for t in CTYPE_NAMES if byt.get(t)}
            # mean compression ratio per type over unique pages (measured
            # on the trace's unique fps)
            row = {
                "workload": wl, "system": r["system"],
                "phys_reduction": phys_red,
                "cap_gain": cap_gain,
                "dedup_ratio": r["dedup_ratio"],
                "compute_us_per_req": r["compute_us_per_req"],
                "mean_cr": r.get("mean_cr", 1.0),
                "type_dedup": type_dedup,
                "sec": round(time.time() - t0, 1),
            }
            rows.append(row)
            print(f"[exp45] {wl}/{sk}: red={phys_red:.3f} "
                  f"cap=+{cap_gain:.3f} ({row['sec']}s)", flush=True)

        # per-type compression ratio on unique pages (one pass)
        seen = set()
        cr_sum = {t: 0.0 for t in CTYPE_NAMES}
        cr_n = {t: 0 for t in CTYPE_NAMES}
        for i in range(len(tr)):
            op, fp, L = tr.event(i)
            if op:
                continue
            if fp in seen:
                continue
            seen.add(fp)
            t = fp_ctype(fp)
            cr_sum[CTYPE_NAMES[t]] += fp_crc(fp)
            cr_n[CTYPE_NAMES[t]] += 1
        for t in CTYPE_NAMES:
            if cr_n[t]:
                rows.append({
                    "workload": wl, "system": "typecr:" + t,
                    "phys_reduction": cr_sum[t] / cr_n[t],
                    "cap_gain": cr_n[t], "dedup_ratio": 0,
                    "compute_us_per_req": 0, "mean_cr": 0, "type_dedup": {},
                    "sec": 0,
                })

    with open(os.path.join(RESULTS, "exp45_compress.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            r2 = dict(r)
            r2["type_dedup"] = json.dumps(r["type_dedup"]) \
                if isinstance(r["type_dedup"], dict) else r["type_dedup"]
            w.writerow(r2)
    print("saved exp45_compress.csv")


import json  # noqa: E402

if __name__ == "__main__":
    main()
