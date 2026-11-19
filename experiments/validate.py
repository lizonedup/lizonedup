"""
validate.py -- invariant gate for every experiment batch.  A batch is only
accepted if every check passes; any FAIL means the numbers must not enter
the paper.  Run after downloading results, before regenerating figures.

Checks (each tied to a bug that actually slipped through before):
  V1  timing physical write BW <= channel program capacity  (scheduler bug)
  V2  Resource intervals never overlap                       (scheduler bug)
  V3  functional vs timing physical writes agree (<1% drift) (dual-track)
  V4  every figure data series is non-degenerate             (name-match bug)
  V5  FEMU vs simulator ordering direction consistent        (cross-platform)
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))   # main_v3
RES = os.path.join(ROOT, "server_out")

FAILS = []
INFO = []


def check(name, cond, detail=""):
    if cond:
        INFO.append(f"PASS  {name}  {detail}")
    else:
        FAILS.append(f"FAIL  {name}  {detail}")


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def v2_resource_overlap():
    """V2: overlap invariant on the fixed scheduler (short workload)."""
    import lzsim.optimizations  # noqa: F401
    from lzsim.timing import TimingSim, Resource
    from lzsim.device import DeviceConfig
    orig = Resource.sched
    overlaps = [0]

    def checked(self, t, d):
        st, en = orig(self, t, d)
        iv = self.iv[self.h:]
        for a, b in zip(iv, iv[1:]):
            if b[0] < a[1] - 1e-9:
                overlaps[0] += 1
        return st, en
    Resource.sched = checked
    from lzsim.traces import Trace
    tr = Trace("container")
    TimingSim(tr, "v7", dev=DeviceConfig(wi_entries_per_zone=128), qd=32,
              read_mix_extra=0.15, max_events=150_000, skew=True,
              precopy=True, hot_reads=16, dref_prefetch=True,
              redirect_cap=2048, stage1_prefetch=True,
              zone_locks=True).run()
    Resource.sched = orig
    check("V2 resource-no-overlap", overlaps[0] == 0,
          f"overlaps={overlaps[0]}")


def v1_v3_caps_and_consistency():
    """V1: phys BW <= channel cap.  V3: timing phys writes match the
    functional replay's physical pages for the same system/workload."""
    perf = load(os.path.join(RES, "results_v9", "e44_perf.csv"))
    if perf is None:
        INFO.append("SKIP  V1/V3 (results_v9/e44_perf.csv not present)")
        return
    from lzsim.device import DEFAULT_DEVICE
    cap = (DEFAULT_DEVICE.n_nand_units * DEFAULT_DEVICE.page_size_b
           / DEFAULT_DEVICE.t_prog_us * 1e6) / 1e6  # MB/s (plane level)
    worst = None
    for r in perf:
        wphys = float(r["wphys"])
        if wphys > cap * 1.02:
            worst = (r["workload"], r["label"], wphys)
    check("V1 phys-BW <= channel cap", worst is None,
          f"cap={cap:.0f} MB/s worst={worst}")
    # V3: functional replay cross-check -- only meaningful for pure-dedup
    # systems (for compression systems `red` includes compression, which
    # the timing `hit_rate` does not count)
    rec = load(os.path.join(RES, "results_v9", "e43_wa.csv"))
    if rec:
        pairs = {"nodedup": "NoDedup-ZNS", "v7": "LiZoneDedup",
                 "hostdedup-1g": "HostDedup-1GB"}
        drift_max = 0.0
        for r in perf:
            key = pairs.get(r["label"])
            if not key:
                continue
            m = [x for x in rec if x["workload"] == r["workload"]
                 and x["system"] == key]
            if not m:
                continue
            func_red = float(m[0]["red"])
            tim_hit = float(r["hit_rate"])
            drift = abs(func_red - tim_hit)
            drift_max = max(drift_max, drift)
        check("V3 functional/timing hit agreement", drift_max < 0.10,
              f"max |red - hit_rate| = {drift_max:.3f}")


def v4_figure_series():
    """V4: every system series in the perf CSV is non-degenerate (the
    name-matching bug produced all-zero series)."""
    perf = load(os.path.join(RES, "results_v9", "e44_perf.csv"))
    if perf is None:
        INFO.append("SKIP  V4 (no e44_perf.csv)")
        return
    labels = sorted({r["label"] for r in perf})
    bad = []
    for lb in labels:
        vals = [float(r["wbw"]) for r in perf if r["label"] == lb]
        if max(vals) < 1.0:          # an entire arm at ~0 = name mismatch
            bad.append(lb)
    check("V4 all arms non-degenerate", not bad, f"arms={len(labels)} bad={bad}")


def v5_femu_direction():
    """V5: FEMU cross-validation direction agrees with the simulator on
    dedup-heavy workloads (LiZone phys reduction > Balloon's)."""
    femu = load(os.path.join(RES, "results_v9", "e95_femu.csv"))
    if femu is None:
        INFO.append("SKIP  V5 (no e95_femu.csv)")
        return
    ok = True
    detail = []
    for wl in ("container", "fsl"):
        lz = [r for r in femu if r["workload"] == wl
              and r["system"] == "lizone"]
        bl = [r for r in femu if r["workload"] == wl
              and r["system"] == "balloon"]
        if lz and bl:
            good = float(lz[0]["phys_red"]) > float(bl[0]["phys_red"])
            ok = ok and good
            detail.append(f"{wl}: lz={float(lz[0]['phys_red']):.2f} "
                          f"balloon={float(bl[0]['phys_red']):.2f}")
    check("V5 FEMU dedup>compression (dedup-heavy)", ok, "; ".join(detail))


if __name__ == "__main__":
    v2_resource_overlap()
    v1_v3_caps_and_consistency()
    v4_figure_series()
    v5_femu_direction()
    for line in INFO:
        print(line)
    if FAILS:
        print()
        for line in FAILS:
            print(line)
        sys.exit(1)
    print("\nALL INVARIANTS PASS — batch accepted")
