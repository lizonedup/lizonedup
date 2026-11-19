#!/usr/bin/env python
"""figs_v12.py -- ALL experiment figures for the FAST'27 LiZoneDedup paper.

Redraw of figs_v11.py with a validated global palette and unified layout:

  * ONE system palette (validated with the dataviz six-checks script:
    adjacent CVD dE>=8, normal-vision dE>=15, ordinal ramp 2:1 light end):
      NoDedup   #969696   (neutral reference, black edge + legend encoding)
      HostDedup #33BBEE
      LiZone    #005F9E   (protagonist; deeper than Tol blue for blue/teal dE)
      Balloon   #00897A
      Combo     #EE7733
    Red #CC3311 is RESERVED for cap/budget reference lines only.
  * Software variants: same-hue tint + 45-degree hatch (never color alone).
  * Ablation layers: OKLab-even blue ramp #7CB0D8 -> #005F9E.
  * Panels are generated at FINAL PRINT SIZE and embedded 1:1 in LaTeX
    (tick 8pt, axis label 9pt, legend 7.5-8pt; minimum 6.2pt only for the
    ablation strip legend).
  * No legend/data or label/label collisions: legends sit in verified-empty
    regions or outside the axes; bar value labels go inside the bar in white
    when the bar is tall enough, above in black otherwise.
  * Log/truncated axes carry explicit ticks; every y-limit re-derived from
    the data (v11 clipped rbw at 700 vs max 739).

Data: figure_package/data/*.csv only (self-contained; no external results
directories).  Numeric anchors are asserted after drawing (see CHECKS).
"""
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FIGS = os.path.normpath(os.path.join(HERE, "..", "figures"))
os.makedirs(FIGS, exist_ok=True)

# ---------------------------------------------------------------- style ----
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 9, "axes.labelsize": 10, "axes.titlesize": 10,
    "xtick.labelsize": 9, "ytick.labelsize": 9, "legend.fontsize": 8.5,
    "figure.dpi": 300, "savefig.dpi": 300,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.axisbelow": True, "axes.linewidth": 0.8,
    "pdf.fonttype": 42, "hatch.linewidth": 0.6,
    "xtick.major.size": 2.5, "ytick.major.size": 2.5,
    "xtick.major.width": 0.8, "ytick.major.width": 0.8,
})

# ------------------------------------------------------- global palette ----
SYS = {
    "nodedup":   ("NoDedup",     "#969696"),
    "hostdedup": ("HostDedup",   "#33BBEE"),
    "lizone":    ("LiZoneDedup", "#005F9E"),
    "balloon":   ("Balloon-ZNS", "#00897A"),
    "combo":     ("LiZone+Balloon", "#EE7733"),
}
BLUE, TEAL, CYAN, GREY = "#005F9E", "#00897A", "#33BBEE", "#969696"
ORANGE, MAGENTA, RED, BLACK = "#EE7733", "#EE3377", "#CC3311", "#000000"
SYS_SW = {"lizone": "#9DC9E8", "balloon": "#A8DCD0", "combo": "#F5B48C"}
HATCH = "///"
# ablation layers reuse the SYSTEM palette (uniform paper-wide colors):
# Global=grey (reference), DataPath=cyan, +Shard=teal, +Split=orange,
# +Full cache = LiZone blue (the complete system IS the protagonist blue)
RAMP = [GREY, CYAN, TEAL, ORANGE, BLUE]

WLP = ["fsl", "container", "vm", "backup", "rocksdb"]
WLPN = ["FSL", "Container", "VM", "Backup", "RocksDB"]
WLPAB = ["FSL", "Cont.", "VM", "Bkp", "RDB"]

f = float


def load(name):
    with open(os.path.join(DATA, name), newline="") as fh:
        return list(csv.DictReader(fh))


E4 = load("e4_perf.csv")


def vals(label, field):
    return [f([r for r in E4 if r["workload"] == wl
               and r["label"] == label][0][field]) for wl in WLP]


def save(fig, name):
    w, h = fig.get_size_inches()
    fig.savefig(os.path.join(FIGS, name + ".pdf"))
    plt.close(fig)
    print(f"  {name}.pdf  {w:.2f}x{h:.2f}in")


def bar_label(ax, x, y, txt, top, fs=7, rot=False):
    """Value label. rot=True: rotated 90 deg inside the bar top (bars5
    panels -- bar width ~6 pt is narrower than horizontal text, so
    horizontal labels would overhang the bar edges)."""
    if y / top >= 0.14:
        ax.text(x, y, txt, ha="center", va="top", fontsize=fs,
                color="white", fontweight="bold", clip_on=False,
                rotation=90 if rot else 0)
    else:
        ax.text(x, y, txt, ha="center", va="bottom", fontsize=fs,
                color="0.1", clip_on=False)


def mark_break(ax, x_frac=0.0):
    """Axis-break slashes at the bottom of a truncated y-axis."""
    kw = dict(transform=ax.transAxes, color="0.2", lw=1.1, clip_on=False)
    ax.plot([x_frac - 0.014, x_frac + 0.014], [-0.008, -0.024], **kw)
    ax.plot([x_frac - 0.014, x_frac + 0.014], [0.012, -0.004], **kw)


