"""
run_q4.py -- rebuttal A-6: high-utilization, continuous-GC steady state,
and the comparison against a global index under the SAME DRAM budget.

Reviewer A's charge: "all workloads occupy ~1% of the device, no GC runs,
every cache holds the whole index -- a global in-DRAM table would fit in
the same DRAM."  This experiment runs BOTH regimes in one harness:

  low  : occupancy held at ~2%  (the reviewer's regime)
  high : occupancy held at ~65% under continuous zone churn (seal/reset/
         GC), host version expiry every version_writes writes

Arms (same trace, same protocol, same adaptive occupancy controller):
  lizone       full design (r=2, LSH routing, targeted migration)
  prefix       same with fingerprint-prefix routing (Q2 sealing curve)
  global=cap    PageFTL-style exact global index on flash + DRAM cache
                capped at the routed design's MEASURED steady DRAM
                (equal-footprint, Reviewer A's own criterion); a 128 MB
                reference cap shows when the index plainly fits
  nodedup      plain ZNS baseline

Reported per arm at steady state: recall, metadata read amplification
(B/req), DRAM working set, total write amplification, GC/migration
volume, locked-zone fraction; plus SimHash-vs-prefix sealed-recall.

Output: results_q4/q4_utilization.csv (+ printed summary)
"""
import csv
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.lifecycle import (LifecycleSim, GlobalIndexLife, NoDupLife)  # noqa
from lzsim.traces import build_trace  # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402
from lzsim.systems import DEAD, RESET_PENDING  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_q4")
os.makedirs(RESULTS, exist_ok=True)

PAGE = 4096
SMOKE = os.environ.get("LZ_SMOKE") == "1"

N_ZONES = 192                 # 128 MB zones -> 24 GB device (as e47/e12)
CFG = {
    "container": dict(vw=700_000, nvers=9),
    "backup":    dict(vw=400_000, nvers=15),
}
REGIMES = {"high": 0.65, "low": 0.02}
WARM = {"container": 6, "backup": 11}       # steady state = versions after
DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)


def ensure_trace(name):
    try:
        from lzsim.traces import Trace
        return Trace(name)
    except FileNotFoundError:
        build_trace(name)
        from lzsim.traces import Trace
        return Trace(name)


# ---------------------------------------------------------------------------
def snapshot(sim, wl, regime, arm, ver, writes, truth, keep):
    n_zones = sim.n_zones_device
    occ = 1.0 - sim.free_zones() / n_zones
    true_dups = writes - len(truth)
    ra = getattr(sim, "md_read_pages", 0.0) * PAGE / max(1, writes)
    if isinstance(sim, GlobalIndexLife):
        dram = sim.dram_working_set() / 2**20
        idx = sim.index_bytes() / 2**20
        extra = dict(index_mb=round(idx, 1),
                     cache_hit_frac=round(sim.n_cache_hits /
                                          max(1, sim.n_cache_hits +
                                              sim.n_cache_misses), 3))
    elif isinstance(sim, LifecycleSim):
        dram = sim.dram_breakdown()["total"] / 2**20
        extra = dict(index_mb="", cache_hit_frac="")
    else:
        dram = 0.0
        extra = dict(index_mb="", cache_hit_frac="")
    alive = [z for z in getattr(sim, "zones", {}).values()
             if getattr(z, "state", 0) != 2]
    locked = sum(1 for z in alive if getattr(z, "incoming_refs", 0) > 0
                 and (z.state == RESET_PENDING or z.live_slots == 0))
    nand_w = (getattr(sim, "data_pages", 0.0) * PAGE +
              getattr(sim, "md_write_bytes", 0.0) +
              getattr(sim, "gc_desc_updates", 0) * 16 +
              getattr(sim, "gc_migrated_pages", 0) * PAGE)
    row = {
        "workload": wl, "regime": regime, "arm": arm, "version": ver,
        "writes": writes, "occupancy": round(occ, 3), "keep": round(keep, 3),
        "exhausted": 0,
        "recall": round(getattr(sim, "n_hits", 0) / max(1, true_dups), 4),
        "hit_rate": round(getattr(sim, "n_hits", 0) / max(1, writes), 4),
        "ra_b_per_req": round(ra, 1),
        "dram_mb": round(dram, 2),
        "wa_total": round(nand_w / max(1, writes * PAGE), 4),
        "locked_frac": round(locked / max(1, len(alive)), 4),
        "gc_count": getattr(sim, "gc_count", 0),
        "mig_gb": round(getattr(sim, "gc_migrated_pages", 0) * PAGE
                        / 2**30, 2),
        "host_resets": getattr(sim, "n_host_resets", 0),
        "sealed_hits": getattr(sim, "hit_source_sealed", 0),
        "fn_sealed": getattr(sim, "fn_sealed_src", 0),
        "fn_active": getattr(sim, "fn_active_src", 0),
        **extra,
    }
    return row


