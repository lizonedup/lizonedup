"""
run_v11.py -- review-completion experiments (FAST '27 rebuttal round).

Covers the gaps left by the V10 matrix:
  11.0  trace overview table (events, unique, dup frac, oracle ratio, GB,
        read fraction, content mix) for all six workloads -> e0_traces.csv
  11.1  r-sweep completion on backup + rocksdb (matches e1 format)
                                                          -> e1b_recall.csv
  11.5  NAND program-latency sweep on rocksdb (60/100/200 us)
                                                          -> e5b_nandlat.csv
  11.7  capacity sweep (DRAM + write latency):
        functional: v9 replay at 100/200/500/1024/2048/4096 GB x 2 loads
                    + DRAM-vs-data-written checkpoints (v9/globalindex/
                    hostdedup)                              -> e7_capacity.csv
        timing: nodedup + lizone-hw at 100/200/500 GB x 2 loads
                                                          -> e7b_captiming.csv
  11.8  zone-size sweep 128/256/512 MB x {container, rocksdb}:
        functional (v9: dedup/physred/dram/cross-zone/probe)
                                                          -> e8_zonesize.csv
        timing (nodedup + lizone-hw)                       -> e8b_zonetiming.csv

Same unified device and flags as V10 (wi=128, plane-level NAND, V7F);
results must pass the V2/V1/V4 invariants before entering the paper.
"""
import csv
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace  # noqa: E402
from lzsim.replay import replay       # noqa: E402
from lzsim.timing import TimingSim    # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v11")
os.makedirs(RESULTS, exist_ok=True)

# LZ_SMOKE=1 caps every run to a tiny event budget (syntax/flow check only)
SMOKE = os.environ.get("LZ_SMOKE") == "1"
MEV = 60_000 if SMOKE else 3_000_000

DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)
V7F = dict(skew=True, precopy=True, hot_reads=16, dref_prefetch=True,
           redirect_cap=2048, stage1_prefetch=True, zone_locks=True)


