"""
systems.py -- all evaluated systems for the LiZoneDedup paper (Sec 4).

Systems
-------
  NoDedup           : plain ZNS, every page programmed.
  GlobalOracle      : device-wide exact dedup, unlimited index (upper bound).
  HostDedup         : host-side global dedup (same physical writes as Oracle,
                      cost paid in host DRAM/CPU).
  GlobalIndex       : traditional in-storage global fingerprint index
                      (ablation layer 1).
  DataPathOnly      : C1+C2 without C3 (naive per-zone hash table, no shard,
                      no split state, no cache) (layer 2).
  PlusIdentityShard : + page-aligned identity shards & filters (layer 3).
  PlusSplitState    : + identity/reference split state (layer 4).
  LiZoneDedup       : full design incl. bounded ref accumulator + caches (5).
  BalloonZNS        : slot-aligned LZ4 compression baseline (DAC'24).
  LiZoneBalloon     : dedup then compress remaining unique pages.

Every write() returns a ReqOutcome (latency recipe + I/O counts) so the
functional drivers and the closed-loop timing model share one code path.
"""

from __future__ import annotations

import math
import random
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional

from .device import DeviceConfig, DEFAULT_DEVICE, bloom_bits, xor_filter_bytes
from .traces import fp_crc

PAGE = 4096

ACTIVE, SEALED, RESET_PENDING, DEAD = 0, 1, 2, 3


# ===========================================================================
# Zone machinery for LiZoneDedup-family systems
# ===========================================================================

class LZZone:
    __slots__ = ("zid", "gen", "state", "slots", "data_pages", "fps", "wi",
                 "delta", "delta_bytes", "bloom_n", "incoming_refs",
                 "outgoing_descs", "desc_slots", "alive_descs", "dead_slots",
                 "created_at", "sealed_at", "reset_pending_at")

    def __init__(self, zid: int, gen: int = 0):
        self.zid = zid
        self.gen = gen
        self.state = ACTIVE
        self.slots = 0                 # logical write pointer (page slots)
        self.data_pages = 0            # physical data pages programmed
        self.fps = set()               # identity set (ground truth)
        self.wi = OrderedDict()        # working index LRU (recent fps)
        self.delta = set()             # fingerprints living in the delta log
        self.delta_bytes = 0
        self.bloom_n = 0
        self.incoming_refs = 0         # live cross-zone descriptors pointing in
        self.outgoing_descs = 0
        self.desc_slots = 0            # descriptor-backed logical slots
        self.alive_descs = 0
        self.dead_slots = 0
        self.created_at = 0
        self.sealed_at = -1
        self.reset_pending_at = -1

    @property
    def appendable(self) -> bool:
        return self.state == ACTIVE

    def wi_add(self, fp: int, cap: int):
        wi = self.wi
        wi[fp] = True
        if len(wi) > cap:
            wi.popitem(last=False)


# ===========================================================================
# Outcome record
# ===========================================================================

@dataclass
class ReqOutcome:
    hit: bool = False
    md_read_pages: float = 0.0     # NAND metadata pages read
    md_write_bytes: float = 0.0    # NAND metadata bytes appended (buffered)
    md_rmw_pages: float = 0.0      # in-place metadata page rewrites
    data_pages: float = 1.0        # data pages programmed
    compute_us: float = 0.0        # serialized foreground compute
    probes: int = 0                # membership filter probes
    cache_kind: str = ""           # wi | shard_cache | filter_flash | none
    sealed_source: bool = False
    cross_zone: bool = False


