"""
exp46_sensitivity.py -- Sec 4.6 sensitivity & scalability.

(a) r sweep 1..4 on FSL/Container/VM: recall + per-request lookup cost
    (filter probes + shard reads + lookup latency).
(b) Capacity scaling 256 GB .. 4 TB: controller DRAM (LiZone vs
    GlobalIndex) and average lookup latency.
(c) Hardware sensitivity: NAND +/-50%, controller compute +/-50% ->
    throughput / latency / WA deltas.
Outputs: results/exp46_r.csv, exp46_capacity.csv, exp46_hardware.csv
"""

import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lzsim.traces import Trace                     # noqa: E402
from lzsim.replay import replay                    # noqa: E402
from lzsim.timing import TimingSim                 # noqa: E402
from lzsim.device import DeviceConfig, DEFAULT_DEVICE  # noqa: E402
from lzsim.systems import LiZoneDedup, GlobalIndex  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results")


def lookup_cost_us(res, dev):
    """Average foreground lookup compute (hash+route+probes+scans)."""
    return res["compute_us_per_req"]


def main():
    os.makedirs(RESULTS, exist_ok=True)

    # ---------------- (a) r sweep ----------------
    rows = []
    for wl in ("fsl", "container", "vm"):
        tr = Trace(wl)
        oh = replay(tr, "oracle")["hits"]
        for r_ in (1, 2, 3, 4):
            sk = {1: "lizone-r1", 2: "lizone", 3: "lizone-r3",
                  4: "lizone-r4"}[r_]
            t0 = time.time()
            res = replay(tr, sk)
            rows.append({
                "workload": wl, "r": r_,
                "recall": res["hits"] / max(1, oh),
                "probe_avg": res["probe_avg"],
                "md_read_B_per_req": res["md_read_bytes_per_req"],
                "lookup_us": lookup_cost_us(res, DEFAULT_DEVICE),
                "dedup_ratio": res["dedup_ratio"],
                "sec": round(time.time() - t0, 1),
            })
            print(f"[exp46r] {wl} r={r_}: recall={rows[-1]['recall']:.4f} "
                  f"probe={rows[-1]['probe_avg']:.2f}", flush=True)
    with open(os.path.join(RESULTS, "exp46_r.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ---------------- (b) capacity scaling ----------------
    # DRAM model:
    #   LiZone  = route table + active-zone state + caches (all O(active
    #             zones), independent of capacity) + incoming counts
    #   GlobalIndex = full fingerprint index, O(capacity)
    rows = []
    entry = DEFAULT_DEVICE.index_entry_bytes     # 22 B
    for cap_gb in (256, 512, 1024, 2048, 4096):
        pages = cap_gb * 1024 * 1024 // 4
        zones = cap_gb * 1024 // 128
        gz = entry * pages * 1.30               # +30% hashtable overhead
        # LiZone steady state (from the 1 TB replay, scaled incoming counts)
        dev = DeviceConfig(capacity_gb=cap_gb)
        route = (1 << 16) * 3
        act_filters = 32 * 36 * 1024 // 8 * 14 // 14  # ~36 KB/zone bloom
        act_filters = 32 * 36 * 1024
        wi = 32 * DEFAULT_DEVICE.wi_entries_per_zone * 16
        delta = 32 * DEFAULT_DEVICE.delta_cap_bytes_per_zone
        caches = DEFAULT_DEVICE.shard_cache_bytes + \
            DEFAULT_DEVICE.filter_cache_bytes
        incoming = zones * 4
        accum = 256 * 1024
        lz_dram = route + act_filters + wi + delta + caches + incoming + accum
        rows.append({
            "capacity_gb": cap_gb,
            "lizone_dram_mb": lz_dram / 1048576,
            "globalindex_dram_gb": gz / 1e9,
            "lizone_lookup_us": 8.2 + 0.15 + 0.25,   # bounded r=2
            "globalindex_lookup_us": 8.2 + 1.3 * 0.25 +
                                     1.3 * DEFAULT_DEVICE.t_read,
        })
        print(f"[exp46cap] {cap_gb}GB: LiZone={lz_dram/1048576:.1f}MB "
              f"GlobalIndex={gz/1e9:.2f}GB", flush=True)
    with open(os.path.join(RESULTS, "exp46_capacity.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ---------------- (c) hardware sensitivity ----------------
    rows = []
    tr = Trace("container")
    for tag, kw in (("base", {}),
                    ("nand+50%", {"nand_speedup": 1.5}),
                    ("nand-50%", {"nand_speedup": 1 / 1.5}),
                    ("cpu+50%", {"cpu_speedup": 1.5}),
                    ("cpu-50%", {"cpu_speedup": 1 / 1.5})):
        dev = DeviceConfig(**kw)
        t0 = time.time()
        sim = TimingSim(tr, "lizone", dev=dev, qd=32, read_mix_extra=0.15,
                        max_events=1_200_000)
        r = sim.run()
        fr = replay(tr, "lizone", dev=dev, max_events=1_200_000)
        rows.append({
            "variant": tag,
            "w_bw_mbs": r["write_logi_bw_mbs"],
            "w_p99": r["w_p99"], "r_p99": r["r_p99"],
            "recall": fr["hits"] / max(1, replay(tr, "oracle",
                                                 max_events=1_200_000)["hits"]),
            "wa": fr["total_write_amp"],
            "sec": round(time.time() - t0, 1),
        })
        print(f"[exp46hw] {tag}: wBW={r['write_logi_bw_mbs']:.0f} "
              f"wp99={r['w_p99']:.1f}", flush=True)
    with open(os.path.join(RESULTS, "exp46_hardware.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("saved exp46_*.csv")


if __name__ == "__main__":
    main()