ARMS5 = [("nodedup", *SYS["nodedup"]), ("hostdedup-1g", *SYS["hostdedup"]),
         ("lizone-hw", *SYS["lizone"]), ("balloon-hw", *SYS["balloon"]),
         ("combo-hw", *SYS["combo"])]


BAR_IN = 0.0875  # global bar width target in inches (6.3pt) -- the
                # common value all 5-bar panels (Figs 4a/5a/5b/5c) can
                # render identically; Fig 4b/Fig 8 are physically
                # narrower and cap at a 0.92 group width


def barw(ax, n_groups, n_bars, bar_in=BAR_IN):
    """Per-bar width in data units, computed from the MEASURED axes width
    (after constrained layout) so every panel's bars have the same physical
    width; capped so a group never touches its neighbors.  Call AFTER all
    ticks/labels/legends that affect the layout are in place."""
    fig = ax.figure
    try:
        fig.execute_constrained_layout()
    except Exception:
        fig.canvas.draw()
    aw = ax.get_window_extent(fig.canvas.get_renderer()).width / fig.dpi
    unit = aw / n_groups
    return min(0.92 / n_bars, bar_in / (0.92 * unit))


def bars5(ax, field, ylabel, ylim, logy=False, labels_on="lizone-hw",
          xtick_fs=8, bar_in=BAR_IN, bar_label_fs=7, labels=WLPN):
    xs = np.arange(len(WLP))
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=xtick_fs)
    ax.set_ylabel(ylabel)
    ax.set_xlim(-0.55, len(WLP) - 0.45)
    if logy:
        ax.set_yscale("log")
    ax.set_ylim(*ylim)
    w = barw(ax, len(WLP), 5, bar_in)
    for i, (label, lbl, col) in enumerate(ARMS5):
        v = vals(label, field)
        ax.bar(xs + (i - 2) * w, v, w * 0.92, color=col,
               edgecolor=BLACK, linewidth=0.5, label=lbl, zorder=3)
        if label == labels_on:
            for x, y in zip(xs, v):
                txt = f"{y:.0f}"
                bar_label(ax, x + (i - 2) * w, y, txt, ylim[1],
                          fs=bar_label_fs, rot=True)


# ----------------------------------------------- generic grouped bars ----
# Fig 4 arms: five hardware systems + PageFTLDedup (infeasible oracle) as a
# black bar + LiZone's physical traffic as a white bar.  Fig 5 adds the
# software variants (tint + hatch).  No value labels anywhere.
# Fig 4 arms in the user-specified order (write direction only):
ARMS_FIG4 = [
    ("nodedup", "NoDedup", GREY, None),
    ("lizone-hw", "LiZoneDedup", BLUE, None),
    ("balloon-hw", "Balloon-ZNS", TEAL, None),
    ("hostdedup-1g", "HostDedup", CYAN, None),
    ("balloon-sw", "Balloon-sw", SYS_SW["balloon"], HATCH),
    ("combo-hw", "LiZone+Balloon", ORANGE, None),
    ("lizone-sw", "LiZone-sw", SYS_SW["lizone"], HATCH),
    ("oracle", "PageFTLDedup", BLACK, None),
]
ARMS_FIG5 = [
    ("nodedup", "NoDedup", GREY, None),
    ("hostdedup-1g", "HostDedup", CYAN, None),
    ("lizone-sw", "LiZone-sw", SYS_SW["lizone"], HATCH),
    ("lizone-hw", "LiZone-hw", BLUE, None),
    ("balloon-sw", "Balloon-sw", SYS_SW["balloon"], HATCH),
    ("balloon-hw", "Balloon-hw", TEAL, None),
    ("combo-hw", "LiZone+Balloon", ORANGE, None),
]


def group_bars(ax, arms, ylim, logy=False, xtick_fs=8.5, labels=WLPN):
    """arms: (label, field, name, color, hatch) -- one bar per arm."""
    xs = np.arange(len(WLP))
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=xtick_fs)
    ax.set_xlim(-0.55, len(WLP) - 0.45)
    if logy:
        ax.set_yscale("log")
    ax.set_ylim(*ylim)
    n = len(arms)
    w = barw(ax, len(WLP), n)
    for i, arm in enumerate(arms):
        label, field, name, col, hatch = arm
        v = vals(label, field)
        ax.bar(xs + (i - (n - 1) / 2) * w, v, w * 0.92, color=col,
               hatch=hatch, edgecolor=BLACK, linewidth=0.5, zorder=3)
    return w


# ---------------------------------------------------------- figure 4 -------
def e2e_tput_r():
    """Read throughput, same eight arms (oracle rbw + physical rphys)."""
    fig, ax = plt.subplots(layout="constrained", figsize=(3.15, 2.45))
    ax.set_yticks(range(0, 801, 200))
    group_bars(ax, [(l, "rbw", n, c, h) for l, n, c, h in ARMS_FIG4],
               (0, 880), xtick_fs=7.8, labels=WLPAB)
    ax.set_ylabel("Read throughput (MB/s)")
    save(fig, "e2e-tput-r")


