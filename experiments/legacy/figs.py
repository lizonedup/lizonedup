"""
figs.py -- generate paper Figures 1-7 from results CSVs.

Figure 1 : dedup recall vs Oracle (r=1/2/3 + oracle line), 3 workloads.
Figure 2 : (a) metadata read amplification, (b) metadata write
           amplification, (c) total WA comparison.
Figure 3 : write/read logical throughput (bar tops: physical NAND BW).
Figure 4 : p99 write/read latency (log scale, stacked splits).
Figure 5 : physical write reduction + effective capacity gain.
Figure 6 : (a) r sensitivity (recall + lookup cost), (b) capacity scaling
           (DRAM + lookup latency).
Figure 7 : (a) reset-pending rate & duration vs dead ratio, (b) GC
           migrated bytes & foreground dip vs dead ratio.
"""

import csv
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "..", "results")
FIGS = os.path.join(HERE, "..", "figures")

plt.rcParams.update({
    "font.size": 9, "axes.grid": True, "grid.alpha": 0.3,
    "figure.dpi": 150, "savefig.bbox": "tight",
    "axes.axisbelow": True, "legend.framealpha": 0.9,
})
C = {"blue": "#4477AA", "cyan": "#66CCEE", "green": "#228833",
     "yellow": "#CCBB44", "red": "#EE6677", "purple": "#AA3377",
     "grey": "#BBBBBB", "dark": "#222222"}


def load(name):
    with open(os.path.join(RESULTS, name), newline="") as f:
        return list(csv.DictReader(f))


def f(x, d=float):
    try:
        return d(x)
    except (TypeError, ValueError):
        return d(0)


# ---------------------------------------------------------------- Fig 1
def fig1():
    rows = load("exp42_recall.csv")
    wls = ["fsl", "container", "vm"]
    rs = [1, 2, 3]
    fig, ax = plt.subplots(figsize=(4.2, 2.6))
    w = 0.18
    xs = np.arange(len(wls))
    for i, r_ in enumerate(rs):
        vals, tops = [], []
        for wl in wls:
            m = [x for x in rows
                 if x["workload"] == wl and f(x["r"], int) == r_]
            vals.append(f(m[0]["recall"]) * 100 if m else 0)
            tops.append(m[0] if m else None)
        bars = ax.bar(xs + (i - 1) * w, vals, w,
                      label=f"LiZoneDedup r={r_}",
                      color=[C["blue"], C["cyan"], C["green"]][i])
        for b, v, t in zip(bars, vals, tops):
            if t:
                ax.text(b.get_x() + b.get_width() / 2, v + 0.6,
                        f"{v:.1f}", ha="center", fontsize=7)
    ax.axhline(100, color=C["dark"], ls="--", lw=1, label="GlobalDedup-Oracle (100%)")
    ax.set_xticks(xs)
    ax.set_xticklabels(["FSL Homes", "Container", "VM snapshots"])
    ax.set_ylabel("Dedup recall vs. Oracle (%)")
    ax.set_ylim(0, 108)
    ax.legend(fontsize=7, ncol=2, loc="lower left")
    fig.savefig(os.path.join(FIGS, "fig1_recall.pdf"))
    fig.savefig(os.path.join(FIGS, "fig1_recall.png"))
    plt.close(fig)


# ---------------------------------------------------------------- Fig 2
SYSKEY = {"GlobalIndex": "globalindex", "DataPathOnly": "datapath",
          "+IdentityShard": "shard", "+SplitState": "split",
          "Full LiZoneDedup": "lizone"}
ABKEY = {"globalindex": "GlobalIndex", "datapath": "DataPathOnly",
         "shard": "+IdentityShard", "split": "+SplitState",
         "lizone": "Full LiZoneDedup"}


