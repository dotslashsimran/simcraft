# Benchmark report

**Verdict:** No clear difference between buf1m and buf64k: time ratio 0.837, 95% CI 0.451 to 1.622 includes 1.0 or the test disagrees (p=0.83). Do not claim a speedup. More runs or a quieter machine may narrow it.

## Warnings

- environment: 1-minute load average is 9.0 on 8 logical cores. Other work is competing for the CPU; close it or expect wide spread.
- environment: 8 other processes are using 5% or more CPU right now.
- environment: Running on battery. Laptops often lower clock speeds on battery; plug in.
- buf64k: times rise steadily during the run (Spearman rho=0.69). Something drifted (thermal throttling, caches, background work). Interleaving keeps the comparison fair, but the absolute numbers are suspect.
- buf64k: robust spread is 76% of the median. The machine is noisy or the work is bimodal; look at the chart before trusting small differences.
- buf64k: 2 run(s) were more than 5 MADs from the median. They are kept (they may be real pauses such as GC); medians are not moved much by them.
- buf1m: times rise steadily during the run (Spearman rho=0.74). Something drifted (thermal throttling, caches, background work). Interleaving keeps the comparison fair, but the absolute numbers are suspect.
- buf1m: robust spread is 58% of the median. The machine is noisy or the work is bimodal; look at the chart before trusting small differences.
- buf1m: 2 run(s) were more than 5 MADs from the median. They are kept (they may be real pauses such as GC); medians are not moved much by them.

## Results (wall time)

| Command | Median | 95% CI of median | p5 | p95 | Min | Max | MAD | CPU time (median) | Peak RSS (median) |
|---|---|---|---|---|---|---|---|---|---|
| `buf64k` | 197.01 ms | 135.95 ms to 325.89 ms | 89.86 ms | 1.084 s | 72.73 ms | 1.504 s | 150.14 ms | 97.18 ms | 10.1 MiB |
| `buf1m` | 164.81 ms | 126.61 ms to 273.52 ms | 98.69 ms | 645.78 ms | 96.29 ms | 1.302 s | 95.26 ms | 98.03 ms | 25.0 MiB |

## Comparisons

| Candidate vs baseline | Time ratio | CI | Mann-Whitney p | Cliff's delta | P(candidate faster) | Floor subtracted |
|---|---|---|---|---|---|---|
| buf1m vs buf64k | 0.837 | 0.451 to 1.622 | 0.83 | -0.03 | 52% | no |

Time ratio = candidate median / baseline median. Below 1.0 means the candidate is faster. CI is a percentile bootstrap (5000 resamples).

## Commands

- `buf64k`: `python3 file_read.py read data.bin 65536`
- `buf1m`: `python3 file_read.py read data.bin 1048576`

## Method

3 warmup and 30 measured runs per command, order: shuffled, seed 12345.

## Reproduce

```
python3 bench.py -n buf64k -n buf1m 'python3 file_read.py read data.bin 65536' 'python3 file_read.py read data.bin 1048576' --same-output --runs 30 --warmup 3 --redact --json file_read.json --md file_read.md --chart file_read.svg
```

## Environment

| Setting | Value |
|---|---|
| Captured | 2026-11-07 23:59:16 IST |
| Host / user | <redacted> / <redacted> |
| OS | macOS 15.7.5 (kernel 24.6.0) |
| CPU | Apple M2, arm64 |
| Cores | 8 physical, 8 logical (4 Performance, 4 Efficiency) |
| Memory | 8.0 GB total, 39% free |
| Power | source: battery, low power mode: False, thermal: no warning recorded |
| Load average (1/5/15 min) | 8.98 / 16.56 / 14.54 |
| Python | CPython 3.9.6 |

**Environment warnings**

- 1-minute load average is 9.0 on 8 logical cores. Other work is competing for the CPU; close it or expect wide spread.
- 8 other processes are using 5% or more CPU right now.
- Running on battery. Laptops often lower clock speeds on battery; plug in.

**Not controlled on this OS**

- macOS has no supported way to pin a process to a core or to disable turbo/boost.
- Apple Silicon schedules work on performance or efficiency cores as it sees fit; a run that lands on efficiency cores is much slower. Keep other load low so the benchmark stays on performance cores.
- Clearing the file cache needs 'sudo purge'. Without it, use F_NOCACHE reads or a fresh file to measure cold reads.
