"""
traces.py -- workload construction for the LiZoneDedup evaluation.

Workloads (paper Sec 4.1):
  fsl       : real FSL Homes user006 fingerprint trace (8 KB chunks expanded
              to 4 KB pages), low-to-mid dedup (~1.2x).
  container : versioned container-image stream, high dedup (~4-5x).
  vm        : VM snapshot stream, mid-high dedup (~3x).
  backup    : daily backup snapshot stream, very high dedup (~12x).
  rocksdb   : db_bench-style LSM stream (fillseq/fillrandom/overwrite/
              readwhilewriting), low dedup (~1.1x), semi-compressible.

Content model
-------------
Each write event is a (fp, L) pair: fp is a 64-bit content fingerprint
(truncated SHA-256 stand-in), L is a 24-bit SimHash route digest.  Byte
identical pages always produce identical (fp, L).  For "similar but modified"
pages the generator derives L from the parent page's L by flipping a small
number of bits, which reproduces SimHash's locality preservation.

Per-page content properties are encoded in the top 8 bits of fp:
  bits 63..60 : content type (0 zero, 1 os, 2 app, 3 meta, 4 media/rand)
  bits 59..56 : compression bucket index
  bits 55..0  : content hash

so page properties decode with zero auxiliary state and are deterministic
across replays.  The compressed size of a page is cr(fp)*4096 where cr is
drawn per (type, bucket) from measured LZ4 behaviour of that content class.

Traces are materialised once into flat binary arrays (uint64 fp, uint32 L)
plus an ops array (0 = write, 1 = read-slot marker for read-mix workloads)
and then memory-mapped by every system replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from array import array
from dataclasses import dataclass, asdict

TRACE_DIR = os.environ.get("LZ_TRACE_DIR", os.path.join(os.path.dirname(__file__), "..", "traces"))

LSH_BITS = 24
LSH_MASK = (1 << LSH_BITS) - 1

CTYPE_NAMES = ["zero", "os", "app", "meta", "media"]
# LZ4 mean compression ratio per content type (measured class behaviour):
#   zero pages   -> ~0.02 ; OS/text -> ~0.45 ; app binaries -> ~0.62
#   fs metadata  -> ~0.35 ; media/random -> ~0.99 (stored raw)
CR_BY_TYPE = {0: 0.02, 1: 0.45, 2: 0.62, 3: 0.35, 4: 0.99}
# multiplicative jitter per bucket (7 buckets around 1.0)
CR_BUCKETS = [0.72, 0.84, 0.93, 1.0, 1.08, 1.18, 1.32]


def mix64(x: int) -> int:
    """splitmix64 finalizer -- fast deterministic mixing."""
    x = (x + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return x ^ (x >> 31)


def make_fp(ctype: int, cr_bucket: int, seed: int) -> int:
    """Encode content properties into the top 8 bits of a 64-bit fp."""
    h = mix64(seed) & ((1 << 56) - 1)
    return (ctype << 60) | (cr_bucket << 56) | h


def fp_ctype(fp: int) -> int:
    return fp >> 60


def fp_crc(fp: int) -> float:
    """Deterministic LZ4 compression ratio for this page."""
    t = fp_ctype(fp)
    b = (fp >> 56) & 0xF
    cr = CR_BY_TYPE[t] * CR_BUCKETS[b]
    return min(1.0, cr)


def fp_of_chunk(fp: int, half: int) -> int:
    """Second 4 KB page of an 8 KB chunk (same content properties)."""
    return fp ^ (0xA5A5A5A5A5A5A5A5 if half else 0)


def simhash_of(fp: int) -> int:
    """Route digest for a page whose real bytes are unknown (FSL trace)."""
    return mix64(fp ^ 0x31F2B3C4D5E6F708) & LSH_MASK


def flip_bits(l: int, nbits: int, rnd: random.Random) -> int:
    """Flip nbits random bits of a 24-bit SimHash digest (locality model)."""
    for _ in range(nbits):
        l ^= (1 << rnd.randrange(LSH_BITS))
    return l & LSH_MASK


# ---------------------------------------------------------------------------
# Synthetic versioned-workload generator
# ---------------------------------------------------------------------------

@dataclass
class VersionedConfig:
    name: str
    families: int
    versions: int
    pages_per_version: int
    shared_frac: float     # pages identical to previous version
    mod_frac: float        # modified pages (new fp, SimHash near parent)
    new_frac: float        # brand-new pages
    drift_bits: int        # SimHash bits flipped for modified pages
    ctype_weights: list    # weights over [zero, os, app, meta, media]
    seed: int
    base_shared_frac: float = 0.25   # fraction of family pages that come
                                     # from a global popular base pool
    p_repull: float = 0.32           # per block: re-pull of a recent block
    repull_gap_min: int = 40_000     # events until the re-pull happens
    repull_gap_max: int = 220_000


CONTAINER_CFG = VersionedConfig(
    name="container", families=40, versions=48, pages_per_version=3200,
    shared_frac=0.76, mod_frac=0.06, new_frac=0.18, drift_bits=2,
    ctype_weights=[2, 38, 42, 10, 8], seed=202,
    base_shared_frac=0.35, p_repull=0.25,
    repull_gap_min=30_000, repull_gap_max=150_000,
)

VM_CFG = VersionedConfig(
    name="vm", families=16, versions=60, pages_per_version=6000,
    shared_frac=0.65, mod_frac=0.15, new_frac=0.20, drift_bits=2,
    ctype_weights=[6, 30, 38, 16, 10], seed=303,
    base_shared_frac=0.15, p_repull=0.20,
    repull_gap_min=30_000, repull_gap_max=150_000,
)

BACKUP_CFG = VersionedConfig(
    name="backup", families=10, versions=60, pages_per_version=9000,
    shared_frac=0.88, mod_frac=0.02, new_frac=0.10, drift_bits=1,
    ctype_weights=[9, 28, 34, 22, 7], seed=404,
    base_shared_frac=0.10,
    p_repull=0.18, repull_gap_min=40_000, repull_gap_max=200_000,
)


CLOUD_CFG = VersionedConfig(
    name="cloud", families=48, versions=40, pages_per_version=3200,
    shared_frac=0.25, mod_frac=0.15, new_frac=0.60, drift_bits=3,
    ctype_weights=[3, 22, 30, 25, 20], seed=606,
    base_shared_frac=0.10, p_repull=0.10,
    repull_gap_min=50_000, repull_gap_max=250_000,
)


def gen_versioned(cfg: VersionedConfig):
    """Yield (fp, L) write events for a versioned-image workload.

    A randomised pull scheduler interleaves families at block granularity
    and re-pulls recently-written blocks after a short delay, reproducing
    the strong short-range temporal locality of registry/VM/backup streams.
    """
    rnd = random.Random(cfg.seed)
    ppv = cfg.pages_per_version
    # global popular base pool (shared across families, e.g. distro base)
    base_pool = []
    for i in range(ppv):
        base_pool.append(_rand_page(rnd, cfg.ctype_weights, 9_000_000 + i))
    n_base = int(ppv * cfg.base_shared_frac)

    fam_pools = []
    for f in range(cfg.families):
        pool = list(base_pool[:n_base])
        for i in range(ppv - n_base):
            pool.append(_rand_page(rnd, cfg.ctype_weights, f * 1_000_000 + i))
        fam_pools.append(pool)

    counter = 100_000_000
    streams = []
    for f in range(cfg.families):
        events = []
        pool = fam_pools[f]
        for v in range(cfg.versions):
            newpool = []
            for i in range(ppv):
                u = rnd.random()
                if v > 0 and u < cfg.shared_frac:
                    pg = pool[i]
                elif v > 0 and u < cfg.shared_frac + cfg.mod_frac:
                    parent = pool[i]
                    counter += 1
                    pg = (_rand_fp(rnd, cfg.ctype_weights, counter),
                          flip_bits(parent[1], cfg.drift_bits, rnd))
                else:
                    counter += 1
                    pg = _rand_page(rnd, cfg.ctype_weights, counter)
                newpool.append(pg)
                events.append(pg)
            pool = newpool
        streams.append(events)

    # ---------------- pull scheduler ----------------
    block = 512
    ptr = [0] * cfg.families
    recent: List[Tuple[int, int, int]] = []     # (f, start, end) ring
    pending: List[Tuple[int, int, int, int]] = []  # (due, f, s, e)
    heapq = __import__("heapq")
    emitted = 0
    stream_len = [len(s) for s in streams]
    while True:
        # fire due re-pulls first
        while pending and pending[0][0] <= emitted:
            _, f, s, e = heapq.heappop(pending)
            evs = streams[f]
            for i in range(s, e):
                yield evs[i]
            emitted += e - s
        # pick a random family that still has fresh content
        avail = [f for f in range(cfg.families) if ptr[f] < stream_len[f]]
        if not avail:
            if pending:
                # only re-pulls remain; jump time forward
                due = pending[0][0]
                _, f, s, e = heapq.heappop(pending)
                evs = streams[f]
                for i in range(s, e):
                    yield evs[i]
                emitted = max(emitted, due) + (e - s)
                continue
            break
        f = avail[rnd.randrange(len(avail))]
        s = ptr[f]
        e = min(s + block, stream_len[f])
        evs = streams[f]
        for i in range(s, e):
            yield evs[i]
        emitted += e - s
        ptr[f] = e
        recent.append((f, s, e))
        if len(recent) > 4096:
            del recent[:2048]
        if recent and rnd.random() < cfg.p_repull:
            rf, rs, re_ = recent[rnd.randrange(len(recent))]
            due = emitted + rnd.randrange(cfg.repull_gap_min,
                                           cfg.repull_gap_max)
            heapq.heappush(pending, (due, rf, rs, re_))


def _rand_fp(rnd: random.Random, weights, seed: int) -> int:
    ctype = rnd.choices(range(len(weights)), weights=weights)[0]
    b = rnd.randrange(len(CR_BUCKETS))
    return make_fp(ctype, b, seed)


def _rand_page(rnd, weights, seed):
    return (_rand_fp(rnd, weights, seed), rnd.getrandbits(LSH_BITS))


# ---------------------------------------------------------------------------
# RocksDB-style workload
# ---------------------------------------------------------------------------

@dataclass
class RocksCfg:
    name: str = "rocksdb"
    fillseq_pages: int = 500_000       # initial fill (unique keys, seq)
    fillrandom_pages: int = 500_000    # unique keys, random order
    overwrite_pages: int = 600_000     # overwrite existing keys
    rww_ops: int = 700_000             # readwhilewriting (mixed)
    rww_read_frac: float = 0.75
    same_value_frac: float = 0.28      # overwrite writes identical value page
    compaction_dup_frac: float = 0.12  # compaction re-emission of live pages
    seed: int = 505
    ctype_weights: list = (0, 22, 30, 26, 22)


def gen_rocksdb(cfg: RocksCfg):
    """Yield (op, fp, L): op 0=write, 1=read-marker (L resolved at replay).

    SST/L0 pages are semi-compressible; overwritten values that did not
    change produce exact duplicates; compaction re-emits a fraction of live
    pages as duplicates.
    """
    rnd = random.Random(cfg.seed)
    live = []          # recent value pages (fp, L)
    counter = 7_000_000_000

    def newpage():
        nonlocal counter
        counter += 1
        return (_rand_fp(rnd, list(cfg.ctype_weights), counter),
                rnd.getrandbits(LSH_BITS))

    # --- fillseq: unique pages written sequentially
    for _ in range(cfg.fillseq_pages):
        pg = newpage()
        live.append(pg)
        yield (0, pg[0], pg[1])

    # --- fillrandom: more unique pages
    for _ in range(cfg.fillrandom_pages):
        pg = newpage()
        live.append(pg)
        yield (0, pg[0], pg[1])
    # keep live bounded
    if len(live) > 800_000:
        live = live[-800_000:]

    # --- overwrite: random key, value usually changes
    for _ in range(cfg.overwrite_pages):
        if rnd.random() < cfg.same_value_frac and live:
            pg = live[rnd.randrange(len(live))]
        else:
            pg = newpage()
            live.append(pg)
        yield (0, pg[0], pg[1])
        if rnd.random() < cfg.compaction_dup_frac and live:
            cpg = live[rnd.randrange(len(live))]
            yield (0, cpg[0], cpg[1])

    # --- readwhilewriting: mixed reads (marker) + writes
    for _ in range(cfg.rww_ops):
        if rnd.random() < cfg.rww_read_frac:
            yield (1, 0, 0)
        else:
            if rnd.random() < 0.30 and live:
                pg = live[rnd.randrange(len(live))]
            else:
                pg = newpage()
                live.append(pg)
            yield (0, pg[0], pg[1])


# ---------------------------------------------------------------------------
# FSL trace loading
# ---------------------------------------------------------------------------

def gen_fsl(path: str):
    """Stream the real FSL Homes fingerprint trace as 4 KB page events.

    Each 8 KB chunk hash expands to two correlated 4 KB page fps; page
    properties (content class, compressibility) are derived from the chunk
    hash with a distribution measured for home-directory data.
    """
    with open(path, "r") as f:
        for line in f:
            s = line.strip()
            if len(s) < 16:
                continue
            h = int(s[:16], 16)
            # classify chunk: 3% zero, 30% os, 40% app, 15% meta, 12% media
            u = (mix64(h ^ 0xABCD) >> 11) / float(1 << 53)
            if u < 0.03:
                ctype = 0
            elif u < 0.33:
                ctype = 1
            elif u < 0.73:
                ctype = 2
            elif u < 0.88:
                ctype = 3
            else:
                ctype = 4
            b = (mix64(h ^ 0x13572468) >> 40) % len(CR_BUCKETS)
            fp0 = make_fp(ctype, b, h)
            fp1 = make_fp(ctype, b, h ^ 0x5DEECE66D)
            yield fp0, simhash_of(fp0)
            yield fp1, simhash_of(fp1)


# ---------------------------------------------------------------------------
# Trace materialisation (build once, replay many)
# ---------------------------------------------------------------------------

def trace_paths(name: str):
    base = os.path.join(TRACE_DIR, name)
    return (base + ".fp.bin", base + ".l.bin", base + ".ops.bin",
            base + ".meta.json")


def build_trace(name: str, fsl_path: str | None = None, force: bool = False):
    """Materialise a workload into flat binary arrays."""
    fp_p, l_p, ops_p, meta_p = trace_paths(name)
    if os.path.exists(meta_p) and not force:
        return False
    os.makedirs(TRACE_DIR, exist_ok=True)

    fps = array("Q")
    ls = array("I")
    ops = array("B")
    if name == "fsl":
        assert fsl_path, "fsl trace needs path to fingerprint file"
        for fp, l in gen_fsl(fsl_path):
            fps.append(fp)
            ls.append(l)
            ops.append(0)
        desc = "FSL Homes user006 real fingerprint trace (8KB chunks -> 2 pages)"
    elif name == "rocksdb":
        for op, fp, l in gen_rocksdb(RocksCfg()):
            fps.append(fp)
            ls.append(l)
            ops.append(op)
        desc = "db_bench style LSM stream: fillseq/fillrandom/overwrite/readwhilewriting"
    elif name in ("container", "vm", "backup", "cloud"):
        cfg = {"container": CONTAINER_CFG, "vm": VM_CFG,
               "backup": BACKUP_CFG, "cloud": CLOUD_CFG}[name]
        for fp, l in gen_versioned(cfg):
            fps.append(fp)
            ls.append(l)
            ops.append(0)
        desc = f"versioned synthetic stream calibrated to published dedup ratios ({name})"
    else:
        raise ValueError(name)

    with open(fp_p, "wb") as f:
        fps.tofile(f)
    with open(l_p, "wb") as f:
        ls.tofile(f)
    with open(ops_p, "wb") as f:
        ops.tofile(f)
    meta = {"name": name, "events": len(fps), "desc": desc}
    with open(meta_p, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"[trace] {name}: {len(fps)} events")
    return True


class Trace:
    """Memory-mapped replayable trace."""

    def __init__(self, name: str):
        fp_p, l_p, ops_p, meta_p = trace_paths(name)
        with open(meta_p) as f:
            self.meta = json.load(f)
        self.name = name
        self.n = self.meta["events"]
        self._fp = array("Q")
        with open(fp_p, "rb") as f:
            self._fp.fromfile(f, self.n)
        self._l = array("I")
        with open(l_p, "rb") as f:
            self._l.fromfile(f, self.n)
        self._ops = array("B")
        with open(ops_p, "rb") as f:
            self._ops.fromfile(f, self.n)

    def __len__(self):
        return self.n

    def event(self, i: int):
        """Returns (op, fp, L)."""
        return self._ops[i], self._fp[i], self._l[i]

    def iter_writes(self):
        for i in range(self.n):
            if self._ops[i] == 0:
                yield self._fp[i], self._l[i]

    def stats(self):
        seen = set()
        dups = 0
        n = 0
        for fp, _ in self.iter_writes():
            if fp in seen:
                dups += 1
            else:
                seen.add(fp)
            n += 1
        from collections import Counter
        tc = Counter(fp_ctype(fp) for fp in seen)
        return {"events": n, "unique": len(seen), "dups": dups,
                "dup_frac": dups / max(1, n),
                "oracle_dedup_ratio": n / max(1, len(seen)),
                "type_mix": {CTYPE_NAMES[k]: v for k, v in sorted(tc.items())}}


if __name__ == "__main__":
    import sys
    fsl = sys.argv[1] if len(sys.argv) > 1 else None
    for name in ("fsl", "container", "vm", "backup", "cloud", "rocksdb"):
        if name == "fsl" and (fsl is None or not os.path.exists(fsl)):
            print("skip fsl (no path)")
            continue
        build_trace(name, fsl_path=fsl)
        t = Trace(name)
        print(name, t.stats())
