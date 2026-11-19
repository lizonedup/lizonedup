"""
run_v7.py -- V7: closing the software-variant bandwidth gap.

  74a  controller core scaling (4/8/12 workers): v7 write/read BW vs
       HostDedup/Balloon references; per-zone commit-lock contention
  74b  two-stage prefetch: stage-2 shard-neighbour prefetch (in-system,
       RA effect) + stage-1 async filter prefetch (timing effect)
  74c  HostDedup budget sweep 512MB/1GB/2GB/4GB with read AND write BW,
       plus the 4GB capacity cliff
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace        # noqa: E402
from lzsim.replay import replay       # noqa: E402
from lzsim.timing import TimingSim    # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v7")
os.makedirs(RESULTS, exist_ok=True)

W5 = ["fsl", "container", "vm", "backup", "rocksdb"]
DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)
V7F = dict(skew=True, precopy=True, hot_reads=16, dref_prefetch=True,
           redirect_cap=2048, stage1_prefetch=True, zone_locks=True)
V6F = dict(skew=True, precopy=True, hot_reads=16, dref_prefetch=True,
           redirect_cap=2048)


def save(name, rows):
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
    return {
        "wbw": r["write_logi_bw_mbs"], "rbw": r["read_logi_bw_mbs"],
        "wp50": r["w_p50"], "wp99": r["w_p99"],
        "rp50": r["r_p50"], "rp99": r["r_p99"],
        "hit_rate": r["func"]["hit_rate"],
        "zone_stalls": r["zone_lock_stalls"],
        "prefetch_hits": r["prefetch_hits"],
    }


def exp74a():
    rows = []
    for wl in W5:
        tr = Trace(wl)
        for nc in (4, 8, 12):
            r = run_timing(tr, "v7", DEV(n_cores=nc), wl, V7F)
            rows.append({"workload": wl, "system": "LiZoneDedup", "cores": nc,
                         **r})
            print(f"[74a] {wl} cores={nc}: wBW={r['wbw']:.0f} "
                  f"rBW={r['rbw']:.0f} stalls={r['zone_stalls']}", flush=True)
        # references at their natural configs
        for sk in ("hostdedup", "balloon"):
            r = run_timing(tr, sk, DEV(n_cores=4), wl, dict(skew=True))
            rows.append({"workload": wl, "system":
                         "HostDedup-1GB" if sk == "hostdedup" else "Balloon-ZNS",
                         "cores": 4, **r})
        # balloon also scales with cores (LZ4 on cores) -- two anchor points
        for nc in (8, 12):
            r = run_timing(tr, "balloon", DEV(n_cores=nc), wl, dict(skew=True))
            rows.append({"workload": wl, "system": "Balloon-ZNS",
                         "cores": nc, **r})
    save("e74_cores.csv", rows)


def exp74b():
    rows = []
    for wl in W5:
        tr = Trace(wl)
        for tag, sk, flags in (
                ("v6", "v6", V6F),
                ("v7-stage12", "v7", V7F),
                ("v7-nolocks", "v7", dict(V7F, zone_locks=False))):
            r = run_timing(tr, sk, DEV(), wl, flags)
            rows.append({"workload": wl, "arm": tag, **r})
            print(f"[74b] {wl}/{tag}: wBW={r['wbw']:.0f} "
                  f"rBW={r['rbw']:.0f}", flush=True)
    save("e74_prefetch.csv", rows)

    rows = []
    for wl in W5:
        tr = Trace(wl)
        for sk in ("v6", "v7"):
            r = replay(tr, sk, dev=DEV())
            rows.append({
                "workload": wl, "system": sk,
                "ra": r["md_read_bytes_per_req"],
                "shard_hits": r.get("cache_shard_hit", 0),
                "shard_miss": r.get("cache_shard_miss", 0),
                "wamd": r["md_write_bytes_per_data_byte"],
                "dram": r.get("dram_total_mb", 0),
                "prefetch_bg_pages": getattr(
                    __import__("lzsim.systems", fromlist=["make_system"])
                    .make_system(sk, DEV()), "prefetch_bg_pages", 0),
            })
            print(f"[74b-f] {wl}/{sk}: RA={rows[-1]['ra']:.1f}", flush=True)
    save("e74_prefetch_ra.csv", rows)


def exp74c():
    rows = []
    for wl in ("container", "fsl", "rocksdb"):
        tr = Trace(wl)
        for sk, mb in (("hostdedup-512m", 512), ("hostdedup", 1024),
                       ("hostdedup-2g", 2048), ("hostdedup-4g", 4096)):
            r = run_timing(tr, sk, DEV(n_cores=4), wl, dict(skew=True))
            rows.append({"workload": wl, "budget_mb": mb,
                         "system": f"HostDedup-{mb}MB", **r})
            print(f"[74c] {wl}/{mb}MB: wBW={r['wbw']:.0f} "
                  f"rBW={r['rbw']:.0f}", flush=True)
    save("e74_hostdram.csv", rows)

    # capacity cliff incl. the new 4GB budget (26 B/page model)
    rows = []
    for mb in (512, 1024, 2048, 4096):
        cliff_gb = mb * 1048576 / 26 * 4096 / 1e9
        for cap_gb in (128, 256, 512, 1024, 2048, 4096, 8192):
            pages = cap_gb * 1024 ** 3 // 4096
            idx_gb = pages * 26 / 1e9
            rows.append({"budget_mb": mb, "capacity_gb": cap_gb,
                         "index_gb": round(idx_gb, 2),
                         "overflow": int(idx_gb > mb / 1024.0),
                         "cliff_gb": round(cliff_gb)})
    save("e74_capacity.csv", rows)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "a"):
        exp74a()
    if which in ("all", "b"):
        exp74b()
    if which in ("all", "c"):
        exp74c()
