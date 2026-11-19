"""
lifecycle.py -- zone lifecycle / GC / reset-pending experiment (Sec 4.7).

Extends LiZoneDedup with slot-level LBA tracking, host discards, host
resets (reset-pending while incoming cross-zone references remain), and
data-moving GC under free-zone pressure (with descriptor updates for
migrated source pages).  All liveness/dead counters are maintained
incrementally so per-event cost stays O(1).
"""

from __future__ import annotations

import math
import random
from array import array
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from .device import DeviceConfig, DEFAULT_DEVICE
from .systems import (LiZoneDedup, LZZone, ReqOutcome, ACTIVE, SEALED,
                       RESET_PENDING, DEAD)
from .traces import Trace

PAGE = 4096


class LifeZone(LZZone):
    __slots__ = ("slot_is_desc", "slot_fp", "slot_dead", "live_slots",
                 "dead_data", "pending_dead")

    def __init__(self, zid: int, gen: int = 0):
        super().__init__(zid, gen)
        self.slot_is_desc = array("B")   # per logical slot
        self.slot_fp = array("q")        # data: own fp; descriptor: source fp
        self.slot_dead = array("B")
        self.live_slots = 0
        self.dead_data = 0               # dead data pages (refcount == 0)
        self.pending_dead = set()        # fps: slot dead but still referenced


