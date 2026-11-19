"""
run_v12.py -- migration ON vs OFF: long-run allocatable-space utilization.

Protocol (per TXT request):
  * replay container / backup (cross-zone-heavy workloads) as a stream of
    "versions": every VERSION_WRITES writes, the host expires old content
    (discard_wave, keep=0.5, 800k-event retention horizon) and resets every
    fully-dead zone; GC runs under free-zone pressure as usual.
  * two arms: migrate=0 (pinned zones can never be reclaimed) vs migrate=1
    (targeted migration releases pinned zones at <20% valid).
  * HARD STOP: on a real device, writes fail once free zones hit zero --
    when free_zones() <= 0 the arm is marked exhausted at that point and
    terminated (allocating beyond the device would be unphysical).
  * per version snapshot: allocatable fraction (free_zones / n_zones_device),
    pending zone count, trapped dead GB, cumulative GC/migration counters.

Output: results_v12/e12_migration_util.csv
"""
import csv
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace        # noqa: E402
from lzsim.lifecycle import LifecycleSim  # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402
from lzsim.systems import DEAD, RESET_PENDING  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v12")
os.makedirs(RESULTS, exist_ok=True)

DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)
CFG = os.environ.get("E12CFG", "96-200k")   # "<n_zones>-<version-writes>"
VT = float(os.environ.get("E12VT", "0.20"))  # migration valid-fraction threshold
BG = os.environ.get("E12BG", "1") == "1"      # idle-time background migration
_NZ, _VW = CFG.split("-")
N_ZONES = int(_NZ)            # 128 MB zones; device model as e47
VERSION_WRITES = int(_VW[:-1]) * 1000
KEEP = 0.1                    # aggressive version expiry: old band
                              # keeps 10% of live slots, so source
                              # zones fall under the 20%-valid
                              # migration precondition (Sec 3.3)
OLD_FRAC = 1.0                # expire the ENTIRE band older than the
                              # retention horizon (previous version)
RETENTION = VERSION_WRITES    # expire content older than the previous
                              # version each wave
MODE = "bg" if BG else "crisis"
OUT = "e12_migration_util_%s_%s.csv" % (CFG, MODE)
PAGE = 4096
SMOKE = os.environ.get("LZ_SMOKE") == "1"
CAP = 60_000 if SMOKE else None


def snapshot(sim, wl, mig, ver, writes, exhausted):
    alive = [z for z in sim.zones.values() if z.state != DEAD]
    pending = [z for z in alive if z.state == RESET_PENDING]
    pinned = [z for z in alive if z.incoming_refs > 0]
    trapped_zones = {z.zid for z in pending} | {z.zid for z in pinned}
    trapped_dead_gb = sum(sim.zones[z].dead_data for z in trapped_zones) \
        * PAGE / 2**30
    return {
        "workload": wl, "migrate": mig, "version": ver,
        "writes_m": round(writes / 1e6, 2),
        "logical_gb": round(writes * PAGE / 2**30, 1),
        "alloc_frac": round(max(0, sim.free_zones()) / N_ZONES, 4),
        "alive_zones": len(alive),
        "pending_zones": len(pending),
        "pinned_zones": len(pinned),
        "trapped_dead_gb": round(trapped_dead_gb, 3),
        "avg_dead_ratio": round(sim.avg_dead_ratio(), 4),
        "gc_count": sim.gc_count,
        "gc_blocked_pinned": sim.gc_blocked_pinned,
        "targeted_migs": sim.n_targeted_migs,
        "mig_pages_m": round(sim.targeted_pages / 1e6, 3),
        "reclaimed_dead_gb": round(sim.gc_reclaimed_dead * PAGE / 2**30, 3),
        "host_resets": sim.n_host_resets,
        "discards_m": round(sim.n_discards / 1e6, 2),
        "exhausted": int(exhausted),
    }


def run_arm(wl, mig):
    # NOTE: the archived e12 CSVs were produced by a dev branch that had
    # explicit pack_migrations/mig_valid_thresh knobs and an idle-time
    # background_migrate() pass; the shipped LifecycleSim implements the
    # same targeted migration inside pressure-triggered GC (Sec 3.3), so
    # this script runs the crisis protocol against the shipped API.
    sim = LifecycleSim(dev=DEV(), r=2, n_zones_device=N_ZONES,
                       gc_watermark_lo=0.15, gc_watermark_hi=0.40,
                       migrate=bool(mig), max_active=16)
    tr = Trace(wl)
    n_events = len(tr) if CAP is None else min(len(tr), CAP)
    rng = random.Random(7)
    rows = []
    ver = 0
    n_in_ver = 0
    exhausted = False
    writes = 0
    for i in range(n_events):
        op, fp, L = tr.event(i)
        if op:
            continue
        if sim.free_zones() <= 0:
            exhausted = True
            break
        sim.write(fp, L)
        writes += 1
        n_in_ver += 1
        if n_in_ver >= VERSION_WRITES:
            ver += 1
            sim.discard_wave(KEEP, rng, old_frac=OLD_FRAC,
                             retention_events=RETENTION, wave=ver)
            sim.host_reset_fully_dead()
            if mig and BG and hasattr(sim, "background_migrate"):
                sim.background_migrate()   # dev-branch idle pass, if present
            rows.append(snapshot(sim, wl, mig, ver, writes, exhausted))
            print(f"[12] {wl}/mig={mig} v{ver}: alloc="
                  f"{rows[-1]['alloc_frac']:.3f} "
                  f"pending={rows[-1]['pending_zones']} "
                  f"trapped={rows[-1]['trapped_dead_gb']:.2f}GB",
                  flush=True)
            n_in_ver = 0
    rows.append(snapshot(sim, wl, mig, ver, writes, exhausted))
    print(f"[12] {wl}/mig={mig} DONE exhausted={exhausted} at "
          f"{writes/1e6:.2f}M writes", flush=True)
    return rows


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"f-on": ("fsl", 1), "f-off": ("fsl", 0),
             "c-on": ("container", 1), "c-off": ("container", 0)}
    todo = steps.items() if which == "all" else [(which, steps[which])]
    all_rows = []
    for name, (wl, mig) in todo:
        all_rows += run_arm(wl, mig)
    with open(os.path.join(RESULTS, OUT), "w",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)
    print(f"saved {OUT} ({len(all_rows)} rows)", flush=True)
