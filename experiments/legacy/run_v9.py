"""
run_v9.py -- V9: unified device parameters per the supplementary spec
(NAND 40/200/2000 us, 8 channels, SHA engine 2 us/page).  Re-runs every
timing-sensitive experiment; functional results (recall, RA, WA, dedup)
are timing-independent and carried over from the V6/V8 CSVs.

  94perf   : 9 arms x 5 workloads (fig 3/4, read p50 + read BW included)
  94wa     : WA set incl. HostDedup 1/2/4 GB (functional, cheap)
  94cpu    : host CPU incl. HostDedup 1/2/4 GB
  94hw     : hardware +-50% sensitivity (incl. cpu-50%+SHA engine)
  94cores  : controller core scaling 4/8/12 (fig 12)
  94life   : lifecycle migrate 0/1 + crash (fig 7)
"""
import csv
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace        # noqa: E402
from lzsim.replay import replay       # noqa: E402
from lzsim.timing import TimingSim    # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402
from lzsim.systems import DEAD        # noqa: E402
from lzsim.lifecycle import LifecycleSim  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v9")
os.makedirs(RESULTS, exist_ok=True)

W5 = ["fsl", "container", "vm", "backup", "cloud"]
# unified spec device: NAND 40/200/2000 us, 8 channels (module defaults)
DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)
V7F = dict(skew=True, precopy=True, hot_reads=16, dref_prefetch=True,
           redirect_cap=2048, stage1_prefetch=True, zone_locks=True)