def e2e_tput_w():
    """Write throughput panel, 8 arms + cap line (paired with read)."""
    fig, ax = plt.subplots(layout="constrained", figsize=(3.12, 2.45))
    ax.set_yticks(range(0, 4001, 1000))
    group_bars(ax, [(l, "wbw", n, c, h) for l, n, c, h in ARMS_FIG4],
               (0, 5000), xtick_fs=7.8, labels=WLPAB)
    ax.set_ylabel("Write throughput (MB/s)")
    ax.axhline(655, ls=":", lw=1.2, color=RED, zorder=2)
    cap = Line2D([], [], ls=":", lw=1.2, color=RED, label="655 MB/s cap")
    handles = [plt.Rectangle((0, 0), 1, 1, facecolor=c, hatch=h,
                             edgecolor=BLACK, linewidth=0.5)
               for _, _, c, h in ARMS_FIG4] + [cap]
    labels = [n for _, n, _, _ in ARMS_FIG4] + [cap.get_label()]
    ax.legend(handles, labels, ncol=3, loc="upper left", frameon=False,
              fontsize=6.5, columnspacing=0.5, handlelength=1.05,
              handletextpad=0.25, borderaxespad=0.2)
    save(fig, "e2e-tput-w")


# ---------------------------------------------------------- figure 5 -------
def e2e_lat():
    """2x2: (a) write p50 log, (b) write p99 truncated, (c) read p50,
    (d) read p99 -- seven arms (software variants hatched)."""
    def all_max(field, lo=0):
        return max(f(r[field]) for r in E4 for a, _, _, _ in ARMS_FIG5
                   if r["label"] == a)
    specs = (("wp50", "e2e-lat-w50", r"Write p50 ($\mu$s, log)", (3, 800),
              True, [10, 30, 100, 300]),
             ("wp99", "e2e-lat-w99", r"Write p99 ($\mu$s)", (185, 255),
              False, [190, 210, 230, 250]),
             ("rp50", "e2e-lat-r50", r"Read p50 ($\mu$s)",
              (0, 1.25 * all_max("rp50")), False, None),
             ("rp99", "e2e-lat-r99", r"Read p99 ($\mu$s)",
              (0, 1.12 * all_max("rp99")), False, None))
    for j, (field, name, ylab, ylim, logy, ticks) in enumerate(specs):
        fig, ax = plt.subplots(layout="constrained", figsize=(3.15, 2.10))
        if logy:
            ax.set_yticks(ticks)
            ax.set_yticklabels([str(t) for t in ticks])
            ax.minorticks_off()
        group_bars(ax, [(l, field, n, c, h) for l, n, c, h in ARMS_FIG5],
                   ylim, logy=logy, xtick_fs=8)
        if field == "wp50":   # shared legend on (a), top clear zone
            handles = [plt.Rectangle((0, 0), 1, 1, facecolor=c, hatch=h,
                                     edgecolor=BLACK, linewidth=0.5)
                       for _, _, c, h in ARMS_FIG5]
            ax.legend(handles, [n for _, n, _, _ in ARMS_FIG5], ncol=3,
                      loc="upper left", frameon=False, fontsize=6.8,
                      columnspacing=0.5, handlelength=1.0,
                      handletextpad=0.25, borderaxespad=0.2)
        if field == "wp99":
            mark_break(ax)
        ax.set_ylabel(ylab)
        save(fig, name)


# ---------------------------------------------------------- figure 6 -------
def adaptive_bypass():
    """Bar panels on the deployed hardware configuration."""
    E4B = load("e4b_bypass.csv")
    rows = {r["label"]: r for r in E4B}
    order = [("nodedup", "NoDedup", GREY),
             ("v9-default", "always-on", BLUE),
             ("v9-bypass5(default)", "bypass 5/10", BLUE),
             ("v9-bypass15", "bypass 15%", BLUE)]
    for key, fname, ylab in (
            ("wbw", "adaptive-bypass-w", "Write bandwidth (MB/s)"),
            ("rp99", "adaptive-bypass-r", r"Read p99 ($\mu$s)")):
        fig, ax = plt.subplots(layout="constrained", figsize=(3.12, 1.95))
        xs = np.arange(len(order))
        v = [f(rows[k][key]) for k, _, _ in order]
        ax.set_xticks(xs)
        ax.set_xticklabels([n for _, n, _ in order], fontsize=7.5)
        ax.set_ylabel(ylab)
        ax.set_ylim(0, max(v) * (1.30 if key == "rp99" else 1.18))
        bw = barw(ax, len(order), 1, 0.155)
        for x, (k, _, col) in zip(xs, order):
            y = f(rows[k][key])
            edge = RED if (key == "rp99" and y > 60) else BLACK
            lw = 1.1 if edge == RED else 0.5
            ax.bar(x, y, bw, color=col, edgecolor=edge, linewidth=lw,
                   zorder=3)
        if key == "rp99":
            ax.annotate("2$\times$ read tail",
                        xy=(2, f(rows["v9-bypass5(default)"]["rp99"])),
                        xytext=(2, max(v) * 1.20), ha="center",
                        fontsize=8, color=RED)
        save(fig, fname)