class LifecycleSim(LiZoneDedup):
    """LiZoneDedup + host-visible slot lifecycle."""

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE, r: int = 2,
                 n_zones_device: int = 96, gc_watermark_lo: float = 0.15,
                 gc_watermark_hi: float = 0.40, migrate: bool = False,
                 max_active: int = 16, route_mode: str = "lsh",
                 track_locus: bool = False):
        super().__init__(dev, r=r, route_mode=route_mode,
                         track_locus=track_locus)
        self.n_zones_device = n_zones_device
        self.gc_lo = gc_watermark_lo
        self.gc_hi = gc_watermark_hi
        self.migrate = migrate          # opt 3: targeted pinned-zone migration
        self.max_active = max_active
        self.refcount: Dict[int, int] = {}      # fp -> live descriptors
        self._fp_zone: Dict[int, int] = {}      # fp -> zone holding the page
        self.alive_zones = 0
        self._gc_cooldown = 0
        self.gc_victim_log: List[Tuple[float, int, int, float, float]] = []
        # metrics
        self.n_discards = 0
        self.n_host_resets = 0
        self.n_pending_resets = 0
        self.pending_durations_events: List[int] = []
        self.pending_now: Dict[int, int] = {}
        self.gc_count = 0
        self.gc_migrated_pages = 0
        self.gc_desc_updates = 0
        self.gc_desc_scan_pages = 0.0
        self.gc_busy_us_total = 0.0
        self.fg_busy_us_total = 0.0
        self.desc_total = 0
        self.cross_zone_desc_created = 0
        self.sealed_ref_created = 0
        self.copy_forward = 0
        self.uref_emitted = 0
        self.gc_blocked_pinned = 0       # victims skipped: incoming refs
        self.n_targeted_migs = 0         # opt 3 counter
        self.targeted_pages = 0
        self.gc_reclaimed_dead = 0       # dead pages erased by GC victims
        self.pressure_samples: List[int] = []   # opt 4: open-zone occupancy

    # ------------------------------------------------------------------
    def _new_zone(self) -> LZZone:
        z = LifeZone(self.next_zid)
        z.created_at = self.n_w
        self.next_zid += 1
        self.zones[z.zid] = z
        self.n_active += 1
        self.alive_zones += 1
        return z

    # ------------------------------------------------------------------
    def write(self, fp: int, L: int):
        out = super().write(fp, L)
        self._record_slot(out, fp)
        self._post_write(out)
        return out

    def _record_slot(self, out, fp: int):
        """Record the logical slot the decision consumed (shared by the
        routed design and any alternate dedup policy)."""
        z: LifeZone = self.zones[self._last_dst]
        if len(z.slot_fp) < z.slots:
            if out.hit:
                z.slot_fp.append(fp)          # descriptor stores SOURCE fp
                z.slot_is_desc.append(1)
                self.refcount[fp] = self.refcount.get(fp, 0) + 1
                self.desc_total += 1
                if out.cross_zone:
                    self.cross_zone_desc_created += 1
                if out.sealed_source:
                    self.sealed_ref_created += 1
            else:
                z.slot_fp.append(fp)
                z.slot_is_desc.append(0)
                self._fp_zone[fp] = z.zid
            z.slot_dead.append(0)
            z.live_slots += 1

    def _post_write(self, out):
        dev = self.dev
        self.fg_busy_us_total += out.data_pages * dev.t_prog
        self.fg_busy_us_total += out.md_read_pages * dev.t_read
        self.fg_busy_us_total += out.md_write_bytes / PAGE * dev.t_prog
        if self.n_w % 10_000 == 0:
            # opt 4: concurrent reset-pending zones each hold one
            # readable-open slot that cannot take appends
            self.pressure_samples.append(len(self.pending_now))
        if (self.alive_zones >= self.n_zones_device - 4
                and self.n_w >= self._gc_cooldown):
            self._gc_batch()

    # ------------------------------------------------------------------
    def avg_dead_ratio(self) -> float:
        zs = [z for z in self.zones.values()
              if z.state != DEAD and z.data_pages > 0]
        if not zs:
            return 0.0
        return (sum(z.dead_data for z in zs) /
                sum(z.data_pages for z in zs))

    def free_zones(self) -> int:
        return self.n_zones_device - self.alive_zones

    # ------------------------------------------------------------------
    def discard_wave(self, keep_frac: float, rng: random.Random,
                     old_frac: float = 0.8, retention_events: int = 800_000,
                     wave: int = 0) -> int:
        """Host deletes old versions: zones whose content is older than the
        retention horizon are deleted completely (version expiry); the
        middle band loses a random (1-keep_frac) fraction of live slots."""
        total = 0
        horizon = self.n_w - retention_events
        order = sorted((z for z in self.zones.values() if z.state != DEAD),
                       key=lambda z: z.created_at)
        n_old = max(1, int(len(order) * old_frac))
        for zi, z in enumerate(order[:n_old]):
            # oldest third of the band: full expiry (whole version deleted)
            full = zi < n_old // 3 and z.created_at <= horizon
            live = [i for i in range(len(z.slot_fp)) if not z.slot_dead[i]]
            if full:
                for i in live:
                    self._discard_slot(z, i)
                total += len(live)
            else:
                n = int(len(live) * (1.0 - keep_frac))
                rng.shuffle(live)
                for i in live[:n]:
                    self._discard_slot(z, i)
                total += n
        return total

    def _discard_slot(self, z: LifeZone, i: int):
        if z.slot_dead[i]:
            return
        z.slot_dead[i] = 1
        z.dead_slots += 1
        z.live_slots -= 1
        self.n_discards += 1
        fp = z.slot_fp[i]
        if z.slot_is_desc[i]:
            c = self.refcount.get(fp, 0) - 1
            if c <= 0:
                self.refcount.pop(fp, None)
                # a data page whose slot is already dead may now be dead
                szid = self._fp_zone.get(fp)
                if szid is not None:
                    sz = self.zones.get(szid)
                    if sz is not None and fp in sz.pending_dead:
                        sz.pending_dead.discard(fp)
                        sz.dead_data += 1
            else:
                self.refcount[fp] = c
            z.alive_descs = max(0, z.alive_descs - 1)
            self.uref_emitted += 1
            szid = self._fp_zone.get(fp)
            if szid is not None and szid != z.zid:
                sz = self.zones.get(szid)
                if sz is not None and sz.incoming_refs > 0:
                    sz.incoming_refs -= 1
                    self._maybe_finish_pending(sz)
        else:
            if self.refcount.get(fp, 0) == 0:
                z.dead_data += 1
            else:
                z.pending_dead.add(fp)

    # ------------------------------------------------------------------
    def host_reset_fully_dead(self) -> int:
        """Host resets every zone whose logical slots are all dead."""
        n = 0
        for z in list(self.zones.values()):
            if z.state == DEAD or z.live_slots > 0 or len(z.slot_fp) == 0:
                continue
            if self.host_reset(z.zid) is not None:
                n += 1
        return n

    def host_reset(self, zid: int) -> Optional[str]:
        z: LifeZone = self.zones[zid]
        if z.state == DEAD:
            return None
        self.n_host_resets += 1
        for i in range(len(z.slot_fp)):
            if not z.slot_dead[i]:
                self._discard_slot(z, i)
        z.alive_descs = 0
        if z.state == RESET_PENDING:
            return "pending"
        if z.incoming_refs > 0:
            z.state = RESET_PENDING
            z.reset_pending_at = self.n_w
            self.n_pending_resets += 1
            self.pending_now[z.zid] = self.n_w
            return "pending"
        self._erase(z)
        return "done"

    def _maybe_finish_pending(self, z: LZZone):
        if z.state == RESET_PENDING and z.incoming_refs == 0:
            start = self.pending_now.pop(z.zid, z.reset_pending_at)
            self.pending_durations_events.append(max(0, self.n_w - start))
            self._erase(z)

    def _erase(self, z: LZZone):
        if z.state == DEAD:
            return
        z.state = DEAD
        self.alive_zones -= 1
        for g in range(self.n_groups):
            if self.primary[g] == z.zid:
                self._install_primary(g)
        self._gc_lineage()

    # ------------------------------------------------------------------
    def _gc_batch(self):
        """Run data-moving GC until the free-zone high watermark is met
        (bounded; retried only after a cooldown of 20k writes)."""
        target = int(self.n_zones_device * self.gc_hi)
        guard = 0
        while self.free_zones() < target and guard < 8:
            guard += 1
            if not self._run_gc():
                break
        self._gc_cooldown = self.n_w + 20_000

    def _run_gc(self) -> bool:
        best, best_dr = None, -1.0
        for z in self.zones.values():
            if z.state == DEAD or z.data_pages == 0:
                continue
            dr = z.dead_data / z.data_pages
            if dr > best_dr:
                best_dr, best = dr, z
        if best is None or best_dr <= 0.10:
            return False
        z: LifeZone = best
        targeted = False
        if z.incoming_refs > 0:
            valid_frac = 1.0 - (z.dead_data / max(1, z.data_pages))
            if self.migrate and valid_frac < 0.20:
                # opt 3: targeted migration -- most pages are dead, only a
                # few are externally referenced; move those pages to a fresh
                # zone and compact the referencing descriptors, releasing
                # the old zone for reset
                targeted = True
                self.n_targeted_migs += 1
            else:
                # conservative source-zone pinning: prefer an unpinned victim
                alt = None
                alt_dr = 0.30
                for z2 in self.zones.values():
                    if z2.state != DEAD and z2.data_pages > 0 and \
                            z2.incoming_refs == 0:
                        d2 = z2.dead_data / z2.data_pages
                        if d2 > alt_dr:
                            alt_dr, alt = d2, z2
                if alt is not None:
                    z = alt
                else:
                    # pinned with live incoming references and no targetable
                    # alternative: the zone (and its dead pages) stays
                    # trapped until the references age out
                    self.gc_blocked_pinned += 1
                    return False
        self.gc_count += 1
        rc = self.refcount
        victim_dr = z.dead_data / max(1, z.data_pages)
        live_slots = [i for i in range(len(z.slot_fp))
                      if not z.slot_is_desc[i] and
                      (not z.slot_dead[i] or rc.get(z.slot_fp[i], 0) > 0)]
        mig_pages = len(live_slots)
        self.gc_migrated_pages += mig_pages
        mig_fps = [z.slot_fp[i] for i in live_slots]
        n_upd = sum(rc.get(fp, 0) for fp in mig_fps)
        self.gc_desc_updates += n_upd
        desc_bytes = sum(zz.alive_descs for zz in self.zones.values()) * 16
        scan_pages = math.ceil(desc_bytes / PAGE)
        self.gc_desc_scan_pages += scan_pages
        dev = self.dev
        busy = (mig_pages * (dev.t_read + dev.t_prog) +
                scan_pages * dev.t_read +
                n_upd * 16 / PAGE * dev.t_prog)
        self.gc_busy_us_total += busy
        self.gc_victim_log.append((victim_dr, mig_pages, n_upd, scan_pages,
                                   busy))
        if targeted and z.state == RESET_PENDING:
            start = self.pending_now.pop(z.zid, z.reset_pending_at)
            self.pending_durations_events.append(max(0, self.n_w - start))
        if targeted:
            # every referencing descriptor is compacted onto the new zone
            # (counted in n_upd) and emits one UREF, so the old zone's
            # incoming count drops to zero and it can erase immediately
            z.incoming_refs = 0
            self.uref_emitted += n_upd
            self.targeted_pages += mig_pages
        self.gc_reclaimed_dead += z.dead_data
        self._erase(z)
        if mig_fps:
            newz = self._new_zone()
            for fp in mig_fps:
                newz.fps.add(fp)
                newz.bloom_n += 1
                self._fp_zone[fp] = newz.zid
            newz.data_pages = len(mig_fps)
            newz.slots = len(mig_fps)
            newz.slot_fp.extend(mig_fps)
            newz.slot_is_desc.extend([0] * len(mig_fps))
            newz.slot_dead.extend([1] * len(mig_fps))
            # migrated pages are live data without host-visible slots
            newz.live_slots = 0
            # the migration target is a sealed holding area, not an
            # allocation primary -- keep it out of the active-zone count
            newz.state = SEALED
            self.n_active -= 1
        return True

    # ------------------------------------------------------------------
    def incoming_stats(self) -> dict:
        zs = [z for z in self.zones.values() if z.state != DEAD]
        peaks = [z.incoming_refs for z in zs]
        return {
            "mean_incoming": sum(peaks) / max(1, len(peaks)),
            "max_incoming": max(peaks) if peaks else 0,
            "zones_nonzero_incoming": sum(1 for p in peaks if p > 0),
        }



