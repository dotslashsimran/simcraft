# Benchmark report

**Verdict:** NOT A VALID COMPARISON: map-unused is no slower than the empty floor command, so its work probably never ran. (The raw numbers said: map-unused vs listcomp: no stable ratio after subtracting the floor, because one of them is not reliably slower than the floor command.)

**Verdict:** No clear difference between map-used and listcomp: time ratio 0.841, 95% CI 0.336 to 1.494 includes 1.0 or the test disagrees (p=0.044). Do not claim a speedup. More runs or a quieter machine may narrow it.

## Warnings

- environment: 1-minute load average is 11.4 on 8 logical cores. Other work is competing for the CPU; close it or expect wide spread.
- environment: 8 other processes are using 5% or more CPU right now.
- environment: Running on battery. Laptops often lower clock speeds on battery; plug in.
- listcomp: robust spread is 15% of the median. The machine is noisy or the work is bimodal; look at the chart before trusting small differences.
- listcomp: 7 run(s) were more than 5 MADs from the median. They are kept (they may be real pauses such as GC); medians are not moved much by them.
- map-unused: times fall steadily during the run (Spearman rho=-0.60). Something drifted (thermal throttling, caches, background work). Interleaving keeps the comparison fair, but the absolute numbers are suspect.
- map-unused: 3 run(s) were more than 5 MADs from the median. They are kept (they may be real pauses such as GC); medians are not moved much by them.
- map-unused: median is within noise of the floor command (150.79 ms vs 154.78 ms). The work you meant to measure may not be running at all: check for unused results, lazy evaluation, dead code elimination or constant folding.
- map-used: robust spread is 17% of the median. The machine is noisy or the work is bimodal; look at the chart before trusting small differences.
- map-used: 3 run(s) were more than 5 MADs from the median. They are kept (they may be real pauses such as GC); medians are not moved much by them.
- Commands printed different output (1fa46517d9a5: listcomp, map-used; aa0de22f00f8: map-unused). They may not be doing the same work, so their times are not comparable.

## Results (wall time)

| Command | Median | 95% CI of median | p5 | p95 | Min | Max | MAD | CPU time (median) | Peak RSS (median) |
|---|---|---|---|---|---|---|---|---|---|
| `listcomp` | 260.79 ms | 252.64 ms to 407.38 ms | 234.02 ms | 990.04 ms | 227.15 ms | 1.081 s | 38.03 ms | 244.65 ms | 161.5 MiB |
| `map-unused` | 150.79 ms | 144.44 ms to 189.86 ms | 140.95 ms | 246.12 ms | 134.83 ms | 277.13 ms | 14.86 ms | 142.14 ms | 84.8 MiB |
| `map-used` | 243.98 ms | 226.79 ms to 322.18 ms | 216.73 ms | 505.58 ms | 214.58 ms | 614.38 ms | 40.99 ms | 223.82 ms | 161.3 MiB |
| floor: `python3 lazy_work.py empty` | 154.78 ms | 146.08 ms to 180.32 ms | 135.63 ms | 238.12 ms | 131.82 ms | 285.17 ms | 23.46 ms | | |

## Comparisons

| Candidate vs baseline | Time ratio | CI | Mann-Whitney p | Cliff's delta | P(candidate faster) | Floor subtracted |
|---|---|---|---|---|---|---|
| map-unused vs listcomp | nan | nan to nan | 1.84e-08 | -0.93 | 96% | yes |
| map-used vs listcomp | 0.841 | 0.336 to 1.494 | 0.0436 | -0.33 | 67% | yes |

Time ratio = candidate median / baseline median. Below 1.0 means the candidate is faster. CI is a percentile bootstrap (5000 resamples).

## Commands

- `listcomp`: `python3 lazy_work.py listcomp`
- `map-unused`: `python3 lazy_work.py map-unused`
- `map-used`: `python3 lazy_work.py map-used`

## Method

3 warmup and 25 measured runs per command, order: shuffled, seed 12345.

## Reproduce

```
python3 bench.py -n listcomp -n map-unused -n map-used 'python3 lazy_work.py listcomp' 'python3 lazy_work.py map-unused' 'python3 lazy_work.py map-used' --floor 'python3 lazy_work.py empty' --subtract-floor --same-output --runs 25 --warmup 3 --redact --json lazy_work.json --md lazy_work.md --chart lazy_work.svg
```

## Environment

| Setting | Value |
|---|---|
| Captured | 2026-11-07 21:30:25 IST |
| Host / user | <redacted> / <redacted> |
| OS | macOS 15.7.5 (kernel 24.6.0) |
| CPU | Apple M2, arm64 |
| Cores | 8 physical, 8 logical (4 Performance, 4 Efficiency) |
| Memory | 8.0 GB total, 26% free |
| Power | source: battery, low power mode: False, thermal: no warning recorded |
| Load average (1/5/15 min) | 11.39 / 16.97 / 13.13 |
| Python | CPython 3.9.6 |

**Environment warnings**

- 1-minute load average is 11.4 on 8 logical cores. Other work is competing for the CPU; close it or expect wide spread.
- 8 other processes are using 5% or more CPU right now.
- Running on battery. Laptops often lower clock speeds on battery; plug in.

**Not controlled on this OS**

- macOS has no supported way to pin a process to a core or to disable turbo/boost.
- Apple Silicon schedules work on performance or efficiency cores as it sees fit; a run that lands on efficiency cores is much slower. Keep other load low so the benchmark stays on performance cores.
- Clearing the file cache needs 'sudo purge'. Without it, use F_NOCACHE reads or a fresh file to measure cold reads.