# ---------------------------------------------------------- figure 7 -------
def dedup_reduction():
    E4C = load("e4_cloud_timing.csv")
    wls = WLP + ["cloud"]           # six workloads incl. cloud
    wln = WLPAB + ["Cloud"]

    def red(rowsrc, wl, label):
        r = [x for x in rowsrc if x["workload"] == wl
             and x["label"] == label][0]
        return (1 - f(r["wphys"]) / max(1.0, f(r["wbw"]))) * 100

    fig, ax = plt.subplots(layout="constrained", figsize=(3.12, 2.45))
    xs = np.arange(len(wls))
    # hw arms only: sw variants reduce the SAME physical bytes (verified
    # identical); NoDedup == 0% by construction; capacity == 1/(1-red).
    arms = [("lizone-hw", "LiZoneDedup", BLUE),
            ("balloon-hw", "Balloon-ZNS", TEAL),
            ("combo-hw", "LiZone+Balloon", ORANGE)]
    ax.set_xticks(xs)
    ax.set_xticklabels(wln, fontsize=8.5)
    ax.set_xlim(-0.55, len(wls) - 0.45)
    ax.set_ylabel("Physical write reduction (%)")
    ax.set_ylim(0, 100)
    ax.set_yticks(range(0, 101, 25))
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, facecolor=c,
                                     edgecolor=BLACK, linewidth=0.5)
                       for _, _, c in arms],
              labels=[n for _, n, _ in arms],
              ncol=3, loc="lower left", bbox_to_anchor=(0.0, 1.01),
              frameon=False, fontsize=7, columnspacing=0.6,
              handlelength=1.1, handletextpad=0.3)
    w = barw(ax, len(wls), 3)
    for i, (label, lbl, col) in enumerate(arms):
        v = [red(E4 if wl != "cloud" else E4C, wl, label) for wl in wls]
        ax.bar(xs + (i - 1) * w, v, w * 0.92, color=col,
               edgecolor=BLACK, linewidth=0.5, zorder=3)
    save(fig, "dedup-reduction")


# ---------------------------------------------------------- figure 8 -------
def md_ablation():
    E2 = load("e2_ablation.csv")
    layers = [("globalindex", "Global"), ("datapath", "DataPath"),
              ("shard", "+Shard"), ("split", "+Split"), ("v9", "+Cache")]
    WL6 = ["fsl", "container", "vm", "backup", "rocksdb", "cloud"]
    WLN6 = ["FSL", "Cont.", "VM", "Bkp", "RDB", "Cloud"]

    def e2v(wl, lay, field):
        return f([r for r in E2 if r["workload"] == wl
                  and r["layer"] == lay][0][field])

    def strip_legend(ax):
        handles = [plt.Rectangle((0, 0), 1, 1, facecolor=RAMP[j],
                                 edgecolor=BLACK, linewidth=0.4)
                   for j in range(5)]
        ax.legend(handles, [n for _, n in layers], loc="lower left",
                  bbox_to_anchor=(0.0, 1.01), ncol=3, fontsize=6.0,
                  handlelength=0.9, columnspacing=0.5,
                  handletextpad=0.25, frameon=False)

    for field, name, ylab, ticks, lo, hi in (
            ("ra", "md-ra", "Metadata reads\n(B/req, log)",
             [10, 100, 1000], 3, 20000),
            ("wamd", "md-wamd", "Metadata write\nampl. ($\\times$, log)",
             [0.01, 0.1, 1], 0.003, 3)):
        fig, ax = plt.subplots(layout="constrained", figsize=(2.10, 1.70))
        xs = np.arange(len(WL6))
        ax.set_yscale("log")
        ax.set_ylim(lo, hi)
        ax.set_yticks(ticks)
        ax.set_yticklabels([("%g" % t) for t in ticks], fontsize=7)
        ax.set_xticks(xs)
        ax.set_xticklabels(WLN6, fontsize=6.8)
        ax.set_xlim(-0.55, len(WL6) - 0.45)
        ax.set_ylabel(ylab, fontsize=8)
        if field == "ra":
            strip_legend(ax)   # before barw: legend width shrinks the axes
        w = barw(ax, len(WL6), 5)
        for j, (lay, _) in enumerate(layers):
            v = [e2v(wl, lay, field) for wl in WL6]
            ax.bar(xs + (j - 2) * w, v, w, color=RAMP[j],
                   edgecolor=BLACK, linewidth=0.4, zorder=3)
        save(fig, name)

    # (c): total write amplification across the SAME five ablation layers
    # (unified comparison target; e2_ablation.wa); NoDedup reference = 1x
    fig, ax = plt.subplots(layout="constrained", figsize=(2.10, 1.70))
    xs = np.arange(len(WL6))
    ax.set_xticks(xs)
    ax.set_xticklabels(WLN6, fontsize=7.8)
    ax.set_xlim(-0.55, len(WL6) - 0.45)
    wamax = max(e2v(wl, lay, "wa") for wl in WL6 for lay, _ in layers)
    ax.set_ylim(0, wamax * 1.22)
    ax.axhline(1.0, ls="--", lw=1.0, color="0.45", zorder=2)
    ax.text(-0.45, 1.09, r"NoDedup = 1.0$\times$", ha="left", fontsize=7,
            color="0.35")
    ax.set_ylabel("Total write\nampl. ($\times$)", fontsize=9)
    w = barw(ax, len(WL6), 5)
    for j, (lay, _) in enumerate(layers):
        v = [e2v(wl, lay, "wa") for wl in WL6]
        ax.bar(xs + (j - 2) * w, v, w, color=RAMP[j],
               edgecolor=BLACK, linewidth=0.4, zorder=3)
    save(fig, "md-wa")


