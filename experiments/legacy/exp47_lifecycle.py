"""
exp47_lifecycle.py -- Sec 4.7 zone lifecycle / GC / reset-pending.

Runs Backup and Container streams through a capacity-limited device with
host discards (old versions deleted), host resets of fully-dead zones,
and watermark-triggered data-moving GC.  Sweeps the discard fraction;
each point reports the achieved average dead ratio and the 4.7 metric set
for LiZoneDedup and NoDedup-ZNS.
Outputs: results/exp47_lifecycle.csv
"""

import csv
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lzsim.traces import Trace                     # noqa: E402
from lzsim.device import DEFAULT_DEVICE, DeviceConfig  # noqa: E402
from lzsim.lifecycle import LifecycleSim, NoDupLife, PAGE  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results")

WAVE_EVENTS = 300_000          # host discard wave cadence (approx 1 version)
MAX_EVENTS = 1_800_000         # prefix of the trace for this experiment
KEEP_FRACS = [0.90, 0.70, 0.50, 0.30, 0.10]  # sweep -> dead ratio
OLD_FRAC = 0.8                 # discard targets the oldest 80% of zones
                                        # (approx. deleting old versions)


def run_point(wl, keep_frac, n_zones):
    rng = random.Random(4747)
    tr = Trace(wl)
    dev = DEFAULT_DEVICE
    lz = LifecycleSim(dev, r=2, n_zones_device=n_zones)
    nd = NoDupLife(dev, n_zones_device=n_zones)
    wave = 0
    i = 0
    while i < MAX_EVENTS and i < len(tr):
        op, fp, L = tr.event(i)
        i += 1
        if op:
            continue
        lz.write(fp, L)
        nd.write(fp, L)
        if lz.n_w % WAVE_EVENTS == 0:
            wave += 1
            lz.discard_wave(keep_frac, rng, OLD_FRAC, wave=wave)
            nd.discard_wave(keep_frac, rng, OLD_FRAC, wave=wave)
            lz.host_reset_fully_dead()
            nd.host_reset_fully_dead()
            nd._gc_batch()

    T = max(1e-9, lz.n_w)
    # foreground write bandwidth estimate (channel-parallel, hit-dominated)
    fg_us = lz.fg_busy_us_total
    sec_per_event = (fg_us / dev.n_channels) / 1e6 / T
    pend_ev = lz.pending_durations_events
    # GC events bucketed by victim-zone dead ratio
    buckets = {}
    for dr, mig, nupd, scan, busy in lz.gc_victim_log:
        b = min(80, max(10, int(dr * 100 // 20 * 20)))
        rec = buckets.setdefault(b, {"n": 0, "mig": 0, "upd": 0,
                                     "scan": 0.0, "busy": 0.0})
        rec["n"] += 1
        rec["mig"] += mig
        rec["upd"] += nupd
        rec["scan"] += scan
        rec["busy"] += busy

    res_lz = {
        "workload": wl, "system": "LiZoneDedup",
        "keep_frac": keep_frac,
        "dead_ratio": lz.avg_dead_ratio(),
        "writes": lz.n_w,
        "dedup_ratio": lz.n_w / max(1, lz.n_w - lz.n_hits),
        "host_resets": lz.n_host_resets,
        "pending_resets": lz.n_pending_resets,
        "pending_rate": lz.n_pending_resets / max(1, lz.n_host_resets),
        "pending_dur_events_mean": (sum(pend_ev) / len(pend_ev)) if pend_ev else 0,
        "pending_dur_s_mean": ((sum(pend_ev) / len(pend_ev)) * sec_per_event)
        if pend_ev else 0.0,
        "pending_dur_gb_mean": ((sum(pend_ev) / len(pend_ev)) * PAGE / 1e9)
        if pend_ev else 0.0,
        "incoming_mean": lz.incoming_stats()["mean_incoming"],
        "incoming_max": lz.incoming_stats()["max_incoming"],
        "cross_zone_desc_frac": lz.cross_zone_desc_created /
                                max(1, lz.desc_total),
        "sealed_ref_frac": lz.sealed_ref_created / max(1, lz.desc_total),
        "copy_forward": lz.copy_forward,
        "gc_count": lz.gc_count,
        "gc_migrated_gb": lz.gc_migrated_pages * PAGE / 1e9,
        "gc_desc_updates": lz.gc_desc_updates,
        "gc_desc_scan_gb": lz.gc_desc_scan_pages * PAGE / 1e9,
        "gc_dip_frac": lz.gc_busy_us_total / max(1e-9, lz.gc_busy_us_total +
                                                 lz.fg_busy_us_total +
                                                 lz.n_w * 0.15 * dev.t_read),
        "uref_emitted": lz.uref_emitted,
        "gc_buckets": buckets,
        "gc_pinned_skips": lz.gc_blocked_pinned,
    }
    res_nd = {
        "workload": wl, "system": "NoDedup-ZNS",
        "keep_frac": keep_frac,
        "dead_ratio": nd.avg_dead_ratio(),
        "writes": nd.n_w,
        "dedup_ratio": 1.0,
        "host_resets": nd.n_host_resets,
        "pending_resets": 0, "pending_rate": 0,
        "pending_dur_events_mean": 0, "pending_dur_s_mean": 0,
        "pending_dur_gb_mean": 0,
        "incoming_mean": 0, "incoming_max": 0,
        "cross_zone_desc_frac": 0, "sealed_ref_frac": 0, "copy_forward": 0,
        "gc_count": nd.gc_count,
        "gc_migrated_gb": nd.gc_migrated_pages * PAGE / 1e9,
        "gc_desc_updates": 0, "gc_desc_scan_gb": 0,
        "gc_dip_frac": nd.gc_busy_us_total / max(1e-9, nd.gc_busy_us_total +
                                                 nd.fg_busy_us_total +
                                                 nd.n_w * 0.15 * dev.t_read),
        "uref_emitted": 0,
        "gc_buckets": {},
        "gc_pinned_skips": 0,
    }
    return res_lz, res_nd


def main():
    os.makedirs(RESULTS, exist_ok=True)
    rows = []
    for wl in ("backup", "container"):
        n_zones = 56 if wl == "backup" else 60
        for kf in KEEP_FRACS:
            t0 = time.time()
            a, b = run_point(wl, kf, n_zones)
            rows.extend([a, b])
            print(f"[exp47] {wl} keep={kf}: dead={a['dead_ratio']:.2f} "
                  f"pending_rate={a['pending_rate']:.3f} "
                  f"gcGB={a['gc_migrated_gb']:.2f} dip={a['gc_dip_frac']:.3f} "
                  f"({time.time()-t0:.0f}s)", flush=True)
    with open(os.path.join(RESULTS, "exp47_lifecycle.csv"), "w",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            r2 = dict(r)
            r2["gc_buckets"] = json.dumps(r2.get("gc_buckets", {}))
            w.writerow(r2)
    print("saved exp47_lifecycle.csv")


if __name__ == "__main__":
    main()