# ---------------------------------------------------------------------------
# Global-index lifecycle arm (rebuttal: distinguish from a global index)
# ---------------------------------------------------------------------------

class GlobalIndexLife(LifecycleSim):
    """PageFTL-style arm under the same zone lifecycle: an exact
    device-wide fingerprint index (flash-resident truth) with a bounded
    DRAM cache, and page-level remap semantics -- a hit remaps in the
    index (no descriptor append, no reference pinning; its GC moves live
    pages freely).  DRAM working set = min(index bytes, cache cap); the
    equal-footprint comparison passes cap = the routed design's measured
    DRAM, which is exactly Reviewer A's criterion."""

    name = "GlobalIndex-Life"

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE,
                 n_zones_device: int = 96, gc_watermark_hi: float = 0.40,
                 dram_cache_bytes: int = 0):
        super().__init__(dev=dev, r=2, n_zones_device=n_zones_device,
                         gc_watermark_hi=gc_watermark_hi, migrate=True,
                         max_active=16)
        # the fp -> source-zone truth is self._fp_zone (the base class
        # maintains it across GC migration); gidx_set only sizes the flash
        # index and the DRAM cache
        self.gidx_set: set = set()
        self.gcache: "OrderedDict[int, int]" = OrderedDict()
        self.cache_cap = dram_cache_bytes     # 0 = uncapped reference arm
        self.n_cache_hits = 0
        self.n_cache_misses = 0
        self._cur_seq: Optional[LifeZone] = None

    # ---- dedup decision (replaces the routed C1/C2 lookup) -------------
    def write(self, fp: int, L: int):
        dev = self.dev
        self.n_w += 1
        self.logical_pages += 1
        out = ReqOutcome()
        out.compute_us += dev.c_sha

        # bounded-DRAM cache over the flash index
        if fp in self.gcache:
            self.gcache.move_to_end(fp)
            self.n_cache_hits += 1
        else:
            self.n_cache_misses += 1
            out.md_read_pages += 1.3          # flash index page(s)
            self.md_read_pages += 1.3
            if self.cache_cap:
                self.gcache[fp] = 1
                while len(self.gcache) * dev.index_entry_bytes                         > self.cache_cap:
                    self.gcache.popitem(last=False)

        src_zid = self._fp_zone.get(fp)
        src = None
        if src_zid is not None:
            cand = self.zones.get(src_zid)
            if cand is not None and cand.state != DEAD:
                src = cand

        if self._cur_seq is None or self._cur_seq.state != ACTIVE or                 self._cur_seq.slots >= dev.pages_per_zone:
            if self.alive_zones >= self.n_zones_device - 4                     and self.n_w >= self._gc_cooldown:
                self._gc_batch()
            if self.free_zones() <= 0:
                # nothing to open: keep appending to the last zone's slots
                # beyond capacity is unphysical -- mark and reuse newest
                alive = [z for z in self.zones.values() if z.state != DEAD]
                self._cur_seq = alive[-1] if alive else self._new_zone()
            else:
                self._cur_seq = self._new_zone()
        dst = self._cur_seq

        if src is not None:
            self.n_hits += 1
            out.hit = True
            out.data_pages = 0
            out.sealed_source = (src.state == SEALED)
            out.cross_zone = (src.zid != dst.zid)
            self._last_src = src.zid
            if out.sealed_source:
                self.hit_source_sealed += 1
            else:
                self.hit_source_active += 1
            if self.track_locus:
                self._locus[fp] = src.zid
        else:
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
            dst.fps.add(fp)
            dst.bloom_n += 1
            dst.data_pages += 1
            self.data_pages += 1
            self._fp_zone[fp] = dst.zid
            self.gidx_set.add(fp)
            self._last_src = dst.zid
            out.data_pages = 1
            # index insert: bucket read + RMW write-back
            out.md_read_pages += 1.0
            self.md_read_pages += 1.0
            out.md_rmw_pages += 1
            self.md_rmw_pages += 1
            self.md_write_bytes += PAGE
            out.md_write_bytes = PAGE
            if self.track_locus:
                self._locus[fp] = dst.zid
        dst.slots += 1
        self._last_dst = dst.zid
        self.compute_us_total += out.compute_us

        self._record_slot(out, fp)
        self._post_write(out)
        return out

    # ---- page-FTL remap semantics on reset -------------------------------
    def _migrate_fps(self, z: LifeZone, fps):
        """Page-level remap: referenced pages of a resetting zone move to
        a fresh holding zone (their remap-table entries are rewritten);
        this is the FTL analogue of the routed design's reset deferral,
        so the comparison isolates the index/lookup design."""
        if not fps:
            return
        newz = self._new_zone()
        for fp in fps:
            newz.fps.add(fp)
            newz.bloom_n += 1
            newz.data_pages += 1
            newz.slot_fp.append(fp)
            newz.slot_is_desc.append(0)
            newz.slot_dead.append(1)     # live data, no host-visible slot
            self._fp_zone[fp] = newz.zid
        newz.slots = len(fps)
        newz.live_slots = 0
        newz.state = SEALED              # holding area, not a primary
        self.n_active -= 1
        self.gc_migrated_pages += len(fps)
        self.md_write_bytes += len(fps) * 16   # remap-table updates
        z.pending_dead -= set(fps)

    def host_reset(self, zid):
        z: LifeZone = self.zones.get(zid)
        if (z is not None and z.state != DEAD and z.live_slots == 0
                and len(z.slot_fp) > 0 and z.pending_dead):
            mig = [fp for fp in z.pending_dead
                   if self.refcount.get(fp, 0) > 0]
            self._migrate_fps(z, mig)
        return super().host_reset(zid)

    # ---- keep the flash index consistent with erased zones -------------
    def _erase(self, z: LZZone):
        if z.state != DEAD:
            for fp in z.fps:
                self.gidx_set.discard(fp)
                self.gcache.pop(fp, None)
        super()._erase(z)
        if self._cur_seq is not None and self._cur_seq.zid == z.zid:
            self._cur_seq = None

    # ---- DRAM accounting ------------------------------------------------
    def index_bytes(self) -> int:
        return len(self.gidx_set) * self.dev.index_entry_bytes

    def dram_working_set(self) -> int:
        if not self.cache_cap:
            return self.index_bytes()
        return min(self.index_bytes(), self.cache_cap)