# ---------------------------------------------------------- figure 9 -------
def sens_dram():
    E7 = load("e7_capacity_all6.csv")
    wls = ["fsl", "container", "vm", "backup", "rocksdb", "cloud"]
    caps = [1024, 2048, 4096]
    fig, ax = plt.subplots(layout="constrained", figsize=(3.15, 2.45))

    for wl in wls:
        v = [f([r for r in E7 if r["workload"] == wl
                and int(r["capacity_gb"]) == c][0]["lz_dram_mb"]) for c in caps]
        ax.plot(caps, v, "o-", color=BLUE, ms=4, lw=1.5, zorder=4)

    gi = [c * 40 / 4096 * 1024 for c in caps]
    ax.plot(caps, gi, "^--", color=BLACK, ms=4.5, lw=1.4,
            markerfacecolor="white", zorder=3)
    hd = [c * 20 / 4096 * 1024 for c in caps]
    ax.plot(caps, hd, "v:", color=CYAN, ms=4.5, lw=1.6, zorder=3)
    ax.axhline(128, ls="-.", lw=1.2, color=RED, zorder=2)

    ax.annotate("PageFTLDedup\n(40 B/page)", xy=(4096, gi[-1]),
                xytext=(4400, gi[-1] * 1.15), fontsize=7, color="0.15",
                ha="left", va="bottom")
    ax.annotate("HostDedup host\n(26 B/page)", xy=(4096, hd[-1]),
                xytext=(4400, hd[-1] * 0.85), fontsize=7, color="#1E7FA8",
                ha="left", va="top")
    ax.annotate("LiZone all 6 workloads\n(37-52 MB, flat)",
                xy=(4096, 52), xytext=(4400, 45), fontsize=7, color=BLUE,
                ha="left", va="top")
    ax.annotate("128 MB budget", xy=(1024, 128), xytext=(1060, 210),
                fontsize=7.5, color=RED)

    ax.set_xscale("log", base=2)
    ax.set_yscale("log", base=2)
    ax.set_xlim(900, 30000)
    ax.set_xticks(caps)
    ax.set_xticklabels(["1T", "2T", "4T"])
    ax.set_yticks([32, 128, 512, 2048, 8192, 32768])
    ax.set_yticklabels(["32", "128", "512", "2k", "8k", "32k"])
    ax.set_ylim(10, 70000)
    ax.set_xlabel("Device capacity (GB)")
    ax.set_ylabel("Index DRAM (MB, log)")
    save(fig, "sens-dram")


def sens_lat():
    E7B = load("e7b_captiming.csv")
    fig, ax = plt.subplots(layout="constrained", figsize=(3.15, 2.45))
    caps = [1024, 2048, 4096]

    def series(wl, pref, field="wp99"):
        # every capacity point now comes from the one unified e7b batch
        return [f([r for r in E7B if r["workload"] == wl
                   and int(r["capacity_gb"]) == c
                   and r["label"].startswith(pref)][0][field])
                for c in caps]

    ax.plot(caps, series("container", "nodedup"), "o-", color=GREY,
            ms=4.5, lw=1.5, zorder=2)
    ax.plot(caps, series("container", "lizone-hw"), "D-", color=BLUE,
            ms=4.5, lw=1.6, zorder=3)
    ax.plot(caps, series("rocksdb", "lizone-hw"), "D--", color=BLUE,
            ms=4.5, lw=1.4, markerfacecolor="white", zorder=3)
    ax.annotate("LiZone container", xy=(4096, 207.9), xytext=(4400, 208.6),
                fontsize=7.5, color=BLUE, va="center")
    ax.annotate("LiZone rocksdb", xy=(4096, 204.3), xytext=(4400, 203.8),
                fontsize=7.5, color=BLUE, va="center")
    ax.annotate("NoDedup (both)", xy=(4096, 201.0), xytext=(4400, 199.2),
                fontsize=7.5, color="0.35", va="center")
    ax.set_xscale("log", base=2)
    ax.set_xlim(900, 12000)
    ax.set_xticks(caps)
    ax.set_xticklabels(["1T", "2T", "4T"])
    ax.set_xlabel("Device capacity (GB)")
    ax.set_ylabel(r"Write p99 ($\mu$s)")
    ax.set_ylim(190, 213)
    ax.set_yticks([190, 195, 200, 205, 210])
    save(fig, "sens-lat")