def run_arm(sim_ctor, wl, regime, target, vw, nvers, dram_cap=0,
            nowave=False):
    sim = sim_ctor()
    tr = ensure_trace(wl)
    rng = random.Random(7)
    truth = set()
    keep = 0.10
    rows = []
    writes = 0
    n_in_ver = 0
    ver = 0
    cap = nvers * vw * 2
    i = 0
    while i < cap and ver < nvers:
        op, fp, L = tr.event(i % len(tr))
        i += 1
        if op:
            continue
        if sim.free_zones() <= 0 and not isinstance(sim, NoDupLife):
            rows[-1]["exhausted"] = 1 if rows else 0
            break
        sim.write(fp, L)
        truth.add(fp)
        writes += 1
        n_in_ver += 1
        if nowave:
            # reviewer's regime: a short fill with no expiry and no GC --
            # occupancy stays in the single digits, caches hold everything
            if writes >= vw * 3:
                rows.append(snapshot(sim, wl, regime,
                                     ARM_OF[sim_ctor], 1, writes, truth,
                                     keep))
                break
            continue
        if n_in_ver >= vw:
            ver += 1
            # retention = 2 versions: bands survive deterministically one
            # extra version, which removes the keep-lottery asymmetry
            # between placement strategies (verified: global vs routed
            # recall then matches theory, exact >= routed)
            sim.discard_wave(keep, rng, old_frac=1.0,
                             retention_events=2 * vw, wave=ver)
            sim.host_reset_fully_dead()
            # occupancy controller: hold the regime's target
            occ = 1.0 - sim.free_zones() / sim.n_zones_device
            if occ > target + 0.03:
                keep *= 0.72
            elif occ < target - 0.03:
                keep = min(0.20, keep * 1.3)
            rows.append(snapshot(sim, wl, regime, ARM_OF[sim_ctor], ver,
                                 writes, truth, keep))
            print(f"[Q4/{wl}/{regime}/{ARM_OF[sim_ctor]}] v{ver}: "
                  f"occ={rows[-1]['occupancy']:.2f} "
                  f"recall={rows[-1]['recall']:.3f} "
                  f"RA={rows[-1]['ra_b_per_req']:.0f}B "
                  f"DRAM={rows[-1]['dram_mb']:.1f}MB "
                  f"WA={rows[-1]['wa_total']:.3f} gc={rows[-1]['gc_count']}",
                  flush=True)
            n_in_ver = 0
    return rows, sim


ARM_OF = {}