# ---------------------------------------------------------------------------
# NoDedup lifecycle baseline
# ---------------------------------------------------------------------------

class NoDupLife:
    """Plain ZNS lifecycle: slots, discards, host resets, GC migration."""

    name = "NoDedup-ZNS"

    def __init__(self, dev: DeviceConfig = DEFAULT_DEVICE,
                 n_zones_device: int = 96, gc_watermark_hi: float = 0.40):
        self.dev = dev
        self.ppz = dev.pages_per_zone
        self.n_zones_device = n_zones_device
        self.gc_hi = gc_watermark_hi
        self.zones: Dict[int, Tuple[array, array]] = {}
        self.next_zid = 0
        self.cur = None
        self.cur_slots = 0
        self.n_w = 0
        self.n_discards = 0
        self.gc_count = 0
        self.gc_migrated_pages = 0
        self.gc_busy_us_total = 0.0
        self.fg_busy_us_total = 0.0
        self.n_host_resets = 0

    def _open(self):
        self.cur = self.next_zid
        self.next_zid += 1
        self.zones[self.cur] = (array("q"), array("B"))
        self.cur_slots = 0

    def write(self, fp: int, L: int):
        if self.cur is None or self.cur_slots >= self.ppz or \
                len(self.zones) >= self.n_zones_device - 4:
            self._gc_batch()
        if self.cur is None or self.cur_slots >= self.ppz:
            self._open()
        sfp, sd = self.zones[self.cur]
        sfp.append(fp)
        sd.append(0)
        self.cur_slots += 1
        self.n_w += 1
        self.fg_busy_us_total += self.dev.t_prog
        if len(self.zones) >= self.n_zones_device - 4 and \
                self.n_w >= getattr(self, "_cool", 0):
            self._gc_batch()
            self._cool = self.n_w + 20000

    def free_zones(self) -> int:
        return self.n_zones_device - len(self.zones)

    def avg_dead_ratio(self) -> float:
        if not self.zones:
            return 0.0
        dead = sum(sum(sd) for _, sd in self.zones.values())
        tot = sum(len(sfp) for sfp, _ in self.zones.values())
        return dead / max(1, tot)

    def discard_wave(self, keep_frac: float, rng: random.Random,
                     old_frac: float = 0.8, retention_events: int = 800_000,
                     wave: int = 0) -> int:
        total = 0
        order = sorted(self.zones, key=lambda zid: zid)
        n_old = max(1, int(len(order) * old_frac))
        for zi, zid in enumerate(order[:n_old]):
            sfp, sd = self.zones[zid]
            live = [i for i in range(len(sfp)) if not sd[i]]
            if zi < n_old // 3:
                for i in live:
                    sd[i] = 1
                total += len(live)
            else:
                n = int(len(live) * (1.0 - keep_frac))
                rng.shuffle(live)
                for i in live[:n]:
                    sd[i] = 1
                total += n
        self.n_discards += total
        return total

    def host_reset_fully_dead(self) -> int:
        n = 0
        for zid in list(self.zones):
            sfp, sd = self.zones[zid]
            if sfp and all(sd):
                del self.zones[zid]
                if self.cur == zid:
                    self.cur = None
                self.n_host_resets += 1
                n += 1
        return n

    def _gc_batch(self):
        target = int(self.n_zones_device * self.gc_hi)
        guard = 0
        while self.free_zones() < target and guard < 64:
            guard += 1
            if not self._run_gc():
                break

    def _run_gc(self) -> bool:
        best, best_dr = None, -1.0
        for zid, (sfp, sd) in self.zones.items():
            dr = sum(sd) / max(1, len(sfp))
            if dr > best_dr:
                best_dr, best = dr, zid
        if best is None or best_dr <= 0.10:
            return False
        sfp, sd = self.zones.pop(best)
        if self.cur == best:
            self.cur = None
        live = sum(1 for d in sd if not d)
        self.gc_count += 1
        self.gc_migrated_pages += live
        dev = self.dev
        self.gc_busy_us_total += live * (dev.t_read + dev.t_prog)
        if live:
            self.zones[self.next_zid] = (array("q", [0]) * live,
                                         array("B", [0]) * live)
            self.next_zid += 1
        return True