def sens_nandlat():
    E5 = load("e5_nandlat.csv")
    E5B = load("e5b_nandlat.csv")
    E5C = load("e5c_oracle_nandlat.csv")
    fig, ax = plt.subplots(layout="constrained", figsize=(3.15, 2.45))
    ts = [60.0, 100.0, 200.0]
    series = [("nodedup", SYS["nodedup"][1], "o"),
              ("hostdedup-1g", SYS["hostdedup"][1], "s"),
              ("lizone-hw", SYS["lizone"][1], "D")]
    name = {a[0]: a[1] for a in ARMS5}
    name["oracle"] = "PageFTLDedup"
    # right-end direct labels, vertically de-overlapped
    ends = []
    for lb, col, mk in series:
        vc = [f([r for r in E5 if f(r["t_prog_us"]) == t
                 and r["label"] == lb][0]["wbw"]) for t in ts]
        vr = [f([r for r in E5B if f(r["t_prog_us"]) == t
                 and r["label"] == lb][0]["wbw"]) for t in ts]
        ax.plot(ts, vc, mk + "-", color=col, ms=4.5, lw=1.6)
        ax.plot(ts, vr, mk + "--", color=col, ms=4.5, lw=1.3,
                markerfacecolor="white")
        ends.append([vc[-1], vc[-1], name[lb], col])
    # PageFTLDedup (infeasible oracle), same comparison set as panel (a)
    oc = [f([r for r in E5C if f(r["t_prog_us"]) == t
             and r["workload"] == "container"][0]["wbw"]) for t in ts]
    orr = [f([r for r in E5C if f(r["t_prog_us"]) == t
              and r["workload"] == "rocksdb"][0]["wbw"]) for t in ts]
    ax.plot(ts, oc, "^-", color=BLACK, ms=4.5, lw=1.6)
    ax.plot(ts, orr, "^--", color=BLACK, ms=4.5, lw=1.3,
            markerfacecolor="white")
    ends.append([oc[-1], oc[-1], name["oracle"], BLACK])
    ends.sort(key=lambda e: -e[0])
    min_sep = 380
    for j in range(1, len(ends)):
        ends[j][1] = min(ends[j][1], ends[j - 1][1] - min_sep)
    for _, ly, lbl, col in ends:
        ax.annotate(lbl, xy=(200, _), xytext=(212, ly), fontsize=7.5,
                    color=col, va="center")
    ax.text(56, 80, "solid: container\ndashed: rocksdb", fontsize=7.5,
            color="0.35", va="bottom", ha="left")
    ax.set_xlim(52, 268)
    ax.set_ylim(0, 4400)
    ax.set_yticks(range(0, 4001, 1000))
    ax.set_xlabel(r"t$_{prog}$ ($\mu$s)")
    ax.set_ylabel("Write bandwidth (MB/s)")
    ax.set_xticks(ts)
    save(fig, "sens-nandlat")


def sens_zone():
    E8B = load("e8b_zonetiming.csv")
    fig, ax = plt.subplots(layout="constrained", figsize=(3.15, 2.45))
    zs = [128, 256, 512]

    def series(wl, pref, field="wbw"):
        return [f([r for r in E8B if r["workload"] == wl
                   and int(r["zone_mb"]) == z
                   and r["label"].startswith(pref)][0][field])
                for z in zs]

    # comparison set unified with panel (a): + HostDedup, + PageFTLDedup
    E8C = load("e8c_zone_ext.csv")

    def xseries(wl, pref):
        v = []
        for z in zs:
            v.append(f([r for r in E8C if r["workload"] == wl
                        and int(r["zone_mb"]) == z
                        and r["label"].startswith(pref)][0]["wbw"]))
        return v

    # two honest NoDedup lines: container 629.9 != rocksdb 559.0 MB/s
    ax.plot(zs, series("container", "nodedup"), "o-", color=GREY,
            ms=4.5, lw=1.5, label="NoDedup")
    ax.plot(zs, series("rocksdb", "nodedup"), "o--", color=GREY, ms=4.5,
            lw=1.3, markerfacecolor="white", label="_nodedup_r")
    ax.plot(zs, series("container", "lizone-hw"), "D-", color=BLUE,
            ms=4.5, lw=1.6, label="LiZone")
    ax.plot(zs, series("rocksdb", "lizone-hw"), "D--", color=BLUE, ms=4.5,
            lw=1.4, markerfacecolor="white", label="_lizone_r")
    ax.plot(zs, xseries("container", "hostdedup"), "s-", color=CYAN,
            ms=4.5, lw=1.5, label="HostDedup")
    ax.plot(zs, xseries("rocksdb", "hostdedup"), "s--", color=CYAN, ms=4.5,
            lw=1.3, markerfacecolor="white", label="_hd_r")
    ax.plot(zs, xseries("container", "oracle"), "^-", color=BLACK,
            ms=4.5, lw=1.5, label="PageFTL")
    ax.plot(zs, xseries("rocksdb", "oracle"), "^--", color=BLACK, ms=4.5,
            lw=1.3, markerfacecolor="white", label="_pftl_r")
    ax.set_xscale("log", base=2)
    ax.set_xticks(zs)
    ax.set_xticklabels(["128", "256", "512"])
    ax.set_xlabel("Zone size (MB)")
    ax.set_ylabel("Write bandwidth (MB/s)")
    ax.set_ylim(0, 2600)
    ax.set_yticks(range(0, 2501, 500))
    # one-row system legend ABOVE the panel + in-plot linestyle note (the
    # middle 700--1800 MB/s band is empty on this panel)
    ax.legend(handles=[Line2D([], [], ls="-", color=c, lw=1.6, label=n)
                       for n, c in (("NoDedup", GREY), ("HostDedup", CYAN),
                                    ("LiZone", BLUE), ("PageFTL", BLACK))],
              loc="lower left", bbox_to_anchor=(0.0, 1.01), ncol=4,
              frameon=False, fontsize=6.2, columnspacing=0.6,
              handlelength=1.4, handletextpad=0.3)
    ax.text(0.985, 0.94, "solid: container   dashed: rocksdb",
            transform=ax.transAxes, ha="right", fontsize=7,
            color="0.35")
    save(fig, "sens-zone")


