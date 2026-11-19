"""run_v10b.py -- bypass ablation for RocksDB (review C1b): the default
bypass threshold (5% hit rate, 2 windows) never triggers at RocksDB's
~11.5% duplication.  Measure: threshold 15% / forced-bypass vs default,
quantifying the p99/BW recovery that adaptive bypass can deliver on
low-dup loads."""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lzsim.optimizations            # noqa: F401,E402
from lzsim.traces import Trace        # noqa: E402
from lzsim.timing import TimingSim    # noqa: E402
from lzsim.device import DeviceConfig  # noqa: E402

RESULTS = os.path.join(os.path.dirname(__file__), "..", "results_v10")
V7F = dict(skew=True, precopy=True, hot_reads=16, dref_prefetch=True,
           redirect_cap=2048, stage1_prefetch=True, zone_locks=True)
DEV = lambda **kw: DeviceConfig(wi_entries_per_zone=128, **kw)


class LiZoneBypass15(sys.modules['lzsim.optimizations'].LiZoneBypass):
    name = "LiZoneDedup-Bypass15"
    OFF_TH = 0.15      # trigger bypass at <=15% hit rate


sys.modules['lzsim.systems'].SYSTEMS['v9-bypass15'] = LiZoneBypass15
sys.modules['lzsim.systems'].SYSTEMS['v9-bypass15'] = LiZoneBypass15

rows = []
tr = Trace('rocksdb')
for sk, label in (('nodedup', 'nodedup'), ('v9', 'v9-default'),
                  ('lizone-bypass', 'v9-bypass5(default)'),
                  ('v9-bypass15', 'v9-bypass15'),
                  ('hostdedup', 'hostdedup-1g')):
    flags = V7F if sk.startswith('v9') else dict(skew=True)
    r = TimingSim(tr, sk, dev=DEV(sha_hw=True), qd=32, read_mix_extra=0.30,
                  max_events=min(len(tr), 3_000_000), **flags).run()
    fn = r['func']
    sysobj = r
    rows.append({
        'label': label, 'system': fn['system'],
        'hit_rate': fn['hit_rate'], 'dedup': fn['dedup_ratio'],
        'wbw': r['write_logi_bw_mbs'], 'wphys': r['write_phys_bw_mbs'],
        'wp50': r['w_p50'], 'wp99': r['w_p99'], 'rp99': r['r_p99'],
        'bypass_frac': fn.get('bypass_frac', 0),
        'n_writes': fn['writes'],
    })
    print(f"[10b] {label}: wBW={r['write_logi_bw_mbs']:.0f} "
          f"wp99={r['w_p99']:.0f} hit={fn['hit_rate']:.3f} "
          f"bypass={rows[-1]['bypass_frac']:.3f}", flush=True)

out = os.path.join(RESULTS, 'e4b_bypass.csv')
with open(out, 'w', newline='') as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)
print(f'saved e4b_bypass.csv ({len(rows)} rows)', flush=True)
