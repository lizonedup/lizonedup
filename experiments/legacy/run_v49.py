"""
run_v49.py -- HostDedup DRAM-budget sensitivity (512 MB / 1 GB / 2 GB).

  49a  trace-scale timing: 3 budgets x 5 workloads (exp44 config) -- at
       trace scale none of the budgets overflow, so the three variants
       coincide; the peak index size per workload is recorded
  49b  forced-overflow regime: HostDedup-8MB (budget < trace peak) measures
       the per-write spill penalty (one extra 4 KB NAND probe read) on
       container/fsl
  49c  capacity model: full-device host index size (26 B/page incl. 1.3x
       hash overhead) vs budget -> overflow cliff per budget, post-overflow
       penalty applied from the 49b measurement
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lzsim.traces import Trace                    # noqa: E402
from lzsim.timing import TimingSim                # noqa: E402
from lzsim.device import DEFAULT_DEVICE           # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v4")
os.makedirs(RESULTS, exist_ok=True)

W5 = ["fsl", "container", "vm", "backup", "rocksdb"]
BUDGETS = [("hostdedup-512m", 512), ("hostdedup", 1024),
           ("hostdedup-2g", 2048)]
ENTRY = 26            # 20 B entry x 1.3 hash-slot overhead
PAGE = 4096


def save(name, rows):
    with open(os.path.join(RESULTS, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"saved {name} ({len(rows)} rows)", flush=True)


def exp49a():
    rows = []
    for wl in W5:
        tr = Trace(wl)
        extra = 0.30 if wl == "rocksdb" else 0.15
        for sk, mb in BUDGETS:
            sim = TimingSim(tr, sk, qd=32, read_mix_extra=extra,
                            max_events=min(len(tr), 3_000_000))
            r = sim.run()
            fn = r["func"]
            rows.append({
                "workload": wl, "budget_mb": mb, "system": fn["system"],
                "index_peak_mb": round(fn.get("host_index_mb", 0.0), 1),
                "spill_reads": fn.get("host_spill_reads", 0),
                "wbw": r["write_logi_bw_mbs"], "wphys": r["write_phys_bw_mbs"],
                "wp50": r["w_p50"], "wp99": r["w_p99"],
                "rp99": r["r_p99"], "dedup": fn["dedup_ratio"],
            })
            print(f"[49a] {wl}/{mb}MB: idx={rows[-1]['index_peak_mb']}MB "
                  f"spill={rows[-1]['spill_reads']} wBW={r['write_logi_bw_mbs']:.0f}",
                  flush=True)
    save("e49_hostdram.csv", rows)


def exp49b():
    rows = []
    for wl in ("container", "fsl"):
        tr = Trace(wl)
        for sk, tag in (("hostdedup", "normal"), ("hostdedup-8m", "overflow")):
            sim = TimingSim(tr, sk, qd=32, read_mix_extra=0.15,
                            max_events=min(len(tr), 3_000_000))
            r = sim.run()
            fn = r["func"]
            rows.append({
                "workload": wl, "regime": tag, "system": fn["system"],
                "index_peak_mb": round(fn.get("host_index_mb", 0.0), 1),
                "spill_reads": fn.get("host_spill_reads", 0),
                "spill_frac": fn.get("host_spill_reads", 0) /
                max(1, fn["writes"]),
                "wbw": r["write_logi_bw_mbs"],
                "wp50": r["w_p50"], "wp99": r["w_p99"],
                "host_us": r["parts_w_us"]["host"],
                "dedup": fn["dedup_ratio"],
            })
            print(f"[49b] {wl}/{tag}: spill={rows[-1]['spill_reads']} "
                  f"wBW={r['write_logi_bw_mbs']:.0f} wp99={r['w_p99']:.0f}",
                  flush=True)
    save("e49_overflow.csv", rows)


def exp49c():
    # post-overflow penalty measured in 49b, applied by the capacity model
    ov = {}
    p = os.path.join(RESULTS, "e49_overflow.csv")
    if os.path.exists(p):
        with open(p, newline="") as fh:
            for r in csv.DictReader(fh):
                ov[r["regime"]] = r
    rows = []
    for _, mb in BUDGETS:
        cliff_gb = mb * 1048576 / ENTRY * PAGE / 1e9
        for cap_gb in (128, 256, 512, 1024, 2048, 4096):
            pages = cap_gb * 1024 ** 3 // PAGE
            idx_gb = pages * ENTRY / 1e9
            rows.append({
                "budget_mb": mb,
                "capacity_gb": cap_gb,
                "index_gb": round(idx_gb, 2),
                "overflow": int(idx_gb > mb / 1024.0),
                "cliff_gb": round(cliff_gb, 0),
                # modeled post-overflow write BW from the measured regime
                # (HostDedup-8MB run): scale = measured overflow wBW /
                # measured normal wBW on the same workload
                "wbw_overflow_model": round(
                    float(ov["overflow"]["wbw"]) if "overflow" in ov else 0, 0),
                "wbw_normal_model": round(
                    float(ov["normal"]["wbw"]) if "normal" in ov else 0, 0),
            })
            print(f"[49c] {mb}MB @ {cap_gb}GB: idx={idx_gb:.2f}GB "
                  f"overflow={rows[-1]['overflow']}", flush=True)
    save("e49_capacity.csv", rows)



def exp49d():
    # host CPU overhead at saturation: HostDedup-1GB vs LiZoneDedup vs
    # NoDedup on four typical applications.  HostDedup pays SHA-256 +
    # index lookup per write on the host (plus 0.3 us per read for index
    # resolve); LiZoneDedup hashes on the controller, host adds nothing
    # beyond the baseline submission cost common to all systems.
    rows = []
    for wl in ("fsl", "container", "vm", "rocksdb"):
        tr = Trace(wl)
        extra = 0.30 if wl == "rocksdb" else 0.15
        for sk in ("hostdedup", "lizone", "nodedup"):
            sim = TimingSim(tr, sk, qd=32, read_mix_extra=extra,
                            max_events=min(len(tr), 3_000_000))
            r = sim.run()
            writes = r["writes"]
            reads = r["reads"]
            host_w_us = r["parts_w_us"]["host"] * writes
            host_r_us = 0.3 * reads if sk == "hostdedup" else 0.0
            host_us = host_w_us + host_r_us
            T = max(1e-9, r["sim_time_s"])
            gb = r["write_logi_bw_mbs"] * T / 1024
            rows.append({
                "workload": wl, "system": r["func"]["system"],
                "wbw": r["write_logi_bw_mbs"], "writes": writes,
                "reads": reads,
                "host_us_per_write": round(r["parts_w_us"]["host"], 2),
                "host_cores": round(host_us / 1e6 / T, 2),
                "host_cpu_s_per_gb": round(host_us / 1e6 / max(1e-9, gb), 2),
                "gb_written": round(gb, 1),
            })
            print(f"[49d] {wl}/{sk}: cores={rows[-1]['host_cores']} "
                  f"per_write={rows[-1]['host_us_per_write']}us "
                  f"wBW={r['write_logi_bw_mbs']:.0f}", flush=True)
    save("e49_hostcpu.csv", rows)

if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "a"):
        exp49a()
    if which in ("all", "b"):
        exp49b()
    if which in ("all", "c"):
        exp49c()
    if which in ("all", "d"):
        exp49d()