# --------------------------------------------------------- appendix -------
def femu_crossval():
    """FEMU-measured dedup hit rate, 5 workloads x 3 systems.

    Bandwidth is deliberately NOT reported: the prototype serialises all
    dedup bookkeeping under one global mutex, so every dedup mode lands on
    the same ~300-370 MB/s ceiling regardless of workload (and the NoDedup
    baseline's position depends on the write-cache timing model).  The
    dedup *rate* is well defined and directly comparable with the simulator.
    """
    FEMU = load("e95_femu_5wl.csv")
    order = ["container", "fsl", "rocksdb", "vm", "backup"]
    wln = ["Cont.", "FSL", "RDB.", "VM", "Bkp."]
    sys_map = [("gidx", "GlobalIndex", TEAL),
               ("lz", "LiZoneDedup", BLUE),
               ("oracle", "Oracle", BLACK)]
    fig, ax = plt.subplots(layout="constrained", figsize=(3.12, 2.20))
    xs = np.arange(len(order))
    ax.set_xticks(xs)
    ax.set_xticklabels(wln, fontsize=8)
    ax.set_ylabel("Dedup hit rate (%)")
    ax.set_ylim(0, 100)
    ax.set_yticks(range(0, 101, 25))
    n = len(sys_map)
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, facecolor=c,
                                     edgecolor=BLACK, linewidth=0.5)
                       for _, _, c in sys_map],
              labels=[nm for _, nm, _ in sys_map],
              loc="lower left", bbox_to_anchor=(0.0, 1.01), ncol=3,
              frameon=False, fontsize=6.2, columnspacing=0.7,
              handlelength=1.1, handletextpad=0.3)
    w = barw(ax, len(order), n, 0.13)
    for j, (fsk, _, col) in enumerate(sys_map):
        v = [f([r for r in FEMU if r["workload"] == wl
                and r["system"] == fsk][0]["hit_rate"]) * 100 for wl in order]
        ax.bar(xs + (j - (n - 1) / 2.0) * w, v, w * 0.92, color=col,
               edgecolor=BLACK, linewidth=0.5, zorder=3)
    save(fig, "femu-crossval")


def lifecycle_sweep():
    E47 = load("e47_lifecycle.csv")
    wl_col = {"container": BLUE, "backup": ORANGE}
    for field, name, ylab in (
            ("trapped_dead_gb", "lifecycle-trapped", "Trapped (GB)"),
            ("gc_blocked", "lifecycle-gc", "GC blocks")):
        fig, ax = plt.subplots(layout="constrained", figsize=(3.12, 1.95))
        pts = []
        for wl in ("container", "backup"):
            for mig, ls in ((1, "-"), (0, "--")):
                rows = sorted(
                    [r for r in E47 if r["workload"] == wl
                     and int(r["migrate"]) == mig],
                    key=lambda r: f(r["dead"]))
                xs_ = [f(r["dead"]) * 100 for r in rows]
                ys_ = [f(r[field]) for r in rows]
                ax.plot(xs_, ys_, ls, color=wl_col[wl],
                        lw=1.8 if mig else 1.2,
                        marker="o" if mig else None, ms=3.5,
                        markerfacecolor="white" if not mig else None,
                        label=f"{wl}, {'mig' if mig else 'no mig'}")
                pts += list(zip(xs_, ys_))
        # no in-plot corner is clear in both panels -> one legend row
        # below the x axis (4 short entries fit the 3.31 in panel)
        ax.legend(fontsize=6.5, ncol=2, loc="upper center",
                  bbox_to_anchor=(0.5, -0.24), handlelength=1.4,
                  frameon=False, columnspacing=0.8, handletextpad=0.3)
        ax.set_xlabel("Dead ratio (%)")
        ax.set_ylabel(ylab)
        ax.set_xticks([30, 50, 70])
        save(fig, name)