class BaseSystem:
    name = "base"
    needs_hash = False
    host_hash = False

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE):
        self.dev = dev
        self.rng = random.Random(0xC0FFEE)
        self.n_w = 0
        self.n_hits = 0
        self.logical_pages = 0
        self.data_pages = 0.0
        self.md_read_pages = 0.0
        self.md_write_bytes = 0.0
        self.md_rmw_pages = 0.0
        self.compute_us_total = 0.0
        self.lz4_calls = 0

    def write(self, fp: int, L: int) -> ReqOutcome:
        raise NotImplementedError

    def _md_pages_written(self) -> float:
        return math.ceil(self.md_write_bytes / PAGE) + self.md_rmw_pages

    def physical_data_bytes(self) -> float:
        return self.data_pages * PAGE

    def summary(self) -> dict:
        n = max(1, self.n_w)
        uniq = self.n_w - self.n_hits
        return {
            "system": self.name,
            "writes": self.n_w,
            "hits": self.n_hits,
            "hit_rate": self.n_hits / n,
            "dedup_ratio": self.n_w / max(1, uniq),
            "physical_write_reduction": self.n_hits / n,
            "data_pages": self.data_pages,
            "md_read_bytes_per_req": self.md_read_pages * PAGE / n,
            "md_write_bytes_per_data_byte": (
                (self._md_pages_written() * PAGE) /
                max(1.0, self.logical_pages * PAGE)),
            "total_write_amp": (
                (self.data_pages * PAGE + self._md_pages_written() * PAGE) /
                max(1.0, self.logical_pages * PAGE)),
            "compute_us_per_req": self.compute_us_total / n,
        }


# ===========================================================================
# Simple systems
# ===========================================================================

class NoDedup(BaseSystem):
    name = "NoDedup-ZNS"

    def write(self, fp, L):
        self.n_w += 1
        self.logical_pages += 1
        self.data_pages += 1
        self.compute_us_total += 0.5
        return ReqOutcome(hit=False, data_pages=1, compute_us=0.5)


class GlobalOracle(BaseSystem):
    name = "PageFTLDedup"

    def __init__(self, dev=DEFAULT_DEVICE):
        super().__init__(dev)
        self.index = set()

    def write(self, fp, L):
        self.n_w += 1
        self.logical_pages += 1
        c = self.dev.c_sha + self.dev.c_simhash
        if fp in self.index:
            self.n_hits += 1
            self.compute_us_total += c
            return ReqOutcome(hit=True, data_pages=0, compute_us=c)
        self.index.add(fp)
        self.data_pages += 1
        self.compute_us_total += c
        return ReqOutcome(hit=False, data_pages=1, compute_us=c)


class HostDedup(BaseSystem):
    """Host-side global dedup with a 1 GB DRAM index cap; overflow entries
    spill to NAND pages (each spill probe costs one 4 KB NAND read)."""
    name = "HostDedup-1GB"
    host_hash = True
    HOST_INDEX_BYTES = 1 << 30
    ENTRY_BYTES = 20

    def __init__(self, dev=DEFAULT_DEVICE):
        super().__init__(dev)
        self.index = set()
        self.host_compute_us = 0.0
        self.host_spill_reads = 0

    def write(self, fp, L):
        self.n_w += 1
        self.logical_pages += 1
        host_c = self.dev.c_sha + 0.3      # SHA-256 + RAM index lookup
        # index overflow: entries beyond the 1 GB cap spill to NAND
        overflow = len(self.index) * self.ENTRY_BYTES > self.HOST_INDEX_BYTES
        if overflow:
            host_c += self.dev.t_read      # spill probe = 1 NAND page read
            self.host_spill_reads += 1
        self.host_compute_us += host_c
        if fp in self.index:
            self.n_hits += 1
            self.compute_us_total += host_c
            return ReqOutcome(hit=True, data_pages=0, compute_us=host_c)
        self.index.add(fp)
        self.data_pages += 1
        self.compute_us_total += host_c
        return ReqOutcome(hit=False, data_pages=1, compute_us=host_c)

    def host_dram_bytes_1tb(self) -> int:
        pages = (1024 ** 4) // PAGE           # 1 TB / 4 KB = 268 M
        return int(pages * 20 * 1.3)          # 12B fp + 8B loc + overhead

    def summary(self) -> dict:
        s = super().summary()
        s["host_spill_reads"] = self.host_spill_reads
        s["host_index_mb"] = len(self.index) * self.ENTRY_BYTES / 1048576
        s["host_budget_mb"] = self.HOST_INDEX_BYTES / 1048576
        return s


def _hostdedup(budget_bytes: int, label: str):
    class _H(HostDedup):
        name = label
        HOST_INDEX_BYTES = budget_bytes
    return _H


HostDedup512M = _hostdedup(1 << 29, "HostDedup-512MB")
HostDedup2G = _hostdedup(1 << 31, "HostDedup-2GB")
HostDedup8M = _hostdedup(1 << 23, "HostDedup-8MB")   # forced-overflow probe
HostDedup4G = _hostdedup(1 << 32, "HostDedup-4GB")


