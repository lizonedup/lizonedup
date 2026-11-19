"""
optimizations.py -- the five closing-the-loop optimizations (paper Sec 4.8,
from "fast优化设计"):

  opt 1  LiZoneBypass   : dynamic pipeline bypass for low-dup workloads
  opt 2  (timing-level) : hot descriptor pre-copy + redirect table
                          (see timing.TimingSim(precopy=True))
  opt 3  (lifecycle)    : targeted migration of pinned zones
                          (see lifecycle.LifecycleSim(migrate=True))
  opt 4  (lifecycle)    : reserve open-zone pool -- measured as pressure
                          samples n_active + pending vs cap 16 / 16+4
  opt 5  LiZoneCold     : bounded cold fingerprint index for zones that
                          aged out of the r-candidate window
"""
from __future__ import annotations

from collections import OrderedDict
from typing import List, Optional

from .device import DeviceConfig, DEFAULT_DEVICE
from .systems import (LiZoneDedup, LZZone, ReqOutcome, SEALED, ACTIVE, DEAD,
                      SYSTEMS)


# ---------------------------------------------------------------------------
# Opt 1: dynamic bypass
# ---------------------------------------------------------------------------

class LiZoneBypass(LiZoneDedup):
    """Adaptive dedup bypass.  A sliding window (1024 writes) tracks the
    hit rate; two consecutive windows below 5% turn the whole pipeline off
    (no hashing, no routing, no filter probes -- plain zone_append, no
    descriptors).  While bypassed, every 64th write still runs the full
    pipeline as a sampling probe; a probe window above 10% re-enables
    dedup."""
    name = "LiZoneDedup-Bypass"
    WINDOW = 1024
    OFF_TH = 0.05
    ON_TH = 0.10
    PROBE_EVERY = 64

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE, r: int = 2):
        super().__init__(dev, r=r)
        self.bypass = False
        self._wn = 0          # writes in current window
        self._wh = 0          # hits (full-path writes only)
        self._wp = 0          # probes while bypassed
        self._ph = 0          # probe hits
        self._low_streak = 0
        self._bypassed_in_w = 0
        self.n_bypassed = 0
        self.n_probes = 0
        self.n_switches = 0
        self.window_log: List[tuple] = []   # (idx, rate, probe_rate, state)

    def _close_window(self):
        if self.bypass:
            # only probe writes carry hit information while bypassed
            rate = self._ph / max(1, self._wp)
        else:
            rate = self._wh / max(1, self._wn)
        self.window_log.append((len(self.window_log), round(rate, 4),
                                round(self._ph / max(1, self._wp), 4),
                                int(self.bypass)))
        if not self.bypass:
            if rate < self.OFF_TH:
                self._low_streak += 1
            else:
                self._low_streak = 0
            if self._low_streak >= 2:
                self.bypass = True
                self.n_switches += 1
        else:
            if rate > self.ON_TH:
                self.bypass = False
                self._low_streak = 0
                self.n_switches += 1
        self._wn = self._wh = self._wp = self._ph = 0
        self._bypassed_in_w = 0

    def write(self, fp: int, L: int) -> ReqOutcome:
        self._wn += 1
        probe = self.bypass and (self.n_w % self.PROBE_EVERY == 0)
        if self.bypass and not probe:
            self.n_bypassed += 1
            self._bypassed_in_w += 1
            out = ReqOutcome(hit=False, data_pages=1, compute_us=0.5)
            self.n_w += 1
            self.logical_pages += 1
            self.data_pages += 1
            self.compute_us_total += 0.5
            if self._wn >= self.WINDOW:
                self._close_window()
            return out
        if probe:
            self.n_probes += 1
            self._wp += 1
        out = super().write(fp, L)
        if out.hit:
            self._wh += 1
            if probe:
                self._ph += 1
        if self._wn >= self.WINDOW:
            self._close_window()
        return out

    def summary(self) -> dict:
        s = super().summary()
        s.update({
            "n_bypassed": self.n_bypassed,
            "n_probes": self.n_probes,
            "n_switches": self.n_switches,
            "bypass_frac": self.n_bypassed / max(1, self.n_w),
            "window_log": self.window_log,
        })
        return s


# ---------------------------------------------------------------------------
# Opt 5: bounded cold fingerprint index
# ---------------------------------------------------------------------------