def save(name, rows):
    with open(os.path.join(RESULTS, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"saved {name} ({len(rows)} rows)", flush=True)


# ---------------------------------------------------------------- 11.0
def exp110():
    """Trace overview: source, size, redundancy, read fraction."""
    rows = []
    src = {
        "fsl": "FSL Homes user006 fingerprint trace",
        "container": "versioned OCI image stream (synthetic, calibrated)",
        "vm": "patched VM image versions (synthetic, calibrated)",
        "backup": "full+incremental backup snapshots (synthetic, calibrated)",
        "rocksdb": "db_bench fillseq/fillrandom/overwrite/rww",
        "cloud": "cloud primary-storage-like mixed writes (calibrated)",
    }
    rfrac = {"fsl": 0.15, "container": 0.15, "vm": 0.15, "backup": 0.15,
             "rocksdb": 0.30, "cloud": 0.15}
    for wl in ("fsl", "container", "vm", "backup", "rocksdb", "cloud"):
        tr = Trace(wl)
        st = tr.stats()
        n_w = st["events"]
        tm = st["type_mix"]
        tot_uniq = max(1, st["unique"])
        rows.append({
            "workload": wl, "source": src[wl],
            "write_events_m": round(n_w / 1e6, 2),
            "logical_gb": round(n_w * 4096 / 2**30, 2),
            "unique_pages_m": round(st["unique"] / 1e6, 2),
            "dup_frac": round(st["dup_frac"], 4),
            "oracle_dedup": round(st["oracle_dedup_ratio"], 2),
            "read_frac": rfrac[wl],
            "top_types": "/".join(
                f"{k}{round(100*v/tot_uniq)}%"
                for k, v in sorted(tm.items(), key=lambda x: -x[1])[:3]),
        })
        print(f"[11.0] {wl}: dedup={st['oracle_dedup_ratio']:.2f}x "
              f"dup={st['dup_frac']:.3f}", flush=True)
    save("e0_traces.csv", rows)


# ---------------------------------------------------------------- 11.1
def exp111():
    """r-sweep on the two workloads missing from e1 (backup, rocksdb)."""
    rows = []
    for wl in ("backup", "rocksdb"):
        tr = Trace(wl)
        oh = replay(tr, "oracle", max_events=MEV)["hits"]
        for sk in ("oracle", "v9-r1", "v9", "v9-r3", "v9-r4", "v9-prefix"):
            r = replay(tr, sk, dev=DEV(), track_locus=(sk == "v9"),
                       max_events=MEV)
            rows.append({
                "workload": wl, "system": r["system"], "arm": sk,
                "writes": r["writes"], "hits": r["hits"],
                "recall": r["hits"] / max(1, oh),
                "dedup": r["dedup_ratio"],
                "phys_red": 1 - r["data_pages"] / max(1, r["writes"]),
                "ra": r["md_read_bytes_per_req"],
                "probe": r.get("probe_avg", 0),
                "fn_sealed": r.get("fn_sealed_src", 0),
                "cross_zone": r.get("cross_zone_desc_frac", 0),
                "cold_hits": r.get("cold_hits", 0),
            })
            print(f"[11.1] {wl}/{sk}: recall={rows[-1]['recall']:.4f} "
                  f"physred={rows[-1]['phys_red']:.4f}", flush=True)
    save("e1b_recall.csv", rows)


# ---------------------------------------------------------------- timing
def _timing_one(job):
    """One TimingSim config in a worker process."""
    (wl, sk, dev_kw, tprog, cap, zsz, label) = job
    dev_kw = dict(dev_kw)
    if tprog:
        dev_kw["t_prog_us"] = tprog
    if cap:
        dev_kw["capacity_gb"] = cap
    if zsz:
        dev_kw["zone_size_mb"] = zsz
    tr = Trace(wl)
    r = TimingSim(tr, sk, dev=DEV(**dev_kw), qd=32,
                  read_mix_extra=0.30 if wl == "rocksdb" else 0.15,
                  max_events=min(len(tr), MEV), **V7F).run()
    row = {"workload": wl, "system": r["func"]["system"], "label": label,
           "t_prog_us": tprog or 200.0, "capacity_gb": cap or 1024,
           "zone_mb": zsz or 128,
           "wbw": r["write_logi_bw_mbs"], "wphys": r["write_phys_bw_mbs"],
           "rbw": r["read_logi_bw_mbs"], "rphys": r["read_phys_bw_mbs"],
           "wp50": r["w_p50"], "wp95": r["w_p95"], "wp99": r["w_p99"],
           "rp50": r["r_p50"], "rp95": r["r_p95"], "rp99": r["r_p99"],
           "hit_rate": r["func"]["hit_rate"],
           "dram_mb": r["func"].get("dram_total_mb", 0.0)}
    print(f"[timing] {label} wl={wl} t={tprog} cap={cap} zone={zsz}: "
          f"wBW={row['wbw']:.0f} wp99={row['wp99']:.1f}", flush=True)
    return row


def run_pool(jobs, name, workers=8):
    with ProcessPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(_timing_one, jobs))
    save(name, rows)


# ---------------------------------------------------------------- 11.5
def exp115():
    """NAND latency sweep on rocksdb (dedup-light bracket for e5)."""
    jobs = []
    for tprog in (60.0, 100.0, 200.0):
        for sk, dev_kw, label in (
                ("nodedup", {}, "nodedup"),
                ("hostdedup", {}, "hostdedup-1g"),
                ("v9", dict(sha_hw=True), "lizone-hw")):
            jobs.append(("rocksdb", sk, dev_kw, tprog, None, None, label))
    run_pool(jobs, "e5b_nandlat.csv")