class GlobalIndex(BaseSystem):
    """Layer 1: device-wide fingerprint hash table on flash (classic
    in-storage dedup, iDFS/DIIT-style).  Lookup: root/bucket page read
    (index >> DRAM so effectively uncached); insert: bucket RMW."""
    name = "GlobalIndex"
    needs_hash = True

    def __init__(self, dev=DEFAULT_DEVICE):
        super().__init__(dev)
        self.index = set()

    def _cache_miss_frac(self) -> float:
        """Expected flash-read fraction for a uniform-random lookup when
        the DRAM cache is capped (0 cap = legacy uncached behaviour)."""
        dev = self.dev
        if dev.dram_cache_bytes <= 0:
            return 1.0
        idx = len(self.index) * dev.index_entry_bytes
        if idx <= dev.dram_cache_bytes:
            return 0.0
        return 1.0 - dev.dram_cache_bytes / idx

    def write(self, fp, L):
        self.n_w += 1
        self.logical_pages += 1
        dev = self.dev
        base = dev.c_sha + dev.c_simhash
        mf = self._cache_miss_frac()
        if fp in self.index:
            self.n_hits += 1
            reads = 1.3 * mf
            self.md_read_pages += reads
            comp = base + 1.3 * mf * dev.c_scan
            self.compute_us_total += comp
            return ReqOutcome(hit=True, md_read_pages=reads, data_pages=0,
                              compute_us=comp)
        self.index.add(fp)
        self.data_pages += 1
        reads = 1.3 * mf + 1.0
        self.md_read_pages += reads
        self.md_rmw_pages += 1
        comp = base + (1.3 * mf + 1.0) * dev.c_scan
        self.compute_us_total += comp
        return ReqOutcome(hit=False, md_read_pages=reads, data_pages=1,
                              compute_us=comp, md_rmw_pages=1)

    def summary(self):
        s = super().summary()
        dev = self.dev
        s["index_bytes_mb"] = (len(self.index) *
                               dev.index_entry_bytes) / 2**20
        s["dram_working_mb"] = min(
            s["index_bytes_mb"], dev.dram_cache_mb or s["index_bytes_mb"])
        return s


# ===========================================================================
# LiZoneDedup family (ablation layers 2..5)
# ===========================================================================

