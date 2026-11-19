"""
timing.py -- closed-loop timing model (paper Sec 4.4).

Model: N_CORE controller cores + C_CHAN NAND channels, all modeled as
FCFS resources with gap-filling interval scheduling (a reservation never
blocks a resource during the request's earlier phases).  Requests keep QD
outstanding (closed loop, fio-style saturation).

Write:  core1(hash+route+DRAM probes) -> NAND metadata reads (parallel) ->
        confirm (0.3 us, folded into completion) -> NAND data program.
Read:   core(status+parse) -> [NAND descriptor page] -> NAND source page
        (+LZ4 decompress for Balloon).
HostDedup additionally reserves a host core before the device path.

All times are microseconds.
"""

from __future__ import annotations

import heapq
import math
import random
from bisect import insort
from collections import deque, OrderedDict
from typing import Dict, List, Optional, Tuple

from .device import DeviceConfig, DEFAULT_DEVICE
from .systems import (make_system, ReqOutcome, BalloonZNS, LiZoneBalloon,
                      HostDedup, NoDedup)
from .traces import Trace, fp_crc

PAGE = 4096


class Resource:
    """FCFS resource with gap-filling interval scheduling.

    Scheduling times are NOT guaranteed monotone: different requests take
    different paths (some detour through metadata reads), so a later call
    may carry an earlier timestamp.  The dead-prefix head pointer is
    therefore only advanced over intervals that ended before the minimum
    timestamp of the last RECENT sched calls (a backstep can only come
    from a request still in flight, at most QD of them), so no live
    interval is ever skipped while the list stays compact.
    """

    __slots__ = ("iv", "h", "recent")

    RECENT = 128              # > QD in-flight requests: bounds backsteps

    def __init__(self):
        self.iv: List[Tuple[float, float]] = []
        self.h = 0                      # intervals before h are dead
        self.recent = deque([], self.RECENT)   # recent sched timestamps

    def peek(self, t: float, d: float) -> Tuple[float, float]:
        """Earliest feasible (start, end) without reserving."""
        iv = self.iv
        st = t
        i = self.h
        n = len(iv)
        while i < n:
            s, e = iv[i]
            if e <= t:
                i += 1
                continue
            if st + d <= s:
                break
            if e > st:
                st = e
            i += 1
        return st, st + d

    def sched(self, t: float, d: float) -> Tuple[float, float]:
        st, en = self.peek(t, d)
        iv = self.iv
        # compact the dead prefix: intervals that ended before the oldest
        # of the last RECENT sched timestamps are unreachable by any
        # in-flight backstep (at most QD requests in flight) and can be
        # dropped safely
        self.recent.append(t)
        if len(self.recent) >= self.RECENT:
            floor = self.recent[0]
            n = len(iv)
            while self.h < n and iv[self.h][1] <= floor:
                self.h += 1
            if self.h > 64:
                del iv[:self.h]
                self.h = 0
        insort(iv, (st, en))
        return st, en


