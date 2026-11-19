"""
run_q3.py -- rebuttal Q3: sustained 100 % write pressure, zero idle time.

Two coupled measurements on the container workload (the highest-pressure
dedup-rich trace):

  A. functional steady state (LifecycleSim "crisis" mode): the device is
     small (96 zones of 128 MB), the host expires whole versions every
     200k writes, and there is NO idle-time migration pass -- the only
     reclamation path is pressure-triggered GC with targeted pinned-zone
     migration (Sec 3.3).  We report steady-state (last 5 versions):
       * total write amplification
           WA = (data + metadata + migration-copy + descriptor-update
                 NAND bytes) / logical bytes written
       * locked-zone fraction = (pinned ∪ reset-pending) / alive zones
       * migration volume per logical GB (the background traffic demand)

  B. foreground throughput under a reserved migration budget
     (TimingSim + bg_reserve_frac): the closed-loop QD-32 run saturates
     the controller with writes; the migration engine is rate-limited to
     5 % of the aggregate plane bandwidth and interleaves its pages on
     the same plane/channel resources (timing.py:_bg_migrate_step).
     Reported: write bandwidth drop vs the same run with no background
     reservation, plus the implied migration drain rate in MB/s.

Output: results_q3/q3_sustained.csv (+ printed summary)
"""
import csv
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.lifecycle import LifecycleSim
from lzsim.systems import DEAD, RESET_PENDING  # noqa: E402
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

from lzsim.timing import TimingSim    # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_q3")
os.makedirs(RESULTS, exist_ok=True)

PAGE = 4096
SMOKE = os.environ.get("LZ_SMOKE") == "1"

# sustained-pressure configurations.  backup is the cross-zone-heavy
# stream whose duplicates come from SEALED zones, so incoming references
# pin old zones (the e12 data showed container's duplicates stay in the
# active window and never pin the device).  container runs the same
# protocol as an honest negative control.
PRESSURE_CFGS = [
    # (workload, n_zones, version_writes, n_versions, warmup)
    ("container", 192, 700_000, 9, 4),
    ("backup",    192, 400_000, 18, 12),
] if not SMOKE else [
    ("container", 192, 40_000, 3, 1),
    ("backup",    192, 40_000, 3, 1),
]
KEEP = 0.1                   # aggressive version expiry (as e12)
RESERVE = float(os.environ.get("Q3_RESERVE", "0.05"))
TIMING_EVENTS = 1_200_000 if not SMOKE else 60_000

DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)
V7F = dict(skew=True, precopy=True, hot_reads=16, dref_prefetch=True,
           redirect_cap=2048, stage1_prefetch=True, zone_locks=True)


def snapshot(sim: LifecycleSim, ver: int, writes: int) -> dict:
    alive = [z for z in sim.zones.values() if z.state != DEAD]
    pending = [z for z in alive if z.state == RESET_PENDING]              # RESET_PENDING
    # "locked" = reclamation-blocked zones: reset deferred, or fully dead
    # but still externally referenced (cannot reset, not yet pending)
    blocked = [z for z in alive
               if z.state != RESET_PENDING and z.incoming_refs > 0
               and z.live_slots == 0]
    locked = {z.zid for z in pending} | {z.zid for z in blocked}
    md_w_bytes = (sim.md_write_bytes +                  # INS/DREF + merges
                  sim.gc_desc_updates * 16)             # UREF/DREF rewrites
    nand_w_bytes = (sim.data_pages * PAGE + md_w_bytes +
                    sim.gc_migrated_pages * PAGE)       # migration copies
    logical_bytes = writes * PAGE
    return {
        "version": ver, "writes": writes,
        "wa_total": round(nand_w_bytes / logical_bytes, 4),
        "locked_frac": round(len(locked) / max(1, len(alive)), 4),
        "alive_zones": len(alive),
        "locked_zones": len(locked),
        "pending_zones": len(pending),
        "blocked_dead_zones": len(blocked),
        "free_zones": sim.free_zones(),
        "mig_pages": sim.gc_migrated_pages,
        "mig_gb": round(sim.gc_migrated_pages * PAGE / 2**30, 3),
        "mig_mb_per_logical_gb": round(
            sim.gc_migrated_pages * PAGE / max(1.0, writes * PAGE / 2**30),
            2),
        "gc_count": sim.gc_count,
        "targeted_migs": sim.n_targeted_migs,
        "gc_blocked_pinned": sim.gc_blocked_pinned,
        "exhausted": int(sim.free_zones() <= 0),
    }