class LiZoneBase(BaseSystem):
    """C1 content routing + C2 dup_append machinery shared by layers 2-5.

    Route model (paper Sec 3.3): the top 16 bits of the 24-bit SimHash
    digest form a route bucket; buckets are covered by route groups
    (Hamming-adjacent buckets share a group).  Each group's route-table
    entry names a primary zone and keeps a lineage of retired primaries.
    The candidate chain is [primary, lineage (recent first), primaries of
    the 5 Hamming-adjacent groups], truncated to r alive zones.  A miss is
    appended to the first appendable candidate; when a primary fills it is
    sealed and a fresh zone becomes the group primary.
    """
    name = "LiZone"
    needs_hash = True
    use_shard = False
    split_state = False
    use_cache = False
    use_wi = False            # +WorkingIndex layer (L1 only, no L2/L3)
    route_mode = "lsh"        # "lsh" | "prefix"
    shard_prefetch_neighbors = 0   # opt: prefetch prefix-adjacent shards

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE, r: int = 2,
                 group_bits: int = 5, track_locus: bool = False,
                 route_mode: str = "lsh"):
        super().__init__(dev)
        self.r = r
        self.group_bits = group_bits
        self.route_mode = route_mode
        self.n_groups = 1 << group_bits
        self.primary = [-1] * self.n_groups
        self.lineage: List[List[int]] = [[] for _ in range(self.n_groups)]
        self.zones: Dict[int, LZZone] = {}
        self.next_zid = 0
        self.n_active = 0
        self.track_locus = track_locus
        self._locus: Dict[int, int] = {} if track_locus else None
        # caches
        self.shard_cache: Optional[OrderedDict] = None
        self.filter_cache: Optional[OrderedDict] = None
        self.cache_stats = {"wi_hit": 0, "shard_hit": 0, "shard_miss": 0,
                            "filter_hit": 0, "filter_miss": 0}
        if self.use_cache:
            self.shard_cap = dev.shard_cache_bytes // PAGE
            self.filter_cap = max(8, dev.filter_cache_bytes //
                                  max(1, xor_filter_bytes(20000)))
            self.shard_cache = OrderedDict()
            self.filter_cache = OrderedDict()
        # stats
        self.probe_total = 0
        self.prefetch_bg_pages = 0
        self.seal_count = 0
        self.seal_md_pages = 0
        self.hit_source_sealed = 0
        self.hit_source_active = 0
        self.cross_zone_descs = 0
        self.incoming_peak = 0
        self.routing_fn = 0
        self.fn_sealed_src = 0     # missed dups whose source zone is sealed
        self.fn_active_src = 0
        self.fn_same_group = 0
        self.minor_merge_pages = 0
        self._truth = set()
        self._last_dst = -1          # zid receiving the last write's slot
        self._last_src = -1          # zid holding the last hit's source

    # ---------------- zone management ----------------
    def _new_zone(self) -> LZZone:
        z = LZZone(self.next_zid)
        z.created_at = self.n_w
        self.next_zid += 1
        self.zones[z.zid] = z
        self.n_active += 1
        return z

    def _install_primary(self, g: int) -> LZZone:
        old = self.primary[g]
        if old >= 0:
            z = self.zones[old]
            if z.state == ACTIVE:
                self._seal(z)
            self.lineage[g].insert(0, old)
            dropped = self.lineage[g][8:]
            del self.lineage[g][8:]
            if dropped:
                self._on_lineage_drop(dropped)
        z = self._new_zone()
        self.primary[g] = z.zid
        return z

    def _seal(self, z: LZZone):
        z.state = SEALED
        z.sealed_at = self.n_w
        self.n_active -= 1
        self.seal_count += 1
        dev = self.dev
        n_keys = len(z.fps)
        idx_pages = math.ceil(n_keys * dev.index_entry_bytes / PAGE)
        flt_pages = (math.ceil(xor_filter_bytes(n_keys, dev.xor_bits_per_key)
                               / PAGE) if self.use_shard else 0)
        self.seal_md_pages += idx_pages + flt_pages
        self.md_write_bytes += (idx_pages + flt_pages) * PAGE
        # delta folded into shards at seal
        z.delta = set()
        z.delta_bytes = 0
        z.wi = OrderedDict()
        if self.filter_cache is not None:
            self.filter_cache.pop(z.zid, None)

    # ---------------- routing ----------------
    def _group_of(self, L: int, fp: int = 0) -> int:
        if self.route_mode == "prefix":
            b = (fp >> 32) & 0xFFFF    # uniform hash region (type bits
                                       # live in the top byte)
        else:
            b = L >> (self.dev.lsh_bits - self.dev.route_table_bits)
        return b >> (self.dev.route_table_bits - self.group_bits)

    def _candidates(self, g: int) -> List[int]:
        out = []
        p = self.primary[g]
        if p >= 0:
            out.append(p)
        out.extend(self.lineage[g][:4])
        for k in range(self.group_bits):
            p2 = self.primary[g ^ (1 << k)]
            if p2 >= 0:
                out.append(p2)
        seen = set()
        res = []
        for z in out:
            if z not in seen:
                seen.add(z)
                res.append(z)
        return res

    def _alloc_zone(self, g: int) -> LZZone:
        p = self.primary[g]
        if p >= 0:
            z = self.zones[p]
            if z.appendable and z.slots < self.dev.pages_per_zone:
                return z
        return self._install_primary(g)

    # ---------------- lookup ----------------
    def _probe_zone(self, z: LZZone, fp: int, out: ReqOutcome) -> bool:
        dev = self.dev
        c = dev.c_filter
        if (self.use_cache or self.use_wi) and z.state == ACTIVE:
            wi = z.wi
            if fp in wi:
                wi.move_to_end(fp)
                self.cache_stats["wi_hit"] += 1
                out.compute_us += c
                out.cache_kind = "wi"
                return True
        if z.state == ACTIVE:
            out.compute_us += c
            out.probes += 1
            self.probe_total += 1
            if fp not in z.fps:
                if self.rng.random() >= dev.bloom_fp_rate:
                    return False
        else:
            # sealed zone: Xor filter.  Layers 3/4 pin the filters of the
            # candidate-chain zones in DRAM (bounded by lineage depth, ~6 MB
            # device-wide, accounted in dram_breakdown); layer 5 replaces
            # this with the bounded filter cache.
            if self.filter_cache is not None:
                if z.zid in self.filter_cache:
                    self.filter_cache.move_to_end(z.zid)
                    self.cache_stats["filter_hit"] += 1
                else:
                    self.cache_stats["filter_miss"] += 1
                    flt_pages = max(1, math.ceil(xor_filter_bytes(
                        len(z.fps), dev.xor_bits_per_key) / PAGE))
                    out.md_read_pages += flt_pages
                    self.md_read_pages += flt_pages
                    self.filter_cache[z.zid] = True
                    if len(self.filter_cache) > self.filter_cap:
                        self.filter_cache.popitem(last=False)
            out.compute_us += c
            out.probes += 1
            self.probe_total += 1
            if fp not in z.fps:
                if self.rng.random() >= dev.xor_fp_rate:
                    return False
        # filter passed -> exact confirmation
        if z.state == ACTIVE and fp in z.delta:
            out.compute_us += dev.c_scan
            return True
        if self.shard_cache is not None:
            key = (z.zid, fp & 0x7F)
            if key in self.shard_cache:
                self.shard_cache.move_to_end(key)
                self.cache_stats["shard_hit"] += 1
                out.cache_kind = "shard_cache"
                out.compute_us += dev.c_scan
                return fp in z.fps
            self.cache_stats["shard_miss"] += 1
            out.md_read_pages += 1
            self.md_read_pages += 1
            self.shard_cache[key] = True
            if self.shard_prefetch_neighbors:
                # stage-2 prefetch: prefix-adjacent shards of the same zone
                # are pulled into the bounded LRU in the background
                p = fp & 0x7F
                for d in range(1, self.shard_prefetch_neighbors + 1):
                    for kk in ((z.zid, p + d), (z.zid, p - d)):
                        if 0 <= kk[1] <= 0x7F:
                            self.shard_cache[kk] = True
                            self.prefetch_bg_pages += 1
            if len(self.shard_cache) > self.shard_cap:
                self.shard_cache.popitem(last=False)
            out.compute_us += dev.c_scan
            return fp in z.fps
        out.md_read_pages += 1
        self.md_read_pages += 1
        out.compute_us += dev.c_scan
        return fp in z.fps

    # ---------------- write path ----------------
    def write(self, fp: int, L: int) -> ReqOutcome:
        dev = self.dev
        self.n_w += 1
        self.logical_pages += 1
        out = ReqOutcome()
        out.compute_us += dev.c_sha + dev.c_simhash + dev.c_route

        g = self._group_of(L, fp)
        cands = self._candidates(g)
        src: Optional[LZZone] = None
        probed = 0
        for zid in cands:
            if probed >= self.r:
                break
            z = self.zones[zid]
            if z.state not in (ACTIVE, SEALED):
                continue
            probed += 1
            if self._probe_zone(z, fp, out):
                src = z
                break
        if src is None:
            # optional second-level lookup (e.g. cold fingerprint index),
            # consulted only after all r hot candidates missed
            src = self._cold_lookup(fp, cands, out)

        if src is not None:
            self.n_hits += 1
            dst = self._alloc_zone(g)
            self._last_dst = dst.zid
            self._last_src = src.zid
            dst.slots += 1
            out.data_pages = 0
            dst.desc_slots += 1
            dst.alive_descs += 1
            out.hit = True
            out.sealed_source = (src.state == SEALED)
            out.cross_zone = (src.zid != dst.zid)
            if out.sealed_source:
                self.hit_source_sealed += 1
            else:
                self.hit_source_active += 1
            if out.cross_zone:
                self.cross_zone_descs += 1
                src.incoming_refs += 1
                self.incoming_peak = max(self.incoming_peak, src.incoming_refs)
            self.md_write_bytes += dev.dref_bytes
            out.md_write_bytes = dev.dref_bytes
            out.compute_us += dev.c_dref
            if self.use_shard and not self.split_state and out.sealed_source:
                # layer 3: no split state -> in-place rewrite of the sealed
                # zone's identity page carrying the reference hint
                out.md_read_pages += 1
                out.md_rmw_pages += 1
                self.md_read_pages += 1
                self.md_rmw_pages += 1
                out.compute_us += dev.c_scan
            self.compute_us_total += out.compute_us
            if dst.slots >= dev.pages_per_zone:
                self._install_primary(g)
                self._gc_lineage()
            return out

        # -------- miss --------
        if fp in self._truth:
            self.routing_fn += 1
            if self.track_locus and fp in self._locus:
                sz = self.zones.get(self._locus[fp])
                if sz is not None:
                    if sz.state == SEALED:
                        self.fn_sealed_src += 1
                    elif sz.state == ACTIVE:
                        self.fn_active_src += 1
        self._truth.add(fp)
        dst = self._alloc_zone(g)
        self._last_dst = dst.zid
        self._last_src = dst.zid
        dst.slots += 1
        dst.data_pages += 1
        dst.fps.add(fp)
        dst.bloom_n += 1
        self.data_pages += 1
        if self.track_locus:
            self._locus[fp] = dst.zid
        out.data_pages = 1
        self._on_miss_insert(dst, fp, out)
        self.compute_us_total += out.compute_us
        if dst.slots >= dev.pages_per_zone:
            self._install_primary(g)
            self._gc_lineage()
        return out

    def _on_miss_insert(self, dst: LZZone, fp: int, out: ReqOutcome):
        """Layer-specific identity-insert metadata accounting."""
        dev = self.dev
        if self.use_shard:
            # INS identity record appended to the DRAM delta log, flushed
            # to page-aligned shards by background minor merges.
            dst.delta.add(fp)
            dst.delta_bytes += dev.ins_bytes
            self.md_write_bytes += dev.ins_bytes
            out.md_write_bytes += dev.ins_bytes
            if self.use_cache or self.use_wi:
                dst.wi_add(fp, dev.wi_entries_per_zone)
            if dst.delta_bytes > dev.delta_cap_bytes_per_zone:
                # minor merge: fold delta into page-aligned shards
                pages = math.ceil(dst.delta_bytes / PAGE)
                self.md_write_bytes += pages * PAGE
                self.minor_merge_pages += pages
                dst.delta = set()
                dst.delta_bytes = 0
        # (layer 2 override adds the naive bucket RMW instead)

    def _gc_lineage(self):
        for g in range(self.n_groups):
            lin = self.lineage[g]
            if lin:
                keep = [z for z in lin
                        if self.zones[z].state != DEAD][:6]
                dropped = [z for z in lin if z not in keep]
                self.lineage[g] = keep
                if dropped:
                    self._on_lineage_drop(dropped)

    # ---------------- optional optimization hooks ----------------
    def _cold_lookup(self, fp: int, cands: List[int],
                     out: ReqOutcome) -> Optional[LZZone]:
        """Second-level lookup after all hot candidates missed (opt 5)."""
        return None

    def _on_lineage_drop(self, zids: List[int]):
        """Zones that slid out of every group's candidate chain (opt 5)."""
        pass

    # ---------------- DRAM accounting ----------------
    def dram_breakdown(self) -> dict:
        dev = self.dev
        route = (1 << dev.route_table_bits) * dev.route_entry_bytes
        active_filters = 0
        wi = 0
        delta = 0
        n_act = 0
        naive_idx = 0
        pinned = 0
        for z in self.zones.values():
            if z.state == ACTIVE:
                n_act += 1
                if self.use_shard:
                    active_filters += bloom_bits(z.bloom_n,
                                                 dev.bloom_fp_rate) // 8
                    delta += z.delta_bytes
                else:
                    naive_idx += len(z.fps) * dev.index_entry_bytes
            elif z.state == SEALED and self.use_shard and not self.use_cache:
                # layers 3/4 pin candidate-chain zone filters in DRAM
                pinned += xor_filter_bytes(len(z.fps), dev.xor_bits_per_key)
        wi_bytes = sum(len(z.wi) for z in self.zones.values()
                       if z.state == ACTIVE) * 16
        caches = 0
        if self.use_cache:
            caches = (len(self.shard_cache) * PAGE +
                      len(self.filter_cache) * xor_filter_bytes(20000))
        total = (route + active_filters + pinned + naive_idx + delta +
                 caches + self.next_zid * 4 + 256 * 1024 + wi_bytes)
        return {"route_table": route,
                "active_zone_filters": active_filters,
                "pinned_chain_filters": pinned,
                "working_index": wi_bytes,
                "delta_logs": delta,
                "naive_index_dram": naive_idx,
                "caches": caches,
                "incoming_ref_counts": self.next_zid * 4,
                "ref_accumulator": 256 * 1024,
                "n_active_zones": n_act,
                "total": total}

    def summary(self) -> dict:
        s = super().summary()
        s.update({
            "zones_used": self.next_zid,
            "seals": self.seal_count,
            "probe_avg": self.probe_total / max(1, self.n_w),
            "hit_source_sealed_frac": (
                self.hit_source_sealed / max(1, self.n_hits)),
            "cross_zone_desc_frac": (
                self.cross_zone_descs / max(1, self.n_hits)),
            "routing_fn_rate": self.routing_fn / max(1, self.n_w),
            "routing_fn_count": self.routing_fn,
            "cache_wi_hit": self.cache_stats["wi_hit"],
            "cache_shard_hit": self.cache_stats["shard_hit"],
            "cache_shard_miss": self.cache_stats["shard_miss"],
            "cache_filter_hit": self.cache_stats["filter_hit"],
            "cache_filter_miss": self.cache_stats["filter_miss"],
            "fn_sealed_src": self.fn_sealed_src,
            "fn_active_src": self.fn_active_src,
        })
        dr = self.dram_breakdown()
        s["dram_total_mb"] = dr["total"] / 1048576
        s["dram"] = dr
        return s