class LiZoneCold(LiZoneDedup):
    """Small second-level fp -> OID index covering sealed zones that aged
    out of every group's candidate chain.  Consulted only after all r hot
    candidates missed; bounded to a few MB of DRAM (LRU eviction)."""
    name = "LiZoneDedup-Cold"
    ENTRY_BYTES = 12

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE, r: int = 2,
                 cold_bytes: int = 4 << 20):
        super().__init__(dev, r=r)
        self.cold_cap = max(1024, cold_bytes // self.ENTRY_BYTES)
        self.cold: "OrderedDict[int, int]" = OrderedDict()
        self.cold_hits = 0
        self.cold_probes = 0
        self.cold_md_pages = 0
        self.cold_evicted = 0
        self._cold_zones = set()

    def _cold_add(self, zid: int):
        if zid in self._cold_zones:
            return
        z = self.zones.get(zid)
        if z is None or z.state == DEAD:
            return
        self._cold_zones.add(zid)
        for fp in z.fps:
            self.cold[fp] = zid
            self.cold.move_to_end(fp)
        while len(self.cold) > self.cold_cap:
            self.cold.popitem(last=False)
            self.cold_evicted += 1

    def _install_primary(self, g: int) -> LZZone:
        z = super()._install_primary(g)
        # zones beyond the hot candidate depth (primary + lineage[:r]) are
        # no longer probed by the hot path; index them for cold lookup
        for zid in self.lineage[g][self.r:]:
            self._cold_add(zid)
        return z

    def _cold_lookup(self, fp: int, cands: List[int],
                     out: ReqOutcome) -> Optional[LZZone]:
        if not self.cold:
            return None
        self.cold_probes += 1
        zid = self.cold.get(fp)
        if zid is None:
            return None
        z = self.zones.get(zid)
        if z is None or z.state not in (ACTIVE, SEALED):
            self.cold.pop(fp, None)
            return None
        # cold shard read: one 4 KB page (kept DRAM-resident only for the
        # most recent entries via the normal shard cache)
        out.md_read_pages += 1
        self.md_read_pages += 1
        self.cold_md_pages += 1
        out.compute_us += self.dev.c_scan
        out.cache_kind = "cold"
        self.cold_hits += 1
        self.cold.move_to_end(fp)
        return z

    def dram_breakdown(self) -> dict:
        d = super().dram_breakdown()
        cold = len(self.cold) * self.ENTRY_BYTES
        d["cold_index"] = cold
        d["total"] += cold
        return d

    def summary(self) -> dict:
        s = super().summary()
        s.update({
            "cold_hits": self.cold_hits,
            "cold_probes": self.cold_probes,
            "cold_md_pages": self.cold_md_pages,
            "cold_entries": len(self.cold),
            "cold_evicted": self.cold_evicted,
        })
        return s


SYSTEMS["lizone-bypass"] = LiZoneBypass
SYSTEMS["lizone-cold"] = LiZoneCold


# ---------------------------------------------------------------------------
# V5: the paper's LiZoneDedup with opt2-opt5 integrated (opt1 bypass stays
# a conditional knob -- bypassed pages lose future dedup opportunities, so
# it is off by default and enabled only on compute-constrained controllers
# or provably non-duplicating streams)
# ---------------------------------------------------------------------------

class LiZoneDedupV5(LiZoneCold):
    """V5 full system: V4 LiZoneDedup + bounded cold fingerprint index."""
    name = "LiZoneDedup"
    COLD_DEFAULT_BYTES = 4 << 20

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE, r: int = 2,
                 route_mode: str = "lsh", cold_bytes: int = 0):
        super().__init__(dev, r=r, cold_bytes=cold_bytes or
                         self.COLD_DEFAULT_BYTES)
        if route_mode == "prefix":
            self.route_mode = "prefix"


def _v5r(r, route_mode="lsh"):
    class _S(LiZoneDedupV5):
        def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE):
            super().__init__(dev, r=r, route_mode=route_mode)
    return _S


LiZoneV5R1 = _v5r(1)
LiZoneV5R3 = _v5r(3)
LiZoneV5R4 = _v5r(4)
LiZoneV5Prefix = _v5r(2, "prefix")
LiZoneV5R1.name = "LiZoneDedup-r1"
LiZoneV5R3.name = "LiZoneDedup-r3"
LiZoneV5R4.name = "LiZoneDedup-r4"
LiZoneV5Prefix.name = "LiZoneDedup-r2-prefix"