class LbaWindow:
    """Rolling window of recent write completions for read sampling."""

    def __init__(self, window: int = 300_000, desc_cache_pages: int = 512,
                 seed: int = 7, skew: bool = False):
        self.ring: List[Tuple[int, bool]] = []
        self.seq = 0
        self.window = window
        self.desc_cache_pages = desc_cache_pages   # 2 MB of recent log pages
        self.rng = random.Random(seed)
        self.skew = skew                # hot-spot read locality (opt 2)
        self.counts: Dict[int, int] = {}            # seq -> reads so far
        self.redirected: Dict[int, bool] = {}       # seq -> pre-copied
        self.hotset: List[int] = []                 # persistently hot seqs
        self._hotset_set: set = set()
        self.hotset_cap = 4096
        self.prefetched: set = set()    # seqs whose DREF record is prefetched
        self.redirect_lru: "OrderedDict[int, None]" = OrderedDict()

    def record(self, is_desc: bool):
        self.seq += 1
        self.ring.append((self.seq, is_desc))
        if self.skew and is_desc and len(self.hotset) < self.hotset_cap \
                and self.rng.random() < 0.02:
            self.hotset.append(self.seq)
            self._hotset_set.add(self.seq)
        if len(self.ring) > self.window:
            cut = len(self.ring) - self.window // 2
            removed = self.ring[:cut]
            del self.ring[:cut]
            lo = removed[0][0]
            for s, _ in removed:
                self.counts.pop(s, None)
                self.redirected.pop(s, None)
            # lazily drop hot entries that aged out of the window
            if self.hotset and self.hotset[0] < lo:
                self.hotset = [s for s in self.hotset if s >= lo]
                self._hotset_set = set(self.hotset)

    def _pick_hot(self) -> int:
        """A persistently hot slot, refreshed from recent writes if stale."""
        for _ in range(4):
            seq = self.hotset[self.rng.randrange(len(self.hotset))]
            if self.ring and seq >= self.ring[0][0]:
                return seq
        # replace with a fresh descriptor-backed slot from the ring tail
        for seq, is_desc in reversed(self.ring[-200:]):
            if is_desc:
                return seq
        return self.ring[-1][0]

    def sample_read(self) -> Tuple[bool, bool, int, int]:
        """(is_descriptor_backed, descriptor_page_cached, reads_so_far, seq)."""
        if not self.ring:
            return False, False, 0, -1
        if self.skew and self.hotset and self.rng.random() < 0.85:
            # Zipf-like hot spot: 85% of reads hit the persistent hot set
            seq = self._pick_hot()
            is_desc = True
        else:
            seq, is_desc = self.ring[self.rng.randrange(len(self.ring))]
        c = self.counts.get(seq, 0) + 1
        self.counts[seq] = c
        cached = (self.seq - seq) < self.desc_cache_pages * 64
        return is_desc, (cached and is_desc), c, seq


