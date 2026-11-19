"""
device.py -- ZNS SSD device model and calibrated timing constants.

Calibration sources (paper Sec 4.1):
  * Samsung PM9D3Z / WD Ultrastar DC ZN540 public datasheets (TLC ZNS).
  * Embedded controller compute: quad-core ARM Cortex-A55-class @ 2.0 GHz,
    with per-op costs folded from public embedded benchmarks.

Timing model
------------
The device is modeled as C_CHAN NAND channels (servers) + N_CORE controller
cores (servers).  Per-op service times:

  * 4 KB page read       : t_read  = 85 us   (channel occupancy; TLC tR)
  * 4 KB page program    : t_prog  = 60 us   (channel occupancy, folded die
                           interleave; C_CHAN * 4KB / t_prog = seq write BW)
  * zone erase           : t_erase = 4 ms    (background)

Derived specs (1 TB config, 24 channels):
  * sequential write BW  : 24 * 4096 / 60us  ~=  1.63 GB/s
                           (PM9D3Z spec 1.4-1.7 GB/s, err < 10%)
  * QD1 4 KB read latency: 85 us             (spec 70-100 us)

Controller compute (per 4 KB page, one core):
  * SHA-256 (truncated 12 B) : 6.8 us   (~600 MB/s per core)
  * SimHash route digest     : 1.4 us   (xxHash64 sub-blocks + fold, ~3 GB/s)
  * LZ4 compress             : 8.2 us   (~500 MB/s per core)
  * LZ4 decompress           : 2.1 us   (~2 GB/s per core)
  * route-table lookup       : 0.10 us  (DRAM)
  * membership filter probe  : 0.15 us  (DRAM)
  * identity shard scan      : 0.25 us  (DRAM-cached 4 KB page)
  * DREF buffer commit       : 0.05 us  (power-loss-protected DRAM)
  * per-slot status lookup   : 0.05 us
  * descriptor parse         : 0.10 us

All numbers can be scaled by sensitivity factors (paper Sec 4.6).
"""

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeviceConfig:
    # Geometry (default = paper Sec 4.1 baseline)
    capacity_gb: int = 1024
    zone_size_mb: int = 128
    page_size_b: int = 4096
    max_active_zones: int = 16      # V4 spec
    dram_mb: int = 128              # V4 spec

    # NAND timing (us) -- unified with the FEMU prototype per the
    # supplementary spec: FEMU-default 40/200/2000 us, 8 channels
    # (LearnedFTL / Hi-ZNS / Balloon-ZNS all use this configuration)
    t_read_us: float = 40.0
    t_prog_us: float = 200.0
    t_erase_us: float = 2000.0
    n_channels: int = 8
    n_luns: int = 8               # dies per channel (spec)
    n_planes: int = 2             # planes per die (spec)

    @property
    def n_nand_units(self) -> int:
        """Parallel NAND units = ch x lun x plane (the FEMU zns model
        schedules ops at plane granularity)."""
        return self.n_channels * self.n_luns * self.n_planes

    # Controller
    n_cores: int = 4
    core_ghz: float = 2.0
    sha_hw: bool = False        # SHA-256 offloaded to a hardware engine
                                # (2 us per 4 KB page per the unified spec)
    lz4_hw: bool = False        # compression on a dedicated engine
                                # (Balloon-ZNS DAC'24 assumption: "a few us
                                # per page, not a bottleneck"; 1.5 us/page
                                # ~= 2.7 GB/s pipelined ASIC)

    # Sensitivity multipliers (Sec 4.6)
    nand_speedup: float = 1.0    # 1.5 = NAND 50% faster, 0.667 = 50% slower
    cpu_speedup: float = 1.0

    # LiZoneDedup metadata layout
    fp_bytes: int = 12           # truncated SHA-256
    oid_bytes: int = 8
    hint_bytes: int = 2          # per-entry reference hint
    ins_bytes: int = 22          # INS identity record
    dref_bytes: int = 16         # DREF compound descriptor record
    uref_bytes: int = 16
    lsh_bits: int = 24           # SimHash route digest width
    dram_cache_mb: float = 0.0   # global-index DRAM cache cap (0 = uncached)
    route_table_bits: int = 16   # high bits of L used for the route table
    bloom_fp_rate: float = 1e-3  # active-zone insertion-only Bloom
    xor_fp_rate: float = 0.004   # sealed-zone Xor filter
    xor_bits_per_key: float = 9.84

    # DRAM caches (steady-state budget; grows with active zones, not capacity)
    wi_entries_per_zone: int = 8192     # working index (recent fingerprints)
    delta_cap_bytes_per_zone: int = 256 * 1024
    shard_cache_bytes: int = 32 * 1024 * 1024
    filter_cache_bytes: int = 16 * 1024 * 1024
    route_entry_bytes: int = 3          # 2 B zone id + 1 B flags

    @property
    def pages_per_zone(self) -> int:
        return (self.zone_size_mb * 1024 * 1024) // self.page_size_b

    @property
    def num_zones(self) -> int:
        return (self.capacity_gb * 1024) // self.zone_size_mb

    @property
    def dram_cache_bytes(self) -> int:
        return int(self.dram_cache_mb * 2**20)

    @property
    def index_entry_bytes(self) -> int:
        return self.fp_bytes + self.oid_bytes + self.hint_bytes

    @property
    def t_read(self) -> float:
        return self.t_read_us / self.nand_speedup

    @property
    def t_prog(self) -> float:
        return self.t_prog_us / self.nand_speedup

    @property
    def t_erase(self) -> float:
        return self.t_erase_us / self.nand_speedup

    # ---- compute costs (us per op), scaled by cpu_speedup -------------
    # Hardware acceleration covers SHA-256 and LZ4 ONLY; SimHash exists as
    # software alone (4 us).  Fingerprint totals: hw 2+4=6 us, sw 8+4=12 us.
    @property
    def c_sha(self) -> float:      # SHA-256 over one 4 KB page
        if self.sha_hw:            # dedicated engine, not a core
            return 2.0              # unified spec: 2 us per 4 KB page
        return 8.0 / self.cpu_speedup

    @property
    def c_simhash(self) -> float:  # SimHash digest over one 4 KB page
        return 4.0 / self.cpu_speedup   # software-only, no hw variant

    @property
    def c_lz4_c(self) -> float:    # LZ4 compress one 4 KB page
        if self.lz4_hw:            # dedicated engine, not a core
            return 1.5
        return 8.0 / self.cpu_speedup

    @property
    def c_lz4_d(self) -> float:    # LZ4 decompress one 4 KB page
        if self.lz4_hw:
            return 1.0
        return 2.0 / self.cpu_speedup

    @property
    def c_route(self) -> float:    # route-table lookup (DRAM)
        return 0.10 / self.cpu_speedup

    @property
    def c_filter(self) -> float:   # membership filter probe (DRAM)
        return 0.15 / self.cpu_speedup

    @property
    def c_scan(self) -> float:     # exact scan of one DRAM-cached shard page
        return 0.25 / self.cpu_speedup

    @property
    def c_dref(self) -> float:     # DREF commit to PLP DRAM buffer
        return 0.05 / self.cpu_speedup

    @property
    def c_status(self) -> float:   # per-slot status lookup (read path)
        return 0.05 / self.cpu_speedup

    @property
    def c_dparse(self) -> float:   # descriptor parse (read path)
        return 0.10 / self.cpu_speedup

    @property
    def seq_write_bw_gbps(self) -> float:
        return self.n_channels * self.page_size_b / self.t_prog / 1e3


DEFAULT_DEVICE = DeviceConfig()


def bloom_bits(n: int, fp_rate: float = 1e-3) -> int:
    """Optimal Bloom filter size in bits for n keys at target FP rate."""
    if n <= 0:
        return 64
    m = -n * math.log(fp_rate) / (math.log(2) ** 2)
    return int(max(64, m))


def xor_filter_bytes(n: int, bits_per_key: float = 9.84) -> int:
    if n <= 0:
        return 64
    return int(math.ceil(n * bits_per_key / 8))