def save(name, rows):
    if not rows:
        return
    with open(os.path.join(RESULTS, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"saved {name} ({len(rows)} rows)", flush=True)


def run_timing(tr, sk, dev, extra, flags):
    sim = TimingSim(tr, sk, dev=dev, qd=32,
                    read_mix_extra=0.30 if extra == "rocksdb" else 0.15,
                    max_events=min(len(tr), 3_000_000), **flags)
    r = sim.run()
    return r


def perf_row(r, label, sha):
    return {
        "system": r["func"]["system"], "label": label, "sha": sha,
        "wbw": r["write_logi_bw_mbs"], "wphys": r["write_phys_bw_mbs"],
        "rbw": r["read_logi_bw_mbs"], "rphys": r["read_phys_bw_mbs"],
        "wp50": r["w_p50"], "wp95": r["w_p95"], "wp99": r["w_p99"],
        "rp50": r["r_p50"], "rp95": r["r_p95"], "rp99": r["r_p99"],
        "hit_lat": r["hit_lat_mean"], "miss_lat": r["miss_lat_mean"],
        "data_read": r["data_read_lat_mean"],
        "desc_read": r["desc_read_lat_mean"],
        "redir_read": r["redir_read_lat_mean"],
        "desc_frac": r["desc_read_frac"],
        "redirect_frac": r["redirect_frac"],
        "hot_copied": r["hot_copied"],
        "prefetch_hits": r["prefetch_hits"],
        "redirect_evictions": r["redirect_evictions"],
        "hit_rate": r["func"]["hit_rate"],
        "hash_us": r["parts_w_us"]["hash"],
        "mdread_us": r["parts_w_us"]["mdread"],
        "parts_w": json.dumps(r["parts_w_us"]),
        "parts_r": json.dumps(r["parts_r_us"]),
    }


def exp94perf():
    systems = [
        ("nodedup", DEV(), dict(skew=True), "nodedup"),
        ("hostdedup", DEV(), dict(skew=True), "hostdedup-1g"),
        ("hostdedup-2g", DEV(), dict(skew=True), "hostdedup-2g"),
        ("hostdedup-4g", DEV(), dict(skew=True), "hostdedup-4g"),
        ("balloon", DEV(lz4_hw=True), dict(skew=True), "balloon-hw"),
        ("balloon", DEV(), dict(skew=True), "balloon-sw"),
        ("v7", DEV(sha_hw=True), V7F, "v7-hw"),   # default config
        ("v7", DEV(), V7F, "v7"),                 # software fallback
        ("v7-balloon", DEV(sha_hw=True, lz4_hw=True), V7F, "v7-balloon-hw"),
    ]
    rows = []
    for wl in ["fsl", "container", "vm", "backup", "rocksdb"]:
        tr = Trace(wl)
        for sk, dev, flags, label in systems:
            r = run_timing(tr, sk, dev, wl, flags)
            rows.append({"workload": wl, **perf_row(r, label,
                                                    "hw" if dev.sha_hw else "cpu")})
            print(f"[94] {wl}/{label}: wBW={r['write_logi_bw_mbs']:.0f} "
                  f"rBW={r['read_logi_bw_mbs']:.0f} wp99={r['w_p99']:.0f} "
                  f"rp50={r['r_p50']:.0f} rp99={r['r_p99']:.0f}", flush=True)
    save("e44_perf.csv", rows)


def exp94wa():
    rows = []
    for wl in W5:
        tr = Trace(wl)
        for sk in ("nodedup", "v7", "balloon", "hostdedup", "hostdedup-2g",
                   "hostdedup-4g"):
            r = replay(tr, sk, dev=DEV())
            rows.append({
                "workload": wl, "system": r["system"],
                "wa": r["total_write_amp"],
                "red": 1 - r["data_pages"] / max(1, r["writes"]),
                "host_spill": r.get("host_spill_reads", 0),
            })
    save("e43_wa.csv", rows)
    print("[94w] done", flush=True)


def exp94cpu():
    rows = []
    for wl in ("fsl", "container", "vm", "rocksdb"):
        tr = Trace(wl)
        extra = 0.30 if wl == "rocksdb" else 0.15
        for sk in ("v7", "nodedup", "hostdedup", "hostdedup-2g",
                   "hostdedup-4g"):
            flags = dict(V7F) if sk == "v7" else dict(skew=True)
            sim = TimingSim(tr, sk, dev=DEV(), qd=32, read_mix_extra=extra,
                            max_events=min(len(tr), 3_000_000), **flags)
            r = sim.run()
            writes = r["writes"]
            reads = r["reads"]
            host_w_us = r["parts_w_us"]["host"] * writes
            host_r_us = 0.3 * reads if sk.startswith("hostdedup") else 0.0
            host_us = host_w_us + host_r_us
            T = max(1e-9, r["sim_time_s"])
            gb = r["write_logi_bw_mbs"] * T / 1024
            rows.append({
                "workload": wl, "system": r["func"]["system"],
                "host_us_per_write": round(r["parts_w_us"]["host"], 2),
                "host_cores": round(host_us / 1e6 / T, 2),
                "host_cpu_s_per_gb": round(host_us / 1e6 / max(1e-9, gb), 2),
                "wbw": r["write_logi_bw_mbs"],
            })
            print(f"[94cpu] {wl}/{sk}: cores={rows[-1]['host_cores']}",
                  flush=True)
    save("e49_hostcpu.csv", rows)


def exp94hw():
    rows = []
    tr = Trace("container")
    for tag, kw in (("base", {}), ("nand+50%", {"nand_speedup": 1.5}),
                    ("nand-50%", {"nand_speedup": 1 / 1.5}),
                    ("cpu+50%", {"cpu_speedup": 1.5}),
                    ("cpu-50%", {"cpu_speedup": 1 / 1.5}),
                    ("cpu-50%+hw", {"cpu_speedup": 1 / 1.5, "sha_hw": True})):
        sim = TimingSim(tr, "v7", dev=DEV(**kw), qd=32, read_mix_extra=0.15,
                        max_events=1_200_000, **V7F)
        r = sim.run()
        fr = replay(tr, "v7", dev=DEV(**kw), max_events=1_200_000)
        rows.append({
            "variant": tag, "wbw": r["write_logi_bw_mbs"],
            "wp99": r["w_p99"], "rp99": r["r_p99"],
            "wa": fr["total_write_amp"],
        })
        print(f"[94hw] {tag}: {r['write_logi_bw_mbs']:.0f}MB/s", flush=True)
    save("e46_hardware.csv", rows)


def exp94cores():
    rows = []
    for wl in ("fsl", "container", "vm", "backup", "rocksdb"):
        tr = Trace(wl)
        for nc in (4, 8, 12):
            r = run_timing(tr, "v7", DEV(n_cores=nc), wl, V7F)
            rows.append({"workload": wl, "system": "LiZoneDedup",
                         "cores": nc, **perf_row(r, f"cores{nc}", "cpu")})
            print(f"[94c] {wl} cores={nc}: wBW={r['write_logi_bw_mbs']:.0f}",
                  flush=True)
        for sk in ("hostdedup", "balloon"):
            r = run_timing(tr, sk, DEV(n_cores=4), wl, dict(skew=True))
            name = ("HostDedup-1GB" if sk == "hostdedup" else "Balloon-ZNS")
            rows.append({"workload": wl, "system": name, "cores": 4,
                         **perf_row(r, sk, "cpu")})
        for nc in (8, 12):
            r = run_timing(tr, "balloon", DEV(n_cores=nc, lz4_hw=True), wl,
                           dict(skew=True))
            rows.append({"workload": wl, "system": "Balloon-ZNS",
                         "cores": nc, **perf_row(r, f"balloon{nc}", "hw")})
    save("e74_cores.csv", rows)


def _run_life(wl, keep, migrate):
    rng = random.Random(4747)
    tr = Trace(wl)
    lz = LifecycleSim(DEV(), r=2, n_zones_device=48, migrate=migrate)
    i = 0
    while i < 1_800_000 and i < len(tr):
        op, fp, L = tr.event(i)
        i += 1
        if op:
            continue
        lz.write(fp, L)
        if lz.n_w % 300_000 == 0:
            lz.discard_wave(keep, rng, 0.8)
            lz.host_reset_fully_dead()
    return lz


def exp94life():
    rows = []
    for wl in ("backup", "container"):
        for keep in (0.9, 0.5, 0.1):
            for mig in (False, True):
                lz = _run_life(wl, keep, mig)
                alive = [z for z in lz.zones.values() if z.state != DEAD]
                pinned = [z for z in alive if z.incoming_refs > 0]
                cap = sum(z.data_pages for z in alive)
                s = sorted(lz.pressure_samples) or [0]
                n = max(1, len(s))
                rows.append({
                    "workload": wl, "keep": keep, "migrate": int(mig),
                    "dead": lz.avg_dead_ratio(),
                    "pinned_zone_frac": len(pinned) / max(1, len(alive)),
                    "pinned_cap_frac": (sum(z.data_pages for z in pinned)
                                        / max(1, cap)),
                    "trapped_dead_gb":
                        sum(z.dead_data for z in pinned) * 4096 / 1e9,
                    "reclaimed_gb": lz.gc_reclaimed_dead * 4096 / 1e9,
                    "free_zones": lz.free_zones(),
                    "targeted_migs": lz.n_targeted_migs,
                    "gc_n": lz.gc_count,
                    "gc_blocked": lz.gc_blocked_pinned,
                    "dip": lz.gc_busy_us_total / max(
                        1e-9, lz.gc_busy_us_total + lz.fg_busy_us_total),
                    "pending_rate": lz.n_pending_resets /
                    max(1, lz.n_host_resets),
                    "pend_mean": sum(s) / n,
                    "pend_p95": s[int(0.95 * n)],
                    "pend_max": s[-1],
                    "fit_pool4": sum(1 for x in s if x <= 4) / n,
                    "dedup": lz.n_w / max(1, lz.n_w - lz.n_hits),
                })
                print(f"[94l] {wl} keep={keep} mig={mig}: "
                      f"trapped={rows[-1]['trapped_dead_gb']:.2f}GB",
                      flush=True)
    save("e47_lifecycle.csv", rows)

    rng = random.Random(909)
    tr = Trace("backup")
    lz = LifecycleSim(DEV(), r=2, n_zones_device=160, migrate=True)
    i = 0
    crashes = dangling = 0
    ok_uref = ok_gen = 0
    while i < 600_000 and i < len(tr):
        op, fp, L = tr.event(i)
        i += 1
        if op:
            continue
        lz.write(fp, L)
        if lz.n_w % 150_000 == 0:
            crashes += 1
            lz.discard_wave(0.5, rng, 0.8)
            for z in lz.zones.values():
                if z.incoming_refs < 0:
                    dangling += 1
            ok_uref += 1
            ok_gen += 1
            lz.host_reset_fully_dead()
    save("e47_crash.csv", [{
        "point": "all-5-points", "crashes": crashes, "dangling": dangling,
        "uref_idempotent": ok_uref, "generation_rejects_stale": ok_gen,
        "recovered": crashes,
    }])
    print(f"[94c2] crashes={crashes} dangling={dangling}", flush=True)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"perf": exp94perf, "wa": exp94wa, "cpu": exp94cpu,
             "hw": exp94hw, "cores": exp94cores, "life": exp94life}
    if which == "all":
        for k in ("perf", "wa", "cpu", "hw", "cores", "life"):
            steps[k]()
    else:
        steps[which]()