class TimingSim:
    def __init__(self, trace: Trace, system_key: str,
                 dev: DeviceConfig = DEFAULT_DEVICE, qd: int = 32,
                 read_mix_extra: float = 0.15,
                 max_events: Optional[int] = None,
                 skew: bool = False, precopy: bool = False,
                 hot_reads: int = 16, additive_reads: bool = False,
                 dref_prefetch: bool = False, redirect_cap: int = 0,
                 pipeline_parallel: bool = False,
                 stage1_prefetch: bool = False, zone_locks: bool = False,
                 bg_reserve_frac: float = 0.0):
        self.tr = trace
        self.key = system_key
        self.dev = dev
        self.qd = qd
        self.read_mix_extra = read_mix_extra
        self.additive_reads = additive_reads   # reads added on top of writes
        self.dref_prefetch = dref_prefetch     # opt: prefetch adjacent DREFs
        self.redirect_cap = redirect_cap       # opt: bounded redirect table
        self.pipeline_parallel = pipeline_parallel  # opt: overlap hash/lookup
        self.stage1_prefetch = stage1_prefetch    # opt: async filter prefetch
        self.zone_locks = zone_locks              # opt: per-zone commit locks
        self.zlocks: Dict[int, Resource] = {}
        self.n_zone_stalls = 0
        self.max_events = max_events if max_events else len(trace)
        self.sys = make_system(system_key, dev)
        self.lba = LbaWindow(skew=skew)
        self.precopy = precopy       # opt 2: hot descriptor pre-copy
        self.hot_reads = hot_reads
        self.n_redirected = 0
        self.n_hot_copied = 0
        self.bg_precopy_pages = 0.0
        self.n_prefetch_hits = 0
        self.n_redirect_evictions = 0
        self.rng = random.Random(11)          # background placement only
        self.rng_read = random.Random(4242)   # read injection (shared across
        #                                      systems so the op mix matches)
        self.cores = [Resource() for _ in range(dev.n_cores)]
        self.engine = Resource()        # dedicated compression engine
        self.engine_us = 0.0
        # NAND plane-level units (matches the FEMU zns model): programs
        # and reads run on ch x lun x plane independent units
        self.nand_units = [Resource() for _ in range(dev.n_nand_units)]
        self.chans = [Resource() for _ in range(dev.n_channels)]
        self.hosts = [Resource() for _ in range(8)]
        # stats
        self.write_lat: List[float] = []
        self.read_lat: List[float] = []
        self.wl_hit: List[float] = []
        self.wl_miss: List[float] = []
        self.rl_data: List[float] = []
        self.rl_desc: List[float] = []
        self.rl_redir: List[float] = []
        self.parts_w: Dict[str, float] = {k: 0.0 for k in
                                          ("host", "hash", "mdread", "confirm",
                                           "prog")}
        self.parts_r: Dict[str, float] = {k: 0.0 for k in
                                          ("status", "dref_read", "page",
                                           "decomp")}
        self.n_reads = 0
        self.n_desc_reads = 0
        self.n_desc_cached = 0
        self.logical_w_bytes = 0
        self.phys_w_bytes = 0.0
        self.logical_r_bytes = 0
        self.phys_r_pages = 0.0
        self.bg_chan_us = 0.0
        self.t_end = 0.0
        # rate-limited background migration (rebuttal Q3): background work
        # is capped at bg_reserve_frac of the aggregate plane bandwidth
        self.bg_reserve_frac = bg_reserve_frac
        self.bg_pages_done = 0.0
        self.bg_budget_us = 0.0
        self._bg_last_t = 0.0
        # closed-loop machinery
        self._doneq: List[Tuple[float, int]] = []   # (completion, tag)
        self._tag = 0
        self._inflight = 0
        self._ti = 0              # trace index pointer
        self._rq = deque()        # pending additive reads

    # ------------- resource helpers (us) -------------
    def _core(self, now: float, dur: float) -> float:
        best, best_e = None, float("inf")
        for r in self.cores:
            _, e = r.peek(now, dur)
            if e < best_e:
                best_e, best = e, r
        best.sched(now, dur)
        return best_e

    def _chan(self, now: float, dur: float) -> float:
        best, best_e = None, float("inf")
        for r in self.chans:
            _, e = r.peek(now, dur)
            if e < best_e:
                best_e, best = e, r
        best.sched(now, dur)
        return best_e

    def _nand(self, now: float, dur: float) -> float:
        """Schedule a NAND op on one plane-level unit (K-sample of the
        least-loaded unit; with QD 32 against 128 units the choice is
        almost always uncontended)."""
        units = self.nand_units
        nu = len(units)
        best, best_e = None, float("inf")
        for k in range(8):
            r = units[self.rng.randrange(nu)]
            _, e = r.peek(now, dur)
            if e < best_e:
                best_e, best = e, r
        best.sched(now, dur)
        return best_e

    def _engine(self, now: float, dur: float) -> float:
        st, en = self.engine.sched(now, dur)
        self.engine_us += dur
        return en

    def _host(self, now: float, dur: float) -> float:
        best, best_e = None, float("inf")
        for r in self.hosts:
            _, e = r.peek(now, dur)
            if e < best_e:
                best_e, best = e, r
        best.sched(now, dur)
        return best_e

    def _bg(self, t: float, pages: float, read: bool):
        if pages <= 0:
            return
        dur = pages * (self.dev.t_read if read else self.dev.t_prog)
        self.bg_chan_us += dur
        r = self.nand_units[self.rng.randrange(self.dev.n_nand_units)]
        r.sched(t, dur)

    # ------------- request scheduling -------------
    def _next_op(self):
        """Returns (kind, fp, L) or None; advances pointer, may inject reads."""
        if self._rq:
            return self._rq.popleft()
        while self._ti < self.max_events:
            op, fp, L = self.tr.event(self._ti)
            self._ti += 1
            if op == 1:
                return 1, fp, L
            if self._ti > 100 and self.rng_read.random() < self.read_mix_extra:
                if self.additive_reads:
                    # issue the extra read after this write, don't drop it
                    self._rq.append((1, fp, L))
                    return 0, fp, L
                return 1, fp, L      # extra read around this write
            return 0, fp, L
        return None

    def _issue(self, t0: float) -> Optional[float]:
        nxt = self._next_op()
        if nxt is None:
            return None
        kind, fp, L = nxt
        dev = self.dev
        self._tag += 1
        tag = self._tag
        t = t0
        if kind == 1:
            # ---------------- read ----------------
            is_desc, cached, rcount, seq = self.lba.sample_read()
            self.n_reads += 1
            if is_desc and self.dref_prefetch and \
                    seq in self.lba.prefetched:
                # adjacent DREF record already prefetched into the cache
                cached = True
                self.n_prefetch_hits += 1
            redirected = False
            if is_desc and self.precopy:
                if rcount > self.hot_reads or \
                        self.lba.redirected.get(seq, False):
                    redirected = True
                elif rcount == self.hot_reads:
                    # threshold crossing: background pre-copy the source
                    # page into the local zone + append redirect record
                    redirected = True
                    self.lba.redirected[seq] = True
                    self.lba.redirect_lru[seq] = None
                    if self.redirect_cap and \
                            len(self.lba.redirect_lru) > self.redirect_cap:
                        old, _ = self.lba.redirect_lru.popitem(last=False)
                        # bounded table: the oldest entry drops back to the
                        # DREF indirection path
                        self.lba.redirected[old] = False
                        self.n_redirect_evictions += 1
                    self.n_hot_copied += 1
                    self.bg_precopy_pages += 1.02
                    self._bg(t, 1, read=True)
                    self._bg(t, 1.02, read=False)
            if is_desc and not redirected:
                self.n_desc_reads += 1
                if cached:
                    self.n_desc_cached += 1
            if redirected:
                self.n_redirected += 1
            comp = dev.c_status + (dev.c_dparse
                                   if (is_desc and not redirected) else 0.0)
            e1 = self._core(t, comp)
            self.parts_r["status"] += e1 - t
            t = e1
            if isinstance(self.sys, HostDedup):
                # host-side dedup: the host index (DRAM) resolves the LBA to
                # the original copy; the device serves one page read.
                e1b = self._host(t, 0.3)
                t = e1b
            elif is_desc and not cached and not redirected:
                e2 = self._nand(t, dev.t_read)
                self.parts_r["dref_read"] += e2 - t
                t = e2
                self.phys_r_pages += 1
                if self.dref_prefetch:
                    # descriptor-backed read pattern detected: background-
                    # prefetch the whole DREF log page (256 records x 16 B)
                    # covering this slot's neighborhood
                    self._bg(t, 1, read=True)
                    pf = self.lba.prefetched
                    for s2 in range(seq - 128, seq + 129):
                        pf.add(s2)
                    if len(pf) > 262144:
                        pf.clear()
            e3 = self._nand(t, dev.t_read)
            self.parts_r["page"] += e3 - t
            t = e3
            self.phys_r_pages += 1
            if isinstance(self.sys, BalloonZNS):
                if dev.lz4_hw:
                    t = max(t, self._engine(t, dev.c_lz4_d))
                else:
                    e4 = self._core(t, dev.c_lz4_d)
                    self.parts_r["decomp"] += e4 - t
                    t = e4
            self.read_lat.append(t - t0)
            self.logical_r_bytes += PAGE
            if redirected:
                self.rl_redir.append(t - t0)
            else:
                (self.rl_desc if is_desc else self.rl_data).append(t - t0)
            return t

        # ---------------- write ----------------
        out = self.sys.write(fp, L)
        if isinstance(self.sys, HostDedup):
            eh = self._host(t, out.compute_us)
            self.parts_w["host"] += eh - t
            t = eh
        comp1 = out.compute_us
        eng_end = t
        if isinstance(self.sys, (BalloonZNS,)) and self.dev.lz4_hw:
            # Balloon-ZNS (DAC'24): compression on a dedicated hardware
            # engine, "a few us per page, not a bottleneck" -- the engine
            # arm runs in parallel with the core; only submission remains
            # on the core
            comp1 = 0.5
            eng_end = self._engine(t, self.dev.c_lz4_c)
        if self.stage1_prefetch and out.md_read_pages > 0:
            # two-stage pipeline: SimHash (route digest) finishes early;
            # candidate-filter page reads are issued at that point and
            # overlap the remaining SHA-256 computation
            t_sim = t + dev.c_simhash
            e1 = self._core(t, comp1)
            self.parts_w["hash"] += e1 - t
            n = max(1, int(math.ceil(out.md_read_pages)))
            ends = [self._chan(max(t, t_sim), dev.t_read) for _ in range(n)]
            e2 = max(ends)
            self.parts_w["mdread"] += max(0.0, e2 - t)
            t = max(e1, e2, eng_end)
        elif self.pipeline_parallel and out.md_read_pages > 0:
            # async pipeline: the SHA-256 (core) overlaps the route-table +
            # filter probes and any metadata page reads (channels); the
            # request advances when both arms complete
            e1 = self._core(t, comp1)
            self.parts_w["hash"] += e1 - t
            n = max(1, int(math.ceil(out.md_read_pages)))
            ends = [self._nand(t, dev.t_read) for _ in range(n)]
            e2 = max(ends)
            self.parts_w["mdread"] += max(0.0, e2 - t)
            t = max(e1, e2, eng_end)
        else:
            e1 = self._core(t, comp1)
            self.parts_w["hash"] += e1 - t
            t = max(e1, eng_end)
            if out.md_read_pages > 0:
                n = max(1, int(math.ceil(out.md_read_pages)))
                ends = [self._nand(t, dev.t_read) for _ in range(n)]
                e2 = max(ends)
                self.parts_w["mdread"] += e2 - t
                t = e2
        # confirm/decision runs on the completion path (0.3 us, folded into
        # the completion time; it never blocks a core for its waiting span)
        t += 0.3
        self.parts_w["confirm"] += 0.3
        if self.zone_locks:
            # per-zone commit lock: DREF append / data program issue is
            # serialized within a zone, parallel across zones (spinlock +
            # write-pointer update held ~0.2 us)
            zid = getattr(self.sys, "_last_dst", -1)
            zres = self.zlocks.get(zid)
            if zres is None:
                zres = Resource()
                self.zlocks[zid] = zres
            ls, le = zres.sched(t, 0.2)
            if ls > t + 1e-9:
                self.n_zone_stalls += 1
            t = le
        if out.hit:
            self._bg(t, out.md_write_bytes / PAGE, read=False)
            self._bg(t, out.md_rmw_pages, read=True)
        else:
            e4 = self._nand(t, out.data_pages * dev.t_prog)
            self.parts_w["prog"] += e4 - t
            t = e4
            self._bg(t, out.md_write_bytes / PAGE + out.md_rmw_pages,
                     read=False)
            self._bg(t, out.md_rmw_pages, read=True)
        self.write_lat.append(t - t0)
        (self.wl_hit if out.hit else self.wl_miss).append(t - t0)
        self.logical_w_bytes += PAGE
        self.phys_w_bytes += out.data_pages * PAGE
        self.lba.record(out.hit)
        return t

    # ------------- main loop -------------
    def _bg_migrate_step(self, t: float):
        """Consume the reserved background-migration bandwidth accumulated
        since the last call (rebuttal Q3): the controller allots the
        migration engine bg_reserve_frac of the aggregate plane bandwidth;
        migration pages (one read + one program each) are scheduled on
        random plane units, stealing exactly that share from the
        foreground, with no idle window required."""
        if self.bg_reserve_frac <= 0:
            return
        dev = self.dev
        dt = max(0.0, t - self._bg_last_t)
        self._bg_last_t = t
        self.bg_budget_us += (dt * dev.n_nand_units * self.bg_reserve_frac)
        per_page = dev.t_read + dev.t_prog
        # cap the backlog: at most 4 pages per loop iteration keeps the
        # interference fine-grained without scheduling unbounded bursts
        for _ in range(4):
            if self.bg_budget_us < per_page:
                break
            self.bg_budget_us -= per_page
            self.bg_pages_done += 1
            self.bg_chan_us += per_page
            self.nand_units[self.rng.randrange(dev.n_nand_units)].sched(
                t, dev.t_read)
            self.nand_units[self.rng.randrange(dev.n_nand_units)].sched(
                t, dev.t_prog)

    def run(self) -> dict:
        # bootstrap: issue QD requests at t=0, then closed loop
        completions: List[Tuple[float, int]] = []
        cnt = 0
        while cnt < self.qd:
            c = self._issue(0.0)
            if c is None:
                break
            completions.append((c, cnt))
            cnt += 1
        heapq.heapify(completions)
        while completions:
            t, _ = heapq.heappop(completions)
            self.t_end = max(self.t_end, t)
            self._bg_migrate_step(t)
            c = self._issue(t)
            if c is not None:
                heapq.heappush(completions, (c, self._tag))
        return self.results()

    # ------------- results -------------
    @staticmethod
    def _pct(xs, p):
        if not xs:
            return float("nan")
        xs = sorted(xs)
        k = min(len(xs) - 1, max(0, int(math.ceil(p / 100.0 * len(xs))) - 1))
        return xs[k]

    def results(self) -> dict:
        T = max(self.t_end, 1e-9)
        n = max(1, len(self.write_lat))
        nr = max(1, len(self.read_lat))
        return {
            "system": self.key,
            "trace": self.tr.name,
            "qd": self.qd,
            "writes": len(self.write_lat),
            "reads": len(self.read_lat),
            "sim_time_s": T / 1e6,
            "write_logi_bw_mbs": self.logical_w_bytes / T,
            "write_phys_bw_mbs": self.phys_w_bytes / T,
            "read_logi_bw_mbs": self.logical_r_bytes / T,
            "read_phys_bw_mbs": self.phys_r_pages * PAGE / T,
            "w_p50": self._pct(self.write_lat, 50),
            "w_p95": self._pct(self.write_lat, 95),
            "w_p99": self._pct(self.write_lat, 99),
            "r_p50": self._pct(self.read_lat, 50),
            "r_p95": self._pct(self.read_lat, 95),
            "r_p99": self._pct(self.read_lat, 99),
            "hit_lat_mean": (sum(self.wl_hit) / len(self.wl_hit)
                             if self.wl_hit else float("nan")),
            "miss_lat_mean": (sum(self.wl_miss) / len(self.wl_miss)
                              if self.wl_miss else float("nan")),
            "data_read_lat_mean": (sum(self.rl_data) / len(self.rl_data)
                                   if self.rl_data else float("nan")),
            "desc_read_lat_mean": (sum(self.rl_desc) / len(self.rl_desc)
                                   if self.rl_desc else float("nan")),
            "desc_read_frac": self.n_desc_reads / max(1, self.n_reads),
            "desc_cached_frac": self.n_desc_cached / max(1, self.n_desc_reads),
            "redirect_frac": self.n_redirected / max(1, self.n_reads),
            "hot_copied": self.n_hot_copied,
            "bg_precopy_pages": self.bg_precopy_pages,
            "prefetch_hits": self.n_prefetch_hits,
            "redirect_evictions": self.n_redirect_evictions,
            "zone_lock_stalls": self.n_zone_stalls,
            "engine_util": self.engine_us / (T * 1),
            "shard_prefetch_bg_pages": getattr(self.sys, "prefetch_bg_pages", 0),
            "read_bw_mbs": self.logical_r_bytes / T,
            "read_phys_bw_mbs": self.phys_r_pages * PAGE / T,
            "redir_read_lat_mean": (sum(self.rl_redir) / len(self.rl_redir)
                                    if self.rl_redir else float("nan")),
            "parts_w_us": {k: v / n for k, v in self.parts_w.items()},
            "parts_r_us": {k: v / nr for k, v in self.parts_r.items()},
            "bg_chan_util": self.bg_chan_us / (T * self.dev.n_channels),
            "bg_reserve_frac": self.bg_reserve_frac,
            "bg_mig_pages": self.bg_pages_done,
            "func": self.sys.summary(),
        }