# ---------------------------------------------------------------------------
# Ablation layers
# ---------------------------------------------------------------------------

class DataPathOnly(LiZoneBase):
    """Layer 2: naive per-zone hash table.  Every probe reads one bucket
    page; every insert is a bucket read-modify-write."""
    name = "DataPathOnly"
    use_shard = False
    split_state = False
    use_cache = False

    def _probe_zone(self, z, fp, out):
        out.md_read_pages += 1
        out.compute_us += self.dev.c_scan
        self.md_read_pages += 1
        out.probes += 1
        self.probe_total += 1
        return fp in z.fps

    def _on_miss_insert(self, dst, fp, out):
        out.md_read_pages += 1
        out.md_rmw_pages += 1
        self.md_read_pages += 1
        self.md_rmw_pages += 1


class PlusIdentityShard(LiZoneBase):
    """Layer 3: + page-aligned identity shards and membership filters.
    One-page exact confirmation, delta-log inserts, but reference updates
    on sealed sources still rewrite identity pages in place."""
    name = "+IdentityShard"
    use_shard = True
    split_state = False
    use_cache = False


class PlusSplitState(LiZoneBase):
    """Layer 4: + identity/reference split state: sealed identity planes
    immutable, all reference mutations appends.  No caches yet."""
    name = "+SplitState"
    use_shard = True
    split_state = True
    use_cache = False