def fig2():
    ab = load("exp43_ablation.csv")
    wa = load("exp43_wa.csv")
    layers = ["globalindex", "datapath", "shard", "split", "lizone"]
    wls = ["fsl", "container", "vm", "backup"]
    wlcolors = [C["yellow"], C["blue"], C["green"], C["purple"]]

    fig, axes = plt.subplots(1, 3, figsize=(9.6, 2.7))
    xs = np.arange(len(layers))
    w = 0.2

    def abval(wl, layer, field):
        for x in ab:
            if x["workload"] == wl and ABKEY.get(x["system"].lower()
                                                 .replace("lizonededup",
                                                          "lizone")
                                                 .replace("+identityshard",
                                                          "shard")
                                                 .replace("+splitstate",
                                                          "split")
                                                 .replace("datapathonly",
                                                          "datapath")) == \
                    ABKEY[layer]:
                return f(x[field])
        return 0.0

    # (a) RA_md
    ax = axes[0]
    for j, wl in enumerate(wls):
        vals = [abval(wl, k, "md_read_B_per_req") for k in layers]
        ax.bar(xs + (j - 1.5) * w, vals, w, label=wl, color=wlcolors[j])
    ax.set_yscale("log")
    ax.set_xticks(xs)
    ax.set_xticklabels(["Global\nIndex", "DataPath\nOnly", "+Identity\nShard",
                        "+Split\nState", "Full\nLiZone"], fontsize=7)
    ax.set_ylabel("Metadata NAND read (B/req, log)")
    ax.set_title("(a) metadata read amplification", fontsize=8)
    ax.legend(fontsize=6)

    # (b) WA_md
    ax = axes[1]
    for j, wl in enumerate(wls):
        vals = [abval(wl, k, "md_write_B_per_data_B") for k in layers]
        ax.bar(xs + (j - 1.5) * w, vals, w, label=wl, color=wlcolors[j])
    ax.set_yscale("log")
    ax.set_xticks(xs)
    ax.set_xticklabels(["Global\nIndex", "DataPath\nOnly", "+Identity\nShard",
                        "+Split\nState", "Full\nLiZone"], fontsize=7)
    ax.set_ylabel("Metadata write (B per data B, log)")
    ax.set_title("(b) metadata write amplification", fontsize=8)

    # (c) total WA
    ax = axes[2]
    systems = ["NoDedup-ZNS", "LiZoneDedup", "Balloon-ZNS", "HostDedup"]
    xs2 = np.arange(len(systems))
    for j, wl in enumerate(wls):
        vals = []
        for s in systems:
            m = [x for x in wa if x["workload"] == wl and x["system"] == s]
            vals.append(f(m[0]["total_wa"]) if m else 1.0)
        ax.bar(xs2 + (j - 1.5) * w, vals, w, label=wl, color=wlcolors[j])
    ax.set_xticks(xs2)
    ax.set_xticklabels(["NoDedup", "LiZone\nDedup", "Balloon\n-ZNS",
                        "Host\nDedup"], fontsize=7)
    ax.set_ylabel("Total write amplification")
    ax.set_title("(c) total WA", fontsize=8)
    fig.savefig(os.path.join(FIGS, "fig2_metadata.pdf"))
    fig.savefig(os.path.join(FIGS, "fig2_metadata.png"))
    plt.close(fig)


# ---------------------------------------------------------------- Fig 3
def fig3():
    rows = load("exp44_perf.csv")
    wls = ["fsl", "container", "vm", "rocksdb"]
    systems = [("nodedup", "NoDedup"), ("hostdedup", "HostDedup"),
               ("lizone", "LiZoneDedup"), ("balloon", "Balloon-ZNS"),
               ("lizone-balloon", "LiZone+Balloon")]
    colors = [C["grey"], C["yellow"], C["blue"], C["green"], C["purple"]]
    fig, axes = plt.subplots(1, 2, figsize=(8.2, 2.6))
    xs = np.arange(len(wls))
    w = 0.16
    for side, (ax, field) in enumerate(zip(axes, ("w_bw_mbs",
                                                  "r_bw_mbs"))):
        for i, (sk, lbl) in enumerate(systems):
            vals, phys = [], []
            for wl in wls:
                m = [x for x in rows if x["workload"] == wl
                     and x["system"] == sk]
                vals.append(f(m[0][field]) / 1000 if m else 0)
                phys.append(f(m[0]["w_phys_mbs"]) / 1000 if m else 0)
            bars = ax.bar(xs + (i - 2) * w, vals, w, label=lbl,
                          color=colors[i])
            for b, v, p in zip(bars, vals, phys):
                if side == 0:
                    ax.text(b.get_x() + b.get_width() / 2, v + 0.03,
                            f"{p:.1f}", ha="center", fontsize=5.5,
                            rotation=90, color=C["dark"])
        ax.set_xticks(xs)
        ax.set_xticklabels(["FSL", "Container", "VM", "RocksDB"], fontsize=8)
        ax.set_ylabel("Logical throughput (GB/s)")
        ax.set_title(("write" if side == 0 else "read") +
                     (" (top labels: physical GB/s)" if side == 0 else ""),
                     fontsize=8)
        ax.set_ylim(0, 3.2)
    axes[0].legend(fontsize=6, ncol=2)
    fig.savefig(os.path.join(FIGS, "fig3_throughput.pdf"))
    fig.savefig(os.path.join(FIGS, "fig3_throughput.png"))
    plt.close(fig)


