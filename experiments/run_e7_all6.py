"""run_e7_all6.py -- e7_capacity for all six workloads (Fig 9a now shows
all six LiZone DRAM lines + PageFTLDedup + HostDedup references)."""
import os, sys, csv
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import lzsim.optimizations
from lzsim.traces import Trace
from lzsim.replay import replay
from lzsim.device import DeviceConfig

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results_v11")
DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)
MEV = 3_000_000

caps = [1024, 2048, 4096]
wls = ["fsl", "container", "vm", "backup", "rocksdb", "cloud"]
rows = []
for wl in wls:
    tr = Trace(wl)
    for cap in caps:
        r = replay(tr, "v9", dev=DEV(capacity_gb=cap), max_events=MEV)
        dram_mb = r.get("dram_total_mb", r.get("dram_mb", 0.0))
        _d = r.get("dram", {}) or {}
        def _mb(b):
            return round(b / 1048576.0, 2)
        if not dram_mb:
            dram_mb = _mb(_d.get("total", 0))
        rows.append({
            "workload": wl, "capacity_gb": cap,
            "lz_dram_mb": round(float(dram_mb), 2),
            "lz_index_mb": _mb(_d.get("active_zone_filters", 0) + _d.get("working_index", 0)
                               + _d.get("delta_logs", 0) + _d.get("pinned_chain_filters", 0)
                               + _d.get("route_table", 0)),
            "lz_cache_mb": _mb(_d.get("caches", 0)),
            "lz_ref_mb": _mb(_d.get("incoming_ref_counts", 0) + _d.get("ref_accumulator", 0)),
            "zones_touched": _d.get("n_active_zones", 0),
            "dedup": r.get("dedup_ratio", 0.0),
            "globalindex_dram_gb_analytic": cap * 40 / 4096 * 1024**3 / 1024**3 * (1024**3/4096) / (1024*1024*1024/4096) * (40 * cap * 1024**3 / 4096 / 1024**3),
            "hostdedup_dram_gb_analytic": cap * 20 * 1024**3 / 4096 / 1024**3,
        })
        print(f"[e7] {wl}/{cap}GB dram={dram_mb:.1f}MB", flush=True)

out = os.path.join(RESULTS, "e7_capacity_all6.csv")
with open(out, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)
print(f"saved e7_capacity_all6.csv ({len(rows)} rows)")
