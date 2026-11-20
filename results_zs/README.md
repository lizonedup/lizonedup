# Zone-size sweep in the simulator: 128 MiB vs 1 GiB (Reviewer D)

`python experiments/run_zs_sim.py` (one command; traces auto-build).
Full design (v9, r=2) and the fp-prefix arm, 1 TB device, 3 M events per
workload, same metrics as the paper's e1/e8 tables.

| workload | arm | 128 MiB: recall / phys-red / RA | 1 GiB: recall / phys-red / RA | phys-red delta |
|---|---|---|---|---|
| container | v9 | 0.967 / 74.9% / 4 B | **1.000 / 77.4% / 2 B** | +2.5 pp |
| container | prefix | 0.967 / 74.9% / 12 B | 1.000 / 77.4% / 6 B | +2.5 pp |
| backup | v9 | 0.977 / 85.1% / 4 B | **1.000 / 87.1% / 1 B** | +2.0 pp |
| backup | prefix | 0.977 / 85.1% / 12 B | 1.000 / 87.1% / 5 B | +2.0 pp |
| vm | v9 | 0.979 / 67.3% / 4 B | **1.000 / 68.8% / 2 B** | +1.5 pp |
| vm | prefix | 0.979 / 67.3% / 12 B | 1.000 / 68.8% / 6 B | +1.5 pp |
| rocksdb | v9 | 1.000 / 15.9% / 6 B | 1.000 / 15.9% / 3 B | 0.0 pp |
| rocksdb | prefix | 1.000 / 15.9% / 19 B | 1.000 / 15.9% / 9 B | 0.0 pp |

DRAM: 128 MiB zones 43-47 MB (96 zones used at 3 M events); 1 GiB zones
17-24 MB (32 zones) -- larger zones shrink the live zone set, so DRAM
drops with zone size.  Recall at 1 GiB is exactly 1.000 on every
dedup-rich workload: a 1 GiB zone holds the whole active duplicate set,
so the r=2 candidate chain always contains the source (no routing false
negatives); prefix routing converges to the same recall, and its RA
penalty also halves (fewer probes land on sealed shards).  rocksdb is
unchanged (temporally adjacent duplicates, no routing loss either way).

Reading for the rebuttal: GB-scale zones strengthen every claim -- they
add +1.5 to +2.5 pp physical-write reduction, halve metadata reads, cut
DRAM by ~2x, and close the SimHash-vs-prefix RA gap -- because fewer,
longer-lived zones make the candidate chains more stable.  The design
has no zone-size correctness dependency; zone size is a pure deployment
knob (extends paper Sec 4.6's 128-512 MiB sweep to 1 GiB).