# ---------------------------------------------------------------- Fig 4
def fig4():
    rows = load("exp44_perf.csv")
    wls = ["fsl", "container", "vm", "rocksdb"]
    systems = [("nodedup", "NoDedup"), ("hostdedup", "HostDedup"),
               ("lizone", "LiZoneDedup"), ("balloon", "Balloon-ZNS"),
               ("lizone-balloon", "LiZone+Balloon")]
    colors = [C["grey"], C["yellow"], C["blue"], C["green"], C["purple"]]
    fig, axes = plt.subplots(1, 2, figsize=(8.2, 2.6))
    xs = np.arange(len(wls))
    w = 0.16
    for side, ax in enumerate(axes):
        for i, (sk, lbl) in enumerate(systems):
            base, extra = [], []
            for wl in wls:
                m = [x for x in rows if x["workload"] == wl
                     and x["system"] == sk]
                if m:
                    p99 = f(m[0]["w_p99" if side == 0 else "r_p99"])
                    if side == 0 and sk == "lizone":
                        # split: duplicate-hit part vs unique/miss part
                        hit = f(m[0]["hit_lat_us"])
                        frac = f(m[0]["hit_rate"])
                        extra.append(hit)
                        base.append(max(p99 - hit, 1.0))
                    elif side == 1 and sk == "lizone":
                        d = f(m[0]["desc_read_lat_us"])
                        frac = f(m[0]["desc_read_frac"])
                        extra.append(d * frac)
                        base.append(max(p99 - d * frac, 1.0))
                    else:
                        base.append(p99)
                        extra.append(0)
                else:
                    base.append(1)
                    extra.append(0)
            pos = xs + (i - 2) * w
            ax.bar(pos, base, w, label=lbl, color=colors[i])
            if sk == "lizone":
                ax.bar(pos, extra, w, bottom=base, color=colors[i],
                       alpha=0.45, hatch="//")
        ax.set_yscale("log")
        ax.set_ylim(10, 3000)
        ax.set_xticks(xs)
        ax.set_xticklabels(["FSL", "Container", "VM", "RocksDB"], fontsize=8)
        ax.set_ylabel("p99 latency (µs, log)")
        ax.set_title(("write" if side == 0 else "read"), fontsize=8)
    axes[0].legend(fontsize=6, ncol=2)
    fig.savefig(os.path.join(FIGS, "fig4_p99.pdf"))
    fig.savefig(os.path.join(FIGS, "fig4_p99.png"))
    plt.close(fig)


# ---------------------------------------------------------------- Fig 5
def fig5():
    rows = [r for r in load("exp45_compress.csv")
            if not r["system"].startswith("typecr:")]
    wls = ["fsl", "container", "vm", "rocksdb"]
    systems = [("nodedup", "NoDedup"), ("lizone", "LiZoneDedup"),
               ("balloon", "Balloon-ZNS"), ("lizone-balloon", "Combo")]
    colors = [C["grey"], C["blue"], C["green"], C["purple"]]
    fig, ax = plt.subplots(figsize=(5.4, 2.7))
    xs = np.arange(len(wls))
    w = 0.2
    for i, (sk, lbl) in enumerate(systems):
        vals, caps = [], []
        for wl in wls:
            m = [x for x in rows if x["workload"] == wl and x["system"] == sk]
            red = f(m[0]["phys_reduction"]) if m else 0.0
            vals.append(red * 100)
            # capacity gain: logical/physical - 1, plus OP elimination for
            # compression-capable systems
            cap = (1.0 / max(1e-9, 1.0 - red) - 1.0) * 100
            if sk in ("balloon", "lizone-balloon"):
                cap += 20.0
            caps.append(cap)
        bars = ax.bar(xs + (i - 1.5) * w, vals, w, label=lbl, color=colors[i])
        ax.plot(xs + (i - 1.5) * w, caps, "o--", color=colors[i], lw=1.1,
                ms=3.5, alpha=0.8)
    ax.set_xticks(xs)
    ax.set_xticklabels(["FSL", "Container", "VM", "RocksDB"], fontsize=8)
    ax.set_ylabel("Physical write reduction (%)")
    ax.legend(fontsize=6.5, ncol=2)
    fig.savefig(os.path.join(FIGS, "fig5_compress.pdf"))
    fig.savefig(os.path.join(FIGS, "fig5_compress.png"))
    plt.close(fig)