def main():
    all_rows = []
    for wl, cfg in CFG.items():
        vw = 40_000 if SMOKE else cfg["vw"]
        nvers = 3 if SMOKE else cfg["nvers"]
        warm = 1 if SMOKE else WARM[wl]

        # 1. routed design at both occupancies (lsh); prefix at high only
        def mk_lsh():
            return LifecycleSim(dev=DEV(), r=2, n_zones_device=N_ZONES,
                                gc_watermark_lo=0.15, gc_watermark_hi=0.40,
                                migrate=True, max_active=16,
                                route_mode="lsh", track_locus=True)
        ARM_OF[mk_lsh] = "lizone"
        def mk_prefix():
            return LifecycleSim(dev=DEV(), r=2, n_zones_device=N_ZONES,
                                gc_watermark_lo=0.15, gc_watermark_hi=0.40,
                                migrate=True, max_active=16,
                                route_mode="prefix", track_locus=True)
        ARM_OF[mk_prefix] = "lizone-prefix"

        steady_dram = {}
        for regime, target in REGIMES.items():
            rows, sim = run_arm(mk_lsh, wl, regime, target, vw, nvers,
                                nowave=(regime == "low"))
            all_rows += rows
            ss = [r for r in rows if r["version"] > warm]
            if ss:
                steady_dram[regime] = sum(r["dram_mb"] for r in ss) / len(ss)

        if not SMOKE:
            rows, _ = run_arm(mk_prefix, wl, "high", REGIMES["high"],
                              vw, nvers)
            all_rows += rows

        # 2. global index at the SAME DRAM budget (equal footprint), both
        #    regimes; plus a 128 MB reference cap at high
        for regime in REGIMES:
            cap_mb = steady_dram.get(regime, 16.0)   # equal-footprint
            def mk_global(cap_mb=cap_mb):
                return GlobalIndexLife(
                    dev=DEV(), n_zones_device=N_ZONES,
                    gc_watermark_hi=0.40,
                    dram_cache_bytes=int(cap_mb * 2**20))
            ARM_OF[mk_global] = f"global-equaldram"
            rows, _ = run_arm(mk_global, wl, regime, REGIMES[regime],
                              vw, nvers, nowave=(regime == "low"))
            all_rows += rows
        if not SMOKE:
            # small-budget controller (4 MB): the regime where the index
            # plainly does not fit even on this 24 GB device
            def mk_global4():
                return GlobalIndexLife(dev=DEV(), n_zones_device=N_ZONES,
                                       gc_watermark_hi=0.40,
                                       dram_cache_bytes=4 * 2**20)
            ARM_OF[mk_global4] = "global-4mb"
            rows, _ = run_arm(mk_global4, wl, "high", REGIMES["high"],
                              vw, nvers)
            all_rows += rows
        if not SMOKE:
            def mk_global128():
                return GlobalIndexLife(dev=DEV(), n_zones_device=N_ZONES,
                                       gc_watermark_hi=0.40,
                                       dram_cache_bytes=128 * 2**20)
            ARM_OF[mk_global128] = "global-128mb"
            rows, _ = run_arm(mk_global128, wl, "high", REGIMES["high"],
                              vw, nvers)
            all_rows += rows

        # 3. NoDedup baseline at high occupancy
        def mk_nodup():
            return NoDupLife(dev=DEV(), n_zones_device=N_ZONES,
                             gc_watermark_hi=0.40)
        ARM_OF[mk_nodup] = "nodedup"
        rows, _ = run_arm(mk_nodup, wl, "high", REGIMES["high"], vw, nvers)
        all_rows += rows

    with open(os.path.join(RESULTS, "q4_utilization.csv"), "w",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)
    print(f"saved q4_utilization.csv ({len(all_rows)} rows)")

    # steady-state summary
    print("=" * 72)
    warm = 1 if SMOKE else None
    for wl in CFG:
        for regime in REGIMES:
            for arm in ("lizone", "lizone-prefix", "global-equaldram",
                        "global-128mb", "global-4mb", "nodedup"):
                w0 = warm or WARM[wl]
                ss = [r for r in all_rows
                      if r["workload"] == wl and r["regime"] == regime
                      and r["arm"] == arm and r["version"] > w0]
                if not ss:
                    continue
                m = lambda k: sum(r[k] for r in ss) / len(ss)
                sh = m("sealed_hits")
                sealed_recall = sh / max(1.0, sh + m("fn_sealed"))
                print(f"[Q4] {wl:9s} {regime:4s} {arm:17s} "
                      f"occ={m('occupancy'):.2f} recall={m('recall'):.3f} "
                      f"RA={m('ra_b_per_req'):7.0f}B DRAM={m('dram_mb'):6.1f}MB "
                      f"WA={m('wa_total'):.3f} gc={ss[-1]['gc_count']} "
                      f"sealed_recall={sealed_recall:.3f}")


if __name__ == "__main__":
    main()