class PlusWorkingIndex(LiZoneBase):
    """Layer 5 (V4): + bounded DRAM working index (L1).  No shard/filter
    caches yet -- isolates the L1 contribution."""
    name = "+WorkingIndex"
    use_shard = True
    split_state = True
    use_cache = False
    use_wi = True


class LiZoneDedup(LiZoneBase):
    """Layer 6 (V4, +FullCache): complete design with the three-tier cache
    (working index L1 + shard cache L2 + filter cache L3) and the bounded
    reference accumulator."""
    name = "LiZoneDedup"
    use_shard = True
    split_state = True
    use_cache = True


class LiZoneDedupPrefix(LiZoneDedup):
    """r=2 with fingerprint-prefix routing instead of LSH (routing ablation).
    Candidates = primary + lineage of the fp-prefix group (no SimHash
    neighbourhood expansion)."""
    name = "LiZoneDedup-r2-prefix"

    def __init__(self, dev=DEFAULT_DEVICE, r: int = 2):
        super().__init__(dev, r=r, route_mode="prefix")


class LiZoneDedupR(LiZoneDedup):
    def __init__(self, dev=DEFAULT_DEVICE, r: int = 2):
        super().__init__(dev, r=r)


def _lizoner(r):
    class _S(LiZoneDedupR):
        def __init__(self, dev=DEFAULT_DEVICE):
            super().__init__(dev, r=r)
    return _S


