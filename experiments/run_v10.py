"""
run_v10.py -- FINAL plan ("LiZoneDedup 完整测试计划（最终版）") matrix.

Deltas vs V9 per the plan:
  10.1 recall incl. r=4 (fig 1: 6 bars -> r1/r2/r3/r4/r2-prefix/Oracle)
  10.2 five-layer ablation (WorkingIndex merged into +FullCaching)
  10.3 perf: 9 arms x 5 loads (fig 4) -- adds PageFTLDedup timing upper
      bound, combo-sw (sw SHA+LZ4 16.4us), hostdedup-1g
  10.4 HostDedup DRAM budgets 512M/1G/2G/4G x 4 loads (fig 6a)
  10.5 NAND program latency sweep 60/100/200 us (tab 4)
  10.6 cross-zone reference statistics (4.3 table)
All timing runs use the scheduler-fixed TimingSim and must pass validate.py.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace        # noqa: E402
from lzsim.replay import replay       # noqa: E402
from lzsim.timing import TimingSim    # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v10")
os.makedirs(RESULTS, exist_ok=True)

W5 = ["fsl", "container", "vm", "backup", "rocksdb"]
W4_RECALL = ["fsl", "container", "vm", "cloud"]
DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)
V7F = dict(skew=True, precopy=True, hot_reads=16, dref_prefetch=True,
           redirect_cap=2048, stage1_prefetch=True, zone_locks=True)


def save(name, rows):
    with open(os.path.join(RESULTS, name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"saved {name} ({len(rows)} rows)", flush=True)


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
        "hit_rate": r["func"]["hit_rate"],
        "hash_us": r["parts_w_us"]["hash"],
        "mdread_us": r["parts_w_us"]["mdread"],
        "prog_us": r["parts_w_us"]["prog"],
        "parts_w": json.dumps(r["parts_w_us"]),
        "parts_r": json.dumps(r["parts_r_us"]),
    }


def run_timing(tr, sk, dev, wl, flags):
    return TimingSim(
        tr, sk, dev=dev, qd=32,
        read_mix_extra=0.30 if wl == "rocksdb" else 0.15,
        max_events=min(len(tr), 3_000_000), **flags).run()


# ---------------------------------------------------------------- 10.1
def exp101():
    rows = []
    for wl in W4_RECALL:
        tr = Trace(wl)
        oh = replay(tr, "oracle")["hits"]
        for sk in ("oracle", "v9-r1", "v9", "v9-r3", "v9-r4", "v9-prefix"):
            r = replay(tr, sk, track_locus=(sk == "v9"))
            rows.append({
                "workload": wl, "system": r["system"],
                "arm": sk,
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
            print(f"[10.1] {wl}/{sk}: recall={rows[-1]['recall']:.4f}",
                  flush=True)
    save("e1_recall.csv", rows)


# ---------------------------------------------------------------- 10.2
def exp102():
    layers = [("globalindex", "GlobalIndex"),
              ("datapath", "DataPathOnly"),
              ("shard", "+IdentityShard"),
              ("split", "+SplitState"),
              ("v9", "+FullCaching")]
    rows = []
    for wl in W5 + ["cloud"]:
        tr = Trace(wl)
        for sk, lay in layers:
            r = replay(tr, sk, dev=DEV())
            rows.append({
                "workload": wl, "layer": sk, "label": lay,
                "ra": r["md_read_bytes_per_req"],
                "wamd": r["md_write_bytes_per_data_byte"],
                "wa": r["total_write_amp"],
                "dedup": r["dedup_ratio"],
                "dram": r.get("dram_total_mb", 0),
                "shard_hit": r.get("cache_shard_hit", 0),
                "shard_miss": r.get("cache_shard_miss", 0),
                "cold_hits": r.get("cold_hits", 0),
            })
            print(f"[10.2] {wl}/{lay}: RA={rows[-1]['ra']:.0f}", flush=True)
    save("e2_ablation.csv", rows)

    # Table 2 source: WA component breakdown (Container + RocksDB)
    rows = []
    for wl in ("container", "rocksdb"):
        tr = Trace(wl)
        r = replay(tr, "v9", dev=DEV())
        n = max(1, r["writes"])
        data_b = r["data_pages"] * 4096
        md_log_b = r["md_write_bytes_per_data_byte"] * n * 4096
        rows.append({
            "workload": wl,
            "data_writes_x": data_b / (n * 4096),
            "md_log_x": md_log_b / (n * 4096),
            "total_wa": r["total_write_amp"],
        })
        print(f"[10.2t] {wl}: WA={r['total_write_amp']:.3f}", flush=True)
    save("e2_wa_table.csv", rows)


# ---------------------------------------------------------------- 10.3
def exp103():
    systems = [
        ("nodedup", DEV(), dict(skew=True), "nodedup"),
        ("hostdedup", DEV(), dict(skew=True), "hostdedup-1g"),
        ("v9", DEV(), V7F, "lizone-sw"),
        ("v9", DEV(sha_hw=True), V7F, "lizone-hw"),
        ("balloon", DEV(), dict(skew=True), "balloon-sw"),
        ("balloon", DEV(lz4_hw=True), dict(skew=True), "balloon-hw"),
        ("v9-balloon", DEV(), V7F, "combo-sw"),
        ("v9-balloon", DEV(sha_hw=True, lz4_hw=True), V7F, "combo-hw"),
        ("oracle", DEV(sha_hw=True), dict(skew=True), "oracle"),
    ]
    rows = []
    for wl in W5:
        tr = Trace(wl)
        for sk, dev, flags, label in systems:
            r = run_timing(tr, sk, dev, wl, flags)
            rows.append({"workload": wl,
                         **perf_row(r, label,
                                    "hw" if dev.sha_hw or dev.lz4_hw
                                    else "cpu")})
            print(f"[10.3] {wl}/{label}: wBW={r['write_logi_bw_mbs']:.0f} "
                  f"rBW={r['read_logi_bw_mbs']:.0f} wp99={r['w_p99']:.0f} "
                  f"rp99={r['r_p99']:.0f}", flush=True)
    save("e4_perf.csv", rows)


# ---------------------------------------------------------------- 10.4
def exp104():
    rows = []
    for wl in ("fsl", "container", "vm", "backup"):
        tr = Trace(wl)
        for sk, mb in (("hostdedup-512m", 512), ("hostdedup", 1024),
                       ("hostdedup-2g", 2048), ("hostdedup-4g", 4096)):
            r = run_timing(tr, sk, DEV(), wl, dict(skew=True))
            rows.append({"workload": wl, "budget_mb": mb,
                         "wbw": r["write_logi_bw_mbs"],
                         "rbw": r["read_logi_bw_mbs"],
                         "hit_rate": r["func"]["hit_rate"],
                         "wp99": r["w_p99"],
                         "index_peak_mb": r["func"].get("host_index_mb", 0)})
            print(f"[10.4] {wl}/{mb}MB: wBW={r['write_logi_bw_mbs']:.0f}",
                  flush=True)
    save("e6_hostdram.csv", rows)


# ---------------------------------------------------------------- 10.5
def exp105():
    rows = []
    tr = Trace("container")
    for tprog in (60.0, 100.0, 200.0):
        for sk, dev, flags, label in (
                ("nodedup", DeviceConfig(wi_entries_per_zone=128,
                                         t_prog_us=tprog),
                 dict(skew=True), "nodedup"),
                ("hostdedup", DeviceConfig(wi_entries_per_zone=128,
                                           t_prog_us=tprog),
                 dict(skew=True), "hostdedup-1g"),
                ("v9", DeviceConfig(wi_entries_per_zone=128, sha_hw=True,
                                    t_prog_us=tprog), V7F, "lizone-hw")):
            r = TimingSim(tr, sk, dev=dev, qd=32, read_mix_extra=0.15,
                          max_events=min(len(tr), 3_000_000), **flags).run()
            rows.append({"t_prog_us": tprog, "label": label,
                         "wbw": r["write_logi_bw_mbs"],
                         "wphys": r["write_phys_bw_mbs"],
                         "wp99": r["w_p99"], "rp99": r["r_p99"],
                         "hit_rate": r["func"]["hit_rate"]})
            print(f"[10.5] tprog={tprog:.0f}/{label}: "
                  f"wBW={r['write_logi_bw_mbs']:.0f}", flush=True)
    save("e5_nandlat.csv", rows)


# ---------------------------------------------------------------- 10.6
def exp106():
    rows = []
    for wl in ("container", "fsl", "rocksdb"):
        tr = Trace(wl)
        r = replay(tr, "v9", dev=DEV(), track_locus=True)
        rows.append({
            "workload": wl,
            "cross_zone_frac": r.get("cross_zone_desc_frac", 0),
            "incoming_peak": max(1, r.get("routing_fn_count", 0)) * 0 +
            _incoming_peak(tr),
            "sealed_hit_frac": r.get("hit_source_sealed_frac", 0),
        })
        print(f"[10.6] {wl}: cross={rows[-1]['cross_zone_frac']:.3f}",
              flush=True)
    save("e3_xzone.csv", rows)


def _incoming_peak(tr):
    from lzsim.systems import make_system
    sys_ = make_system("v9", DEV(), track_locus=False)
    peak = 0
    for i in range(min(len(tr), 3_000_000)):
        op, fp, L = tr.event(i)
        if op:
            continue
        sys_.write(fp, L)
        peak = max(peak, sys_.incoming_peak)
    return peak


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"1": exp101, "2": exp102, "3": exp103, "4": exp104,
             "5": exp105, "6": exp106}
    if which == "all":
        for k in ("1", "2", "3", "4", "5", "6"):
            steps[k]()
    else:
        steps[which]()