def part_a() -> list:
    """Sustained-pressure functional runs (crisis mode: no idle passes).

    Each configuration replays the workload as a stream of versions: every
    version_writes writes the host expires the previous band entirely
    (KEEP=0.1, old_frac=1.0) and resets every fully-dead zone.  There is
    NO idle-time migration pass; the only reclamation path beyond host
    resets is pressure-triggered GC with targeted pinned-zone migration
    (Sec 3.3)."""
    rows = []
    for (wl, n_zones, vw, nvers, warmup) in PRESSURE_CFGS:
        sim = LifecycleSim(dev=DEV(), r=2, n_zones_device=n_zones,
                           gc_watermark_lo=0.15, gc_watermark_hi=0.40,
                           migrate=True, max_active=16)
        tr = ensure_trace(wl)
        if tr is None:
            continue
        rng = random.Random(7)
        n_in_ver = 0
        writes = 0
        ver = 0
        exhausted = 0
        i = 0
        cap = nvers * vw * 2
        while i < cap:
            op, fp, L = tr.event(i % len(tr))   # wrap: keep pinning after
            i += 1                              # the trace ends
            if op:
                continue
            if sim.free_zones() <= 0:
                exhausted = 1
                print(f"[Q3A/{wl}] EXHAUSTED at {writes} writes", flush=True)
                break
            sim.write(fp, L)
            writes += 1
            n_in_ver += 1
            if n_in_ver >= vw:
                ver += 1
                sim.discard_wave(KEEP, rng, old_frac=1.0,
                                 retention_events=vw, wave=ver)
                sim.host_reset_fully_dead()
                snap = snap_one(sim, wl, ver, writes, warmup)
                snap["exhausted"] = exhausted
                rows.append(snap)
                print(f"[Q3A/{wl}] v{ver}: WA={snap['wa_total']:.3f} "
                      f"locked={snap['locked_frac']*100:.1f}% "
                      f"free={snap['free_zones']} gc={snap['gc_count']} "
                      f"mig={snap['mig_gb']:.2f}GB", flush=True)
                n_in_ver = 0
                if ver >= nvers:
                    break
        if n_in_ver and not rows or rows[-1]["version"] != ver:
            rows.append(snap_one(sim, wl, ver, writes, warmup))
    return rows


def snap_one(sim: LifecycleSim, wl: str, ver: int, writes: int,
             warmup: int) -> dict:
    snap = snapshot(sim, ver, writes)
    snap["workload"] = wl
    snap["warmup"] = int(ver <= warmup)
    return snap


def part_b() -> list:
    """Sustained writes vs the same run with a 5 % reserved
    background-migration budget.  QD 32 is the paper default (latency
    bound); QD 256 saturates the plane resources, which is where a
    bandwidth reservation must show its cost."""
    tr = ensure_trace("container")
    rows = []
    for qd in (32, 256):
        for reserve in (0.0, RESERVE):
            ts = TimingSim(tr, "v9", dev=DEV(sha_hw=True), qd=qd,
                           max_events=TIMING_EVENTS,
                           **V7F, bg_reserve_frac=reserve)
            r = ts.run()
            rows.append({
                "qd": qd,
                "reserve": reserve,
                "write_mbs": round(r["write_logi_bw_mbs"], 1),
                "phys_mbs": round(r["write_phys_bw_mbs"], 1),
                "w_p50": round(r["w_p50"], 1),
                "w_p99": round(r["w_p99"], 1),
                "sim_s": round(r["sim_time_s"], 2),
                "bg_mig_pages": r["bg_mig_pages"],
                "bg_mig_mbs": round(r["bg_mig_pages"] * PAGE /
                                    r["sim_time_s"] / 1e6, 1),
                "hit_rate": round(r["func"]["hit_rate"], 4),
            })
            print(f"[Q3B] qd={qd} reserve={reserve}: "
                  f"write={rows[-1]['write_mbs']} MB/s "
                  f"p99={rows[-1]['w_p99']}us bg={rows[-1]['bg_mig_mbs']}"
                  f"MB/s", flush=True)
    return rows


def main():
    a = part_a()
    with open(os.path.join(RESULTS, "q3_functional.csv"), "w",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(a[0].keys()))
        w.writeheader()
        w.writerows(a)

    b = []
    if os.environ.get("Q3_PARTS", "ab") in ("ab", "b"):
        b = part_b()
        with open(os.path.join(RESULTS, "q3_timing.csv"), "w",
                  newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(b[0].keys()))
            w.writeheader()
            w.writerows(b)

    for (wl, _, _, _, warmup) in PRESSURE_CFGS:
        ss = [r for r in a if r.get("workload") == wl
              and not r.get("warmup") and not r.get("exhausted")]
        if not ss:
            print(f"[Q3] {wl}: no steady-state versions (check exhaustion)")
            continue
        wa = sum(r["wa_total"] for r in ss) / len(ss)
        lock = sum(r["locked_frac"] for r in ss) / len(ss)
        migd = sum(r["mig_mb_per_logical_gb"] for r in ss) / len(ss)
        exh = any(r.get("exhausted") for r in a if r.get("workload") == wl)
        print(f"[Q3] {wl}: WA={wa:.3f}x locked={lock*100:.1f}% "
              f"mig={migd:.1f}MB/logicalGB exhausted={int(exh)}")
    if not b:
        print("saved q3_functional.csv (timing skipped)")
        return
    for qd in (32, 256):
        base = next(r for r in b if r["qd"] == qd and r["reserve"] == 0.0)
        rsv = next(r for r in b if r["qd"] == qd and r["reserve"] > 0)
        drop = 1 - rsv["write_mbs"] / max(1e-9, base["write_mbs"])
        print(f"[Q3] QD{qd}: write BW drop {drop*100:.1f}% "
              f"({base['write_mbs']} -> {rsv['write_mbs']} MB/s), "
              f"bg drain {rsv['bg_mig_mbs']} MB/s")
    print("saved q3_functional.csv + q3_timing.csv")


if __name__ == "__main__":
    main()