LiZoneDedupR1 = _lizoner(1)
LiZoneDedupR3 = _lizoner(3)
LiZoneDedupR4 = _lizoner(4)
LiZoneDedupR1.name = "LiZoneDedup-r1"
LiZoneDedupR3.name = "LiZoneDedup-r3"
LiZoneDedupR4.name = "LiZoneDedup-r4"


# ===========================================================================
# Compression systems
# ===========================================================================

SLOT_BYTES = 256


def balloon_bytes(fp: int) -> int:
    cr = fp_crc(fp)
    if cr >= 0.95:
        return PAGE
    return max(SLOT_BYTES, math.ceil(PAGE * cr / SLOT_BYTES) * SLOT_BYTES)


class BalloonZNS(BaseSystem):
    """Slot-aligned LZ4 compression baseline."""
    name = "Balloon-ZNS"

    def __init__(self, dev=DEFAULT_DEVICE):
        super().__init__(dev)
        self.phys_bytes = 0
        self.n_compressed = 0
        self.n_raw = 0
        self.n_zero = 0

    def write(self, fp, L):
        self.n_w += 1
        self.logical_pages += 1
        b = balloon_bytes(fp)
        if b < PAGE:
            self.n_compressed += 1
        else:
            self.n_raw += 1
        if fp_crc(fp) <= 0.05:
            self.n_zero += 1
        self.phys_bytes += b
        self.data_pages += b / PAGE
        self.lz4_calls += 1
        self.md_write_bytes += 2          # slot-map entry (amortized log)
        comp = self.dev.c_lz4_c
        self.compute_us_total += comp
        return ReqOutcome(hit=False, data_pages=b / PAGE, compute_us=comp)

    def summary(self):
        s = super().summary()
        s["physical_bytes"] = self.phys_bytes
        s["mean_cr"] = self.phys_bytes / max(1.0, self.logical_pages * PAGE)
        s["n_compressed"] = self.n_compressed
        s["n_raw"] = self.n_raw
        s["n_zero"] = self.n_zero
        return s


