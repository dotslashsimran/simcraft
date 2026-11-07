# Benchmark report

**Verdict:** No clear difference between copysort and sorted: time ratio 1.095, 95% CI 0.846 to 1.320 includes 1.0 or the test disagrees (p=0.84). Do not claim a speedup. More runs or a quieter machine may narrow it.

**Verdict:** sorted-x4 is 1.34x slower than sorted (95% CI 1.08x to 1.79x, Mann-Whitney p=0.0014).

## Warnings

- environment: 1-minute load average is 16.1 on 8 logical cores. Other work is competing for the CPU; close it or expect wide spread.
- environment: 8 other processes are using 5% or more CPU right now.
- environment: Running on battery. Laptops often lower clock speeds on battery; plug in.
- sorted: times fall steadily during the run (Spearman rho=-0.53). Something drifted (thermal throttling, caches, background work). Interleaving keeps the comparison fair, but the absolute numbers are suspect.
- sorted: the first 10 measured runs are 293% slower than the last 10. Either warmup was not enough (raise --warmup) or background load dropped during the run.
- sorted: robust spread is 20% of the median. The machine is noisy or the work is bimodal; look at the chart before trusting small differences.
- sorted: 10 run(s) were more than 5 MADs from the median. They are kept (they may be real pauses such as GC); medians are not moved much by them.
- copysort: times fall steadily during the run (Spearman rho=-0.61). Something drifted (thermal throttling, caches, background work). Interleaving keeps the comparison fair, but the absolute numbers are suspect.
- copysort: the first 10 measured runs are 303% slower than the last 10. Either warmup was not enough (raise --warmup) or background load dropped during the run.
- copysort: robust spread is 30% of the median. The machine is noisy or the work is bimodal; look at the chart before trusting small differences.
- copysort: 7 run(s) were more than 5 MADs from the median. They are kept (they may be real pauses such as GC); medians are not moved much by them.
- sorted-x4: times fall steadily during the run (Spearman rho=-0.56). Something drifted (thermal throttling, caches, background work). Interleaving keeps the comparison fair, but the absolute numbers are suspect.
- sorted-x4: the first 10 measured runs are 258% slower than the last 10. Either warmup was not enough (raise --warmup) or background load dropped during the run.
- sorted-x4: robust spread is 28% of the median. The machine is noisy or the work is bimodal; look at the chart before trusting small differences.
- sorted-x4: 8 run(s) were more than 5 MADs from the median. They are kept (they may be real pauses such as GC); medians are not moved much by them.

## Results (wall time)

| Command | Median | 95% CI of median | p5 | p95 | Min | Max | MAD | CPU time (median) | Peak RSS (median) |
|---|---|---|---|---|---|---|---|---|---|
| `sorted` | 225.57 ms | 214.60 ms to 272.24 ms | 181.03 ms | 1.176 s | 172.68 ms | 2.050 s | 44.47 ms | 208.24 ms | 31.9 MiB |
| `copysort` | 246.92 ms | 211.42 ms to 298.01 ms | 190.12 ms | 1.028 s | 172.45 ms | 1.673 s | 75.09 ms | 213.79 ms | 31.6 MiB |
| `sorted-x4` | 301.64 ms | 276.71 ms to 406.38 ms | 225.64 ms | 1.366 s | 213.52 ms | 1.458 s | 85.01 ms | 264.07 ms | 34.1 MiB |

## Comparisons

| Candidate vs baseline | Time ratio | CI | Mann-Whitney p | Cliff's delta | P(candidate faster) | Floor subtracted |
|---|---|---|---|---|---|---|
| copysort vs sorted | 1.095 | 0.846 to 1.320 | 0.844 | 0.03 | 49% | no |
| sorted-x4 vs sorted | 1.337 | 1.082 to 1.793 | 0.00138 | 0.42 | 29% | no |

Time ratio = candidate median / baseline median. Below 1.0 means the candidate is faster. CI is a percentile bootstrap (5000 resamples).

## Commands

- `sorted`: `python3 two_sorts.py sorted`
- `copysort`: `python3 two_sorts.py copysort`
- `sorted-x4`: `python3 two_sorts.py sorted-x4`

## Method

5 warmup and 40 measured runs per command, order: shuffled, seed 12345.

## Reproduce

```
python3 bench.py -n sorted -n copysort -n sorted-x4 'python3 two_sorts.py sorted' 'python3 two_sorts.py copysort' 'python3 two_sorts.py sorted-x4' --same-output --runs 40 --warmup 5 --redact --json two_sorts.json --md two_sorts.md --chart two_sorts.svg
```

## Environment

| Setting | Value |
|---|---|
| Captured | 2026-11-08 00:00:49 IST |
| Host / user | <redacted> / <redacted> |
| OS | macOS 15.7.5 (kernel 24.6.0) |
| CPU | Apple M2, arm64 |
| Cores | 8 physical, 8 logical (4 Performance, 4 Efficiency) |
| Memory | 8.0 GB total, 41% free |
| Power | source: battery, low power mode: False, thermal: no warning recorded |
| Load average (1/5/15 min) | 16.06 / 17.5 / 15.13 |
| Python | CPython 3.9.6 |

**Environment warnings**

- 1-minute load average is 16.1 on 8 logical cores. Other work is competing for the CPU; close it or expect wide spread.
- 8 other processes are using 5% or more CPU right now.
- Running on battery. Laptops often lower clock speeds on battery; plug in.

**Not controlled on this OS**

- macOS has no supported way to pin a process to a core or to disable turbo/boost.
- Apple Silicon schedules work on performance or efficiency cores as it sees fit; a run that lands on efficiency cores is much slower. Keep other load low so the benchmark stays on performance cores.
- Clearing the file cache needs 'sudo purge'. Without it, use F_NOCACHE reads or a fresh file to measure cold reads.