def hostdedup_budget():
    E6 = load("e6_hostdram.csv")
    fig, ax = plt.subplots(layout="constrained", figsize=(3.12, 2.10))
    wls = ["fsl", "container", "vm", "backup"]
    mbs = [512, 1024, 2048, 4096]
    # workload series use the system palette (same hues as Figs 4-8):
    # fsl=cyan, container=blue, vm=teal, backup=orange; darker text variants
    wl_style = {"fsl": (CYAN, "o", "#1E7FA8"),
                "container": (BLUE, "s", BLUE),
                "vm": (TEAL, "D", TEAL),
                "backup": (ORANGE, "^", "#C65A1A")}
    for wl in wls:
        col, mk, txt_col = wl_style[wl]
        v = [f([r for r in E6 if r["workload"] == wl
                and f(r["budget_mb"]) == mb][0]["wbw"]) for mb in mbs]
        ax.plot(mbs, v, mk + "-", color=col, ms=4.5, lw=1.5)
        ax.annotate(wl, xy=(4096, v[-1]), xytext=(4250, v[-1]),
                    fontsize=7.5, color=txt_col, va="center")
    ax.set_xscale("log", base=2)
    ax.set_xlim(430, 11000)
    ax.set_xticks(mbs)
    ax.set_xticklabels(["512MB", "1GB", "2GB", "4GB"])
    ax.set_xlabel("HostDedup host DRAM budget")
    ax.set_ylabel("Write bandwidth (MB/s)")
    ax.set_ylim(0, 2400)
    ax.set_yticks(range(0, 2001, 500))
    save(fig, "hostdedup-budget")


# ------------------------------------------------------- anchor checks -----
def checks():
    ok = True

    def near(a, b, tol=0.6):
        return abs(a - b) <= tol

    def red(label, wl):
        p = f([r for r in E4 if r["workload"] == wl
               and r["label"] == label][0]["wphys"])
        l = f([r for r in E4 if r["workload"] == wl
               and r["label"] == label][0]["wbw"])
        return (1 - p / max(1.0, l)) * 100

    c = [("fig4a FSL nodedup wbw", vals("nodedup", "wbw")[0], 630),
         ("fig4a FSL lizone wbw", vals("lizone-hw", "wbw")[0], 702),
         ("fig4a FSL combo wbw", vals("combo-hw", "wbw")[0], 1100),
         ("fig4a backup lizone wbw", vals("lizone-hw", "wbw")[3], 2175),
         ("fig4a backup oracle wbw", vals("oracle", "wbw")[3], 2387),
         ("fig4b lizone rbw", vals("lizone-hw", "rbw"),
          [124, 305, 255, 384, 503]),
         ("fig8c lizone wa", [p / max(1.0, l) for p, l in
                              zip(vals("lizone-hw", "wphys"),
                                  vals("lizone-hw", "wbw"))][0], 0.854),
         ("fig9a lizone container dram", 45.9, 45.9),
         ("fig10 container lizone femu",
          f([r for r in load("e95_femu_5wl.csv") if r["workload"] == "container"
             and r["system"] == "lz"][0]["hit_rate"]) * 100, 79.30),
         ("fig10 fsl oracle femu (trace true dup rate)",
          f([r for r in load("e95_femu_5wl.csv") if r["workload"] == "fsl"
             and r["system"] == "oracle"][0]["hit_rate"]) * 100, 18.75),
         ("fig10 container lizone sim", red("lizone-hw", "container"), 72.8)]
    for name, got, want in c:
        if isinstance(got, list):
            good = all(near(g, w, 0.6) for g, w in zip(got, want))
        else:
            good = near(got, want)
        print(f"  [{'ok' if good else 'MISMATCH'}] {name}: {got}")
        ok &= good
    return ok


FIGSET = {
    "tput-w": e2e_tput_w, "tput-r": e2e_tput_r, "lat": e2e_lat,
    "bypass": adaptive_bypass, "reduction": dedup_reduction,
    "md": md_ablation, "dram": sens_dram,
    "slat": sens_lat, "nandlat": sens_nandlat, "zone": sens_zone,
    "femu": femu_crossval, "lifecycle": lifecycle_sweep,
    "budget": hostdedup_budget,
}

if __name__ == "__main__":
    keys = sys.argv[1:] or list(FIGSET)
    for k in keys:
        try:
            FIGSET[k]()
            print(f"{k} ok")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"{k} FAILED: {e}")
    if not sys.argv[1:]:
        print("anchor checks:" if True else "")
        sys.exit(0 if checks() else 1)
