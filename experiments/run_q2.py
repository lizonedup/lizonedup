"""
run_q2.py -- rebuttal Q2: SimHash vs fingerprint-prefix routing (r=2,
identical DRAM lookup budget) on container + FSL, r=2.

Arms: oracle (recall denominator), v9 (SimHash/LSH routing, r=2),
v9-prefix (top bits of the content fingerprint as the route bucket, r=2).
Both routed arms probe exactly r=2 candidate zones and pay the same
metadata traffic; only the bucket function differs.  The route digest
cost is 4 us/page for SimHash (software, Sec 4.1) and 0 for prefix
(folded into the fingerprint the controller already computes) -- both
negligible against the 200 us/page NAND program.

Output: results_q2/q2_routing.csv  (+ printed summary)
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.replay import replay       # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

import os
# --- one-click: materialize the trace if the fresh clone has none -------
from lzsim.traces import build_trace

def ensure_trace(name):
    """Load a trace, building it on first use (one-click reproducibility).
    'fsl' needs the public FSL-Homes fingerprint file (LZ_FSL_FP env var);
    without it the caller skips that workload with a pointer to the
    committed CSV."""
    try:
        from lzsim.traces import Trace
        return Trace(name)
    except FileNotFoundError:
        if name == "fsl":
            fsl_path = os.environ.get("LZ_FSL_FP")
            if not fsl_path:
                return None
            build_trace("fsl", fsl_path=fsl_path)
        else:
            build_trace(name)
        from lzsim.traces import Trace
        return Trace(name)


RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_q2")
os.makedirs(RESULTS, exist_ok=True)

# default: full trace length (matches the committed q2_routing.csv);
# override with Q2_MEV=<events> for a faster partial run
MEV = int(os.environ.get("Q2_MEV", "0")) or None
DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)

ARMS = ("oracle", "v9", "v9-prefix")


def main():
    rows = []
    for wl in ("container", "fsl"):
        tr = ensure_trace(wl)
        if tr is None:
            print("[Q2] fsl skipped: set LZ_FSL_FP=<FSL-Homes fingerprint "
                  "file> to build the FSL trace (see README); the committed "
                  "results_q2/q2_routing.csv carries the fsl rows", flush=True)
            continue
        n = min(MEV, len(tr)) if MEV else len(tr)
        oh = replay(tr, "oracle", max_events=n)["hits"]
        for sk in ARMS:
            r = replay(tr, sk, dev=DEV(), track_locus=True, max_events=n)
            # systems.summary() exposes the sealed-source hit fraction
            sealed_hits = int(round(
                r.get("hit_source_sealed_frac", 0.0) * r["hits"]))
            row = {
                "workload": wl, "arm": sk, "system": r["system"],
                "writes": r["writes"], "oracle_hits": oh,
                "hits": r["hits"],
                "recall": round(r["hits"] / max(1, oh), 4),
                "dedup": round(r["dedup_ratio"], 3),
                "phys_red": round(1 - r["data_pages"] / max(1, r["writes"]),
                                  4),
                "oracle_phys_red": round(1 - oh / max(1, r["writes"]), 4),
                "hit_src_sealed": sealed_hits,
                "sealed_hit_frac": round(sealed_hits / max(1, r["hits"]), 4),
                "fn_sealed": r.get("fn_sealed_src", 0),
                "fn_active": r.get("fn_active_src", 0),
                "wa_total": round(r.get("total_write_amp", 0.0), 4),
                "probe_avg": round(r.get("probe_avg", 0.0), 3),
                "ra_b_per_req": round(r.get("md_read_bytes_per_req", 0.0), 2),
                "routing_fn": r.get("routing_fn_count", 0),
                "cold_hits": r.get("cache_wi_hit", 0),
            }
            rows.append(row)
            print(f"[Q2] {wl}/{sk}: recall={row['recall']:.4f} "
                  f"physred={row['phys_red']:.4f} "
                  f"sealed_hits={sealed_hits} fn_sealed={row['fn_sealed']} "
                  f"fn_active={row['fn_active']}", flush=True)
    with open(os.path.join(RESULTS, "q2_routing.csv"), "w",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("saved q2_routing.csv")

    # headline: phys-red gap and its sealed-recall attribution
    for wl in ("container", "fsl"):
        d = {r["arm"]: r for r in rows if r["workload"] == wl}
        if "v9" not in d:
            continue
        lsh, pfx = d["v9"], d["v9-prefix"]
        print(f"[Q2] {wl}: SimHash phys-red {lsh['phys_red']*100:.1f}% vs "
              f"prefix {pfx['phys_red']*100:.1f}% "
              f"(gap {(lsh['phys_red']-pfx['phys_red'])*100:.1f} pp; "
              f"recall {lsh['recall']*100:.1f}% vs {pfx['recall']*100:.1f}%)",
              flush=True)


if __name__ == "__main__":
    main()