# ---------------------------------------------------------------- 11.7
def exp117f():
    """Functional capacity sweep + DRAM-vs-written checkpoints."""
    rows = []
    for wl in ("container", "rocksdb"):
        tr = Trace(wl)
        for cap in (100, 200, 500, 1024, 2048, 4096):
            r = replay(tr, "v9", dev=DEV(capacity_gb=cap), max_events=MEV)
            dr = r["dram"]
            rows.append({
                "workload": wl, "capacity_gb": cap,
                "lz_dram_mb": r["dram_total_mb"],
                "lz_active_zone_dram_mb": (dr["active_zone_filters"] +
                                           dr["delta_logs"] +
                                           dr["working_index"]) / 1048576,
                "lz_cache_mb": dr["caches"] / 1048576,
                "zones_touched": r["zones_used"],
                "dedup": r["dedup_ratio"],
                "globalindex_dram_gb_analytic":
                    (cap * 2**30 / 4096) * 40 / 2**30,
                "hostdedup_dram_gb_analytic":
                    (cap * 2**30 / 4096) * 26 / 2**30,
            })
            print(f"[11.7f] {wl}/{cap}GB: lzDRAM={r['dram_total_mb']:.1f}MB "
                  f"zones={r['zones_used']}", flush=True)
    save("e7_capacity.csv", rows)

    # DRAM checkpoints vs data written (measured growth curves)
    from lzsim.systems import make_system
    rows = []
    step = 250_000 if not SMOKE else 20_000
    for wl in ("container", "rocksdb"):
        tr = Trace(wl)
        for sk, label in (("v9", "lizone"), ("globalindex", "globalindex"),
                          ("hostdedup", "hostdedup-1g")):
            sys_ = make_system(sk, DEV())
            for i in range(min(len(tr), MEV)):
                op, fp, L = tr.event(i)
                if op:
                    continue
                sys_.write(fp, L)
                if (i + 1) % step == 0:
                    if sk == "v9":
                        dram = sys_.dram_breakdown()["total"] / 1048576
                    elif sk == "globalindex":
                        dram = len(sys_.index) * 40 / 1048576
                    else:
                        dram = min(sys_.HOST_INDEX_BYTES,
                                   len(sys_.index) * 20) / 1048576
                    rows.append({
                        "workload": wl, "system": label,
                        "writes_m": round((i + 1) / 1e6, 2),
                        "unique_gb": round(
                            len(sys_.index if sk != "v9" else sys_._truth)
                            * 4096 / 2**30, 2),
                        "dram_mb": dram,
                    })
            print(f"[11.7f] {wl}/{label}: checkpoints done", flush=True)
    save("e7_dram_growth.csv", rows)


def exp117t():
    """Timing capacity sweep: write latency / BW vs device capacity."""
    jobs = []
    for wl in ("container", "rocksdb"):
        for cap in (100, 200, 500):
            jobs.append((wl, "nodedup", {}, None, cap, None,
                         f"nodedup-{cap}g"))
            jobs.append((wl, "v9", dict(sha_hw=True), None, cap, None,
                         f"lizone-hw-{cap}g"))
    run_pool(jobs, "e7b_captiming.csv")


# ---------------------------------------------------------------- 11.8
def exp118f():
    """Functional zone-size sweep on container + rocksdb."""
    rows = []
    for wl in ("container", "rocksdb"):
        tr = Trace(wl)
        for zsz in (128, 256, 512):
            dev = DEV(zone_size_mb=zsz)
            r = replay(tr, "v9", dev=dev, max_events=MEV)
            rows.append({
                "workload": wl, "zone_mb": zsz,
                "dedup": r["dedup_ratio"],
                "phys_red": 1 - r["data_pages"] / max(1, r["writes"]),
                "dram_mb": r["dram_total_mb"],
                "zones_used": r["zones_used"],
                "probe_avg": r.get("probe_avg", 0),
                "cross_zone": r.get("cross_zone_desc_frac", 0),
                "ra": r["md_read_bytes_per_req"],
                # measured post-dedup unique pages per zone x entry size
                "perzone_idx_kb": (r["data_pages"] /
                                   max(1, r["zones_used"])
                                   * dev.index_entry_bytes / 1024),
            })
            print(f"[11.8f] {wl}/{zsz}MB: dedup={r['dedup_ratio']:.2f} "
                  f"cross={rows[-1]['cross_zone']:.3f} "
                  f"dram={r['dram_total_mb']:.1f}", flush=True)
    save("e8_zonesize.csv", rows)


def exp118t():
    """Timing zone-size sweep: nodedup + lizone-hw at 128/256/512 MB."""
    jobs = []
    for wl in ("container", "rocksdb"):
        for zsz in (128, 256, 512):
            jobs.append((wl, "nodedup", {}, None, None, zsz,
                         f"nodedup-z{zsz}"))
            jobs.append((wl, "v9", dict(sha_hw=True), None, None, zsz,
                         f"lizone-hw-z{zsz}"))
    run_pool(jobs, "e8b_zonetiming.csv")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"0": exp110, "1": exp111, "5": exp115, "7f": exp117f,
             "7t": exp117t, "8f": exp118f, "8t": exp118t}
    if which == "all":
        for k in ("0", "1", "7f", "8f", "5", "7t", "8t"):
            steps[k]()
    else:
        steps[which]()