class LiZoneBalloon(LiZoneDedup):
    """Dedup first, LZ4-compress the remaining unique pages."""
    name = "LiZoneDedup+Balloon"

    def __init__(self, dev=DEFAULT_DEVICE, r: int = 2):
        super().__init__(dev, r=r)
        self.phys_bytes = 0
        self.n_compressed = 0
        self.n_raw = 0

    def write(self, fp, L):
        out = super().write(fp, L)
        if out.hit:
            self.phys_bytes += self.dev.dref_bytes
        else:
            b = balloon_bytes(fp)
            self.phys_bytes += b
            if b < PAGE:
                self.n_compressed += 1
            else:
                self.n_raw += 1
            self.lz4_calls += 1
            out.compute_us += self.dev.c_lz4_c
            self.compute_us_total += self.dev.c_lz4_c
            out.data_pages = b / PAGE
            # parent counted a full page; correct data page accounting
            self.data_pages -= 1
            self.data_pages += b / PAGE
        return out

    def summary(self):
        s = super().summary()
        s["data_physical_bytes"] = self.phys_bytes
        return s


SYSTEMS = {
    "nodedup": NoDedup,
    "oracle": GlobalOracle,
    "hostdedup": HostDedup,
    "hostdedup-512m": HostDedup512M,
    "hostdedup-2g": HostDedup2G,
    "hostdedup-8m": HostDedup8M,
    "hostdedup-4g": HostDedup4G,
    "globalindex": GlobalIndex,
    "datapath": DataPathOnly,
    "shard": PlusIdentityShard,
    "split": PlusSplitState,
    "wi": PlusWorkingIndex,
    "lizone": LiZoneDedup,
    "lizone-prefix": LiZoneDedupPrefix,
    "lizone-r1": LiZoneDedupR1,
    "lizone-r3": LiZoneDedupR3,
    "lizone-r4": LiZoneDedupR4,
    "balloon": BalloonZNS,
    "lizone-balloon": LiZoneBalloon,
}


def make_system(key: str, dev: DeviceConfig = DEFAULT_DEVICE,
                track_locus: bool = False) -> BaseSystem:
    cls = SYSTEMS[key]
    try:
        return cls(dev, track_locus=track_locus)
    except TypeError:
        return cls(dev)
