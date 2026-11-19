"""
replay.py -- functional trace replay driver.

Runs a system over a materialised trace and returns a metrics dict.
Per-request latency recipes can optionally be sampled for the timing model.
"""

from __future__ import annotations

import time
from typing import Optional

from .systems import make_system, BaseSystem
from .traces import Trace, fp_ctype, CTYPE_NAMES, fp_crc
from .device import DeviceConfig, DEFAULT_DEVICE


def replay(trace: Trace, system_key: str, dev: DeviceConfig = DEFAULT_DEVICE,
           track_locus: bool = False, max_events: Optional[int] = None,
           sample_rate: float = 0.0, progress: bool = False):
    sys = make_system(system_key, dev, track_locus=track_locus)
    n = len(trace) if max_events is None else min(max_events, len(trace))
    t0 = time.time()
    samples = []
    by_type_hits = {}
    by_type_total = {}
    for i in range(n):
        op, fp, L = trace.event(i)
        if op != 0:
            continue
        out = sys.write(fp, L)
        if sample_rate:
            import random
            # deterministic thin sampling
            if (i * 2654435761) % 1000 < sample_rate * 1000:
                samples.append((out.hit, out.md_read_pages, out.data_pages,
                                out.compute_us, out.sealed_source))
        t = fp_ctype(fp)
        by_type_total[t] = by_type_total.get(t, 0) + 1
        if out.hit:
            by_type_hits[t] = by_type_hits.get(t, 0) + 1
    elapsed = time.time() - t0
    res = sys.summary()
    res["trace"] = trace.name
    res["events"] = sys.n_w
    res["replay_sec"] = elapsed
    res["by_type_hits"] = {CTYPE_NAMES[k]: v for k, v in by_type_hits.items()}
    res["by_type_total"] = {CTYPE_NAMES[k]: v for k, v in by_type_total.items()}
    if samples:
        res["samples"] = samples
    if hasattr(sys, "host_compute_us"):
        res["host_compute_us_total"] = sys.host_compute_us
        res["host_dram_gb_1tb"] = sys.host_dram_bytes_1tb() / 1e9
    return res


def replay_all(trace_names, system_keys, dev=DEFAULT_DEVICE, **kw):
    out = []
    for tn in trace_names:
        tr = Trace(tn)
        for sk in system_keys:
            r = replay(tr, sk, dev, **kw)
            out.append(r)
            print(f"[replay] {tn}/{sk}: hits={r['hits']} "
                  f"dedup={r['dedup_ratio']:.2f}x "
                  f"RA={r['md_read_bytes_per_req']:.0f}B/req "
                  f"WAmd={r['md_write_bytes_per_data_byte']:.4f} "
                  f"({r['replay_sec']:.1f}s)")
    return out
