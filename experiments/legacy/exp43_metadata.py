"""
exp43_metadata.py -- Sec 4.3 metadata overhead & write amplification.

Five-layer ablation (GlobalIndex / DataPathOnly / +IdentityShard /
+SplitState / Full) on FSL/Container/VM/Backup: metadata read & write
amplification, cache hit rates, DRAM breakdown.  Plus total-WA comparison
of NoDedup / LiZoneDedup / Balloon-ZNS / HostDedup.
Outputs: results/exp43_ablation.csv, results/exp43_wa.csv
"""

import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lzsim.traces import Trace                     # noqa: E402
from lzsim.replay import replay                    # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results")
WORKLOADS = ["fsl", "container", "vm", "backup"]
ABLATION = ["globalindex", "datapath", "shard", "split", "lizone"]
WA_SYSTEMS = ["nodedup", "lizone", "balloon", "hostdedup"]


def main():
    os.makedirs(RESULTS, exist_ok=True)
    rows = []
    for wl in WORKLOADS:
        tr = Trace(wl)
        for sk in ABLATION:
            t0 = time.time()
            r = replay(tr, sk)
            dram = r.get("dram", {})
            row = {
                "workload": wl, "system": r["system"],
                "md_read_B_per_req": r["md_read_bytes_per_req"],
                "md_write_B_per_data_B": r["md_write_bytes_per_data_byte"],
                "total_wa": r["total_write_amp"],
                "dedup_ratio": r["dedup_ratio"],
                "dram_total_mb": r.get("dram_total_mb", 0.0),
                "dram_route_mb": dram.get("route_table", 0) / 1048576,
                "dram_filters_mb": dram.get("active_zone_filters", 0) / 1048576,
                "dram_wi_mb": dram.get("working_index", 0) / 1048576,
                "dram_delta_mb": dram.get("delta_logs", 0) / 1048576,
                "dram_naive_idx_mb": dram.get("naive_index_dram", 0) / 1048576,
                "dram_caches_mb": dram.get("caches", 0) / 1048576,
                "dram_incoming_mb": dram.get("incoming_ref_counts", 0) / 1048576,
                "wi_hits": r.get("cache_wi_hit", 0),
                "shard_hits": r.get("cache_shard_hit", 0),
                "shard_misses": r.get("cache_shard_miss", 0),
                "filter_hits": r.get("cache_filter_hit", 0),
                "filter_misses": r.get("cache_filter_miss", 0),
                "seals": r.get("seals", 0),
                "sec": round(time.time() - t0, 1),
            }
            rows.append(row)
            print(f"[exp43] {wl}/{sk}: RA={row['md_read_B_per_req']:.0f}B/req "
                  f"WAmd={row['md_write_B_per_data_B']:.4f} "
                  f"DRAM={row['dram_total_mb']:.1f}MB ({row['sec']}s)",
                  flush=True)
    cols = list(rows[0].keys())
    with open(os.path.join(RESULTS, "exp43_ablation.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    rows2 = []
    for wl in WORKLOADS:
        tr = Trace(wl)
        for sk in WA_SYSTEMS:
            r = replay(tr, sk)
            row = {
                "workload": wl, "system": r["system"],
                "total_wa": r["total_write_amp"],
                "phys_reduction": 1.0 - r["data_pages"] / max(1, r["writes"]),
                "host_dram_gb_1tb": r.get("host_dram_gb_1tb", 0.0),
                "dedup_ratio": r["dedup_ratio"],
            }
            rows2.append(row)
            print(f"[exp43-wa] {wl}/{sk}: WA={row['total_wa']:.4f}", flush=True)
    with open(os.path.join(RESULTS, "exp43_wa.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows2[0].keys()))
        w.writeheader()
        w.writerows(rows2)
    print("saved exp43_ablation.csv / exp43_wa.csv")


if __name__ == "__main__":
    main()