# combo: dedup (V5, incl. cold index) + LZ4 on remaining uniques.
# MRO: LiZoneCold provides the cold-index hooks, LiZoneBalloon provides
# the compress-after-dedup write chain.
from .systems import LiZoneBalloon as _LZB


class LiZoneBalloonV5(LiZoneCold, _LZB):
    name = "LiZoneDedup+Balloon"

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE, r: int = 2):
        # LiZoneCold.__init__ chains to LiZoneBalloon's (phys-bytes setup)
        LiZoneCold.__init__(self, dev, r=r,
                            cold_bytes=LiZoneDedupV5.COLD_DEFAULT_BYTES)


SYSTEMS["v5"] = LiZoneDedupV5
SYSTEMS["v5-r1"] = LiZoneV5R1
SYSTEMS["v5-r3"] = LiZoneV5R3
SYSTEMS["v5-r4"] = LiZoneV5R4
SYSTEMS["v5-prefix"] = LiZoneV5Prefix
SYSTEMS["v5-balloon"] = LiZoneBalloonV5


# ---------------------------------------------------------------------------
# V6 naming wrappers: V5 mechanics (cold index etc.) -- the V6 delta lives
# in DeviceConfig (wi_entries_per_zone=128, sha_hw) and TimingSim flags
# (dref_prefetch, redirect_cap=2048, pipeline_parallel)
# ---------------------------------------------------------------------------

class LiZoneDedupV6(LiZoneDedupV5):
    name = "LiZoneDedup"


def _v6r(r, route_mode="lsh"):
    class _S(LiZoneDedupV6):
        def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE):
            super().__init__(dev, r=r, route_mode=route_mode)
    return _S


LiZoneV6R1 = _v6r(1)
LiZoneV6R3 = _v6r(3)
LiZoneV6R4 = _v6r(4)
LiZoneV6Prefix = _v6r(2, "prefix")
LiZoneV6R1.name = "LiZoneDedup-r1"
LiZoneV6R3.name = "LiZoneDedup-r3"
LiZoneV6R4.name = "LiZoneDedup-r4"
LiZoneV6Prefix.name = "LiZoneDedup-r2-prefix"


class LiZoneBalloonV6(LiZoneCold, _LZB):
    name = "LiZoneDedup+Balloon"

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE, r: int = 2):
        LiZoneCold.__init__(self, dev, r=r,
                            cold_bytes=LiZoneDedupV5.COLD_DEFAULT_BYTES)


SYSTEMS["v6"] = LiZoneDedupV6
SYSTEMS["v6-r1"] = LiZoneV6R1
SYSTEMS["v6-r3"] = LiZoneV6R3
SYSTEMS["v6-r4"] = LiZoneV6R4
SYSTEMS["v6-prefix"] = LiZoneV6Prefix
SYSTEMS["v6-balloon"] = LiZoneBalloonV6


# ---------------------------------------------------------------------------
# V7: V6 + stage-2 shard neighbour prefetch (in-system); the stage-1 filter
# prefetch, zone commit locks and core-count scaling are timing-level
# (TimingSim flags / DeviceConfig.n_cores)
# ---------------------------------------------------------------------------

class LiZoneDedupV7(LiZoneDedupV6):
    name = "LiZoneDedup"
    shard_prefetch_neighbors = 2


class LiZoneBalloonV7(LiZoneCold, _LZB):
    name = "LiZoneDedup+Balloon"
    shard_prefetch_neighbors = 2

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE, r: int = 2):
        LiZoneCold.__init__(self, dev, r=r,
                            cold_bytes=LiZoneDedupV5.COLD_DEFAULT_BYTES)


SYSTEMS["v7"] = LiZoneDedupV7
SYSTEMS["v7-balloon"] = LiZoneBalloonV7


# V9/V10 final-plan aliases: v9 == V7 full system (name per plan)
SYSTEMS["v9"] = LiZoneDedupV7
SYSTEMS["v9-balloon"] = LiZoneBalloonV7
SYSTEMS["v9-r1"] = LiZoneV6R1
SYSTEMS["v9-r3"] = LiZoneV6R3
SYSTEMS["v9-r4"] = LiZoneV6R4
SYSTEMS["v9-prefix"] = LiZoneV6Prefix