# ---------------------------------------------------------------- Fig 6
def fig6():
    rr = load("exp46_r.csv")
    cap = load("exp46_capacity.csv")
    fig, axes = plt.subplots(1, 2, figsize=(8.2, 2.6))

    ax = axes[0]
    wls = ["fsl", "container", "vm"]
    colors = [C["yellow"], C["blue"], C["green"]]
    ax2 = ax.twinx()
    ax2.grid(False)
    for wl, c in zip(wls, colors):
        m = sorted([x for x in rr if x["workload"] == wl],
                   key=lambda x: f(x["r"], int))
        rs = [f(x["r"], int) for x in m]
        rec = [f(x["recall"]) * 100 for x in m]
        cost = [f(x["lookup_us"]) for x in m]
        ax.plot(rs, rec, "o-", color=c, label=f"{wl} recall", ms=4)
        ax2.plot(rs, cost, "s--", color=c, alpha=0.55, ms=3)
    ax.set_xlabel("candidate set size r")
    ax.set_ylabel("Dedup recall (%)")
    ax2.set_ylabel("Lookup cost (µs/req)")
    ax.set_xticks([1, 2, 3, 4])
    ax.legend(fontsize=6.5, loc="lower right")
    ax.set_title("(a) r sensitivity", fontsize=8)

    ax = axes[1]
    ax2 = ax.twinx()
    ax2.grid(False)
    caps = [f(x["capacity_gb"], int) for x in cap]
    lz = [f(x["lizone_dram_mb"]) for x in cap]
    gz = [f(x["globalindex_dram_gb"]) * 1024 for x in cap]
    lu = [f(x["lizone_lookup_us"]) for x in cap]
    gu = [f(x["globalindex_lookup_us"]) for x in cap]
    ax.plot(caps, lz, "o-", color=C["blue"], label="LiZoneDedup DRAM (MB)")
    ax.plot(caps, gz, "s-", color=C["red"], label="GlobalIndex DRAM (MB)")
    ax.set_yscale("log")
    ax.set_xlabel("Device capacity (GB)")
    ax.set_ylabel("Controller/host DRAM (MB, log)")
    ax2.plot(caps, lu, "^--", color=C["cyan"], label="LiZone lookup (µs)")
    ax2.plot(caps, gu, "v--", color=C["purple"], label="GlobalIndex (µs)")
    ax2.set_ylabel("Avg lookup latency (µs)")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=6, loc="center left")
    ax.set_title("(b) capacity scaling", fontsize=8)
    fig.savefig(os.path.join(FIGS, "fig6_sensitivity.pdf"))
    fig.savefig(os.path.join(FIGS, "fig6_sensitivity.png"))
    plt.close(fig)


# ---------------------------------------------------------------- Fig 7
def fig7():
    rows = load("exp47_lifecycle.csv")
    fig, axes = plt.subplots(1, 2, figsize=(8.4, 2.6))

    ax = axes[0]
    ax2 = ax.twinx()
    ax2.grid(False)
    for wl, c in (("backup", C["blue"]), ("container", C["green"])):
        m = sorted([x for x in rows if x["workload"] == wl
                    and x["system"] == "LiZoneDedup"],
                   key=lambda x: f(x["dead_ratio"]))
        dr = [f(x["dead_ratio"]) * 100 for x in m]
        pr = [f(x["pending_rate"]) * 100 for x in m]
        dur = [f(x["pending_dur_gb_mean"]) for x in m]
        ax.plot(dr, pr, "o-", color=c, label=f"{wl} pending rate (%)", ms=4)
        ax2.plot(dr, dur, "s--", color=c, alpha=0.6, ms=3)
    ax.set_xlabel("Zone dead ratio (%)")
    ax.set_ylabel("Reset-pending occurrence (%)")
    ax2.set_ylabel("Pending duration (GB written)")
    ax.legend(fontsize=6.5)
    ax.set_title("(a) reset-pending vs dead ratio", fontsize=8)

    ax = axes[1]
    xs = None
    w = 0.35
    for i, (sysname, c) in enumerate((("LiZoneDedup", C["blue"]),
                                      ("NoDedup-ZNS", C["grey"]))):
        m = sorted([x for x in rows if x["workload"] == "backup"
                        and x["system"] == sysname],
                   key=lambda x: f(x["dead_ratio"]))
        dr = [f(x["dead_ratio"]) * 100 for x in m]
        gb = [f(x["gc_migrated_gb"]) for x in m]
        dip = [f(x["gc_dip_frac"]) * 100 for x in m]
        if xs is None:
            xs = np.arange(len(dr))
        ax.bar(xs + (i - 0.5) * w, gb, w, label=sysname, color=c)
        ax2 = ax if False else ax
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{d:.0f}%" for d in dr])
    ax.set_xlabel("Zone dead ratio (Backup)")
    ax.set_ylabel("GC migrated data (GB)")
    ax.legend(fontsize=7)
    ax.set_title("(b) GC migration vs dead ratio", fontsize=8)
    fig.savefig(os.path.join(FIGS, "fig7_lifecycle.pdf"))
    fig.savefig(os.path.join(FIGS, "fig7_lifecycle.png"))
    plt.close(fig)


if __name__ == "__main__":
    os.makedirs(FIGS, exist_ok=True)
    which = sys.argv[1:] or ["1", "2", "3", "4", "5", "6", "7"]
    for n in which:
        try:
            globals()[f"fig{n}"]()
            print(f"fig{n} ok")
        except Exception as e:
            print(f"fig{n} FAILED: {e}")
