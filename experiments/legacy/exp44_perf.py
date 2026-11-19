"""
exp44_perf.py -- Sec 4.4 end-to-end performance (closed-loop timing).

Systems: NoDedup / HostDedup / LiZoneDedup / Balloon-ZNS / combo on
FSL / Container / VM / RocksDB.  Reports logical/physical throughput,
p50/p95/p99 read+write latency, hit/miss and data/descriptor-backed splits,
latency decomposition, and the 4.1 microbenchmark constants.
Outputs: results/exp44_perf.csv
"""

import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lzsim.traces import Trace                     # noqa: E402
from lzsim.timing import TimingSim                 # noqa: E402
from lzsim.device import DEFAULT_DEVICE            # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results")
WORKLOADS = ["fsl", "container", "vm", "rocksdb"]
SYSTEMS = ["nodedup", "hostdedup", "lizone", "balloon", "lizone-balloon"]


def main():
    os.makedirs(RESULTS, exist_ok=True)
    rows = []
    for wl in WORKLOADS:
        tr = Trace(wl)
        extra = 0.15 if wl != "rocksdb" else 0.0
        me = len(tr) if wl == "rocksdb" else 3_000_000
        for sk in SYSTEMS:
            t0 = time.time()
            sim = TimingSim(tr, sk, dev=DEFAULT_DEVICE, qd=32,
                            read_mix_extra=extra, max_events=me)
            r = sim.run()
            row = {
                "workload": wl, "system": sk, "qd": r["qd"],
                "writes": r["writes"], "reads": r["reads"],
                "w_bw_mbs": r["write_logi_bw_mbs"],
                "w_phys_mbs": r["write_phys_bw_mbs"],
                "r_bw_mbs": r["read_logi_bw_mbs"],
                "r_phys_mbs": r["read_phys_bw_mbs"],
                "w_p50": r["w_p50"], "w_p95": r["w_p95"], "w_p99": r["w_p99"],
                "r_p50": r["r_p50"], "r_p95": r["r_p95"], "r_p99": r["r_p99"],
                "hit_lat_us": r["hit_lat_mean"],
                "miss_lat_us": r["miss_lat_mean"],
                "data_read_lat_us": r["data_read_lat_mean"],
                "desc_read_lat_us": r["desc_read_lat_mean"],
                "desc_read_frac": r["desc_read_frac"],
                "desc_cached_frac": r["desc_cached_frac"],
                "hit_rate": r["func"]["hit_rate"],
                "parts_w": json.dumps(r["parts_w_us"]),
                "parts_r": json.dumps(r["parts_r_us"]),
                "sec": round(time.time() - t0, 1),
            }
            rows.append(row)
            print(f"[exp44] {wl}/{sk}: wBW={row['w_bw_mbs']:.0f}MB/s "
                  f"wp99={row['w_p99']:.1f}us rp99={row['r_p99']:.1f}us "
                  f"({row['sec']}s)", flush=True)
    with open(os.path.join(RESULTS, "exp44_perf.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("saved exp44_perf.csv")

    # 4.1 microbenchmarks (deterministic constants from the device model)
    d = DEFAULT_DEVICE
    micro = {
        "route_table_lookup_ns": d.c_route * 1000,
        "filter_probe_ns": d.c_filter * 1000,
        "confirm_cached_ns": d.c_scan * 1000,
        "confirm_flash_us": d.c_scan + d.t_read,
        "dref_commit_us": d.c_dref,
        "read_resolve_data_us": d.c_status + d.t_read,
        "read_resolve_desc_us": (d.c_status + d.c_dparse +
                                 d.t_read * 2),
        "sha256_4k_us": d.c_sha,
        "simhash_4k_us": d.c_simhash,
        "lz4_4k_us": d.c_lz4_c,
        "page_read_us": d.t_read,
        "page_prog_us": d.t_prog,
        "seq_write_bw_gbps": d.seq_write_bw_gbps,
    }
    with open(os.path.join(RESULTS, "exp41_microbench.json"), "w") as f:
        json.dump(micro, f, indent=2)
    print("saved exp41_microbench.json:", micro)


if __name__ == "__main__":
    main()
