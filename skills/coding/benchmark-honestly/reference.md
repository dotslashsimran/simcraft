# Benchmark honestly: reference

This file holds the detail behind `SKILL.md`. Read the section you need.

## 1. Defining the question

**Throughput or latency.** Throughput is how much work finishes per second when the system is kept busy. Latency is how long one piece of work takes from the point of view of whoever asked for it. They trade off: batching raises throughput and raises latency. Say which one the change is meant to improve.

**Which statistic.**
- The **median** (p50) describes a typical run and is not moved by a few outliers. Use it for comparing two versions of a batch job or a function.
- **p90, p99, p99.9** describe the tail. For anything a person waits on, the tail is what they remember. A page that loads 100 resources hits each service's p99 on most loads.
- The **mean** is right only when total cost matters (for example CPU-hours billed) and should then be reported with the tail, because one 2-second GC pause in 1,000 runs moves the mean and nothing else tells you why.
- The **minimum** is useful in microbenchmarks as "the time when nothing interfered", but it hides real costs such as GC and cache misses. Never use it as the only number.

**Cold or warm.** Cold means the first execution: caches empty, files not in the page cache, JIT not compiled, connection pools empty, lazy imports not loaded. Warm means steady state. A CLI tool that runs once per invocation should be measured cold or at least per-process; a server should be measured warm and separately for its first requests after a deploy.

**Realistic workload.** Use input sizes and distributions from production when you can. Data that fits in L1 (tens of KB) or L2 (around 1 MB) runs several times faster per element than data that has to come from main memory. Sorted, repeated or all-equal inputs make branch predictors and caches look perfect. Run at least two sizes and check that the conclusion holds at both.

**What difference matters.** Decide the smallest difference worth acting on before you see the results. Otherwise any statistically detectable difference, even 0.3%, starts to look important. bench.py takes this as `--min-effect` and says "the same speed within +/-2%" when the CI sits inside that band.

## 2. Environment control and disclosure

Things that change results by more than most optimisations do:

| Factor | Effect | What to do |
|---|---|---|
| Other load (browsers, builds, indexing, other agents) | Wide spread, random slow runs, drift | Close it, check load average, use `env_report.py` |
| Battery and low power mode | Lower clocks, sometimes 30% or more | Plug in, turn low power mode off |
| Turbo/boost | Clock depends on temperature and how many cores are busy | Linux: disable (`/sys/devices/system/cpu/intel_pstate/no_turbo` or `cpufreq/boost`). macOS: cannot, so disclose |
| Thermal throttling | Later runs slower than early ones | Cool-down pauses, shorter runs, watch for drift warnings |
| CPU governor (Linux) | `powersave`/`schedutil` ramp clocks slowly | `cpupower frequency-set -g performance` |
| Core type (Apple Silicon, Intel hybrid) | Efficiency cores are 2-4x slower | Keep load low; on Linux pin with `taskset -c` to a performance core |
| SMT/hyperthreading | A sibling thread shares the core | Pin away from siblings or disable SMT for microbenchmarks |
| Memory pressure | Swapping | Check free memory |
| VMs, containers, CI runners | Noisy neighbours, CPU steal | Run many more repeats, never compare across machines |
| ASLR and memory layout | Small changes in alignment move results by a few percent | Run several processes, not one long run (bench.py does) |
| Versions | Interpreter, compiler, flags, library versions | Record them; `env_report.py --tool "node --version"` |
| Build type | Debug builds can be 10-100x slower and slow in different places | `-O2`, `cargo --release`, `NODE_ENV=production`, no profilers or coverage attached |

On **macOS** you cannot pin a process to a core, disable turbo, change the governor or drop the file cache without `sudo purge`. Say this in the report. The honest workaround is a quiet machine on AC power, interleaved runs, and more repeats.

## 3. Warmup, JIT and tiered compilation

Runtimes with JITs (JVM, V8, PyPy, .NET, LuaJIT) start by interpreting, then compile hot code, sometimes in several tiers, and may later deoptimise. The first hundreds or thousands of calls are slower and noisy. In-process tools (JMH, criterion, mitata, pyperf) handle this by running until timings settle. For per-process timing, warmup runs fill the OS page cache, CPU caches and branch predictors, and load shared libraries.

bench.py checks whether warmup was enough: it compares the first quarter of measured runs with the last quarter and warns if the first is clearly slower. It also warns when time drifts across the whole run (Spearman correlation between run order and time), which usually means thermal throttling or changing background load.

## 4. How many repeats

Enough that the confidence interval is narrower than the difference you care about. As a start: 20 to 30 runs per variant for process-level timing on a quiet machine, more on a noisy one or in CI. If the CI is too wide, the fix is more runs or a quieter machine, not a different statistic. For tail percentiles you need at least 10 samples beyond the percentile: about 1,000 samples for p99 and 10,000 for p99.9. latency_test.py warns when the sample is too small.

## 5. Statistics

**Why not means and standard deviations.** Timing data is skewed (it cannot go below the true cost, but it can be much higher) and often has several clusters. The mean is pulled by a few slow runs, and "mean +/- 2 SD" can include negative times. Use medians, percentiles and the median absolute deviation (MAD).

**Bootstrap confidence intervals.** Resample the runs with replacement thousands of times, compute the statistic each time, and take the 2.5th and 97.5th percentiles of those values. It makes no assumption about the shape of the data. bench.py reports the CI of each median and the CI of the ratio of medians between each candidate and the baseline.

**Comparing two versions.**
- **Ratio of medians with a bootstrap CI.** This is the headline: "new/old = 0.85, 95% CI 0.80 to 0.91". If the CI includes 1.0, there is no clear difference.
- **Mann-Whitney U test.** Asks whether one set of times tends to be larger than the other, using ranks, so outliers and skew do not break it. bench.py uses the normal approximation with tie and continuity correction, which is fine from about 8 runs per side. A p-value only says a difference is unlikely to be noise; it says nothing about size.
- **Effect size.** Cliff's delta (from -1 to +1) and its plain form, "the candidate was faster in 80% of pairs of runs". This tells a reader how often the improvement shows up, which a median ratio hides.
- **Interleaving.** Running all of A and then all of B means any drift (thermal, background job finishing, cache state) becomes part of the "difference". Shuffled rounds spread drift across both. bench.py does this by default; `--order sequential` exists only to show why it is wrong.

**Outliers.** Do not delete slow runs unless you know they are not part of the real cost. A GC pause, a page fault or a lock wait is part of what users see. Medians already resist them; report them and look at the chart.

**Subtracting startup.** When each run starts a process, the ratio of raw times understates the change in the work itself. `--floor` measures an empty version (same interpreter, imports and setup, no work), and `--subtract-floor` compares (time minus floor) instead. The floor is resampled in the bootstrap, so its noise widens the CI honestly.

## 6. The lies

| Lie | What happens | How to catch it |
|---|---|---|
| **Dead code elimination** | The compiler sees the result is unused and removes the work. Common in C, C++, Rust, Go, Java and JS JITs. | Use the result: `black_box` (Rust), `benchmark::DoNotOptimize` (C++), assign to a package-level sink (Go), return it (JMH Blackhole). Compare against a floor. |
| **Lazy evaluation** | `map()`, generators, iterators, LINQ, Java streams, lazy ORMs build a plan and do nothing until consumed. | Force it: `list(...)`, `.collect()`, `.toList()`, `len(...)`. Print a checksum and use `--same-output`. |
| **Constant folding** | `2 ** 64` or `"a" * 3` is computed at compile time, so the loop measures loading a constant. | Read inputs from variables or arguments; `dis` (Python) or the compiler explorer shows the folded code. |
| **Warm caches from the previous run** | Whichever variant runs first pays for loading files, filling CPU caches or the JIT, and the next one looks faster. | Warmup runs, shuffled order, and say which state (warm or cold) you measured. |
| **Measuring the timer or startup** | For work under a few microseconds, the clock call costs more than the work. For process timing, interpreter startup can be 20-100 ms. | Batch many iterations per timing, use in-process tools for small work, compare to the floor. |
| **Coordinated omission** | A load generator that waits for each reply before sending the next stops sending while the server stalls, so it records one slow request instead of hundreds. p99 looks fine while users wait. | Open-loop load at a fixed rate with latency measured from the intended send time (`latency_test.py`, `wrk2`, `vegeta`, `k6` with constant-arrival-rate). |
| **GC pauses hidden by averaging** | A 300 ms pause every few seconds barely moves the mean of thousands of 2 ms requests. | Report p99, p99.9 and max; look at the time series, not one number. |
| **Unequal work** | Variants handle different input, skip validation, return early on an error, or produce a different result. A failing fast path is not a fast success. | Same input for every variant, compare outputs, check error counts. |
| **Debug builds** | Debug code is slow in different places than release code, so it also ranks variants wrongly. | Benchmark the build you ship, with the same flags. |
| **OS file cache** | The second read of a file comes from RAM, not disk, and can be several times faster. | Decide which you mean. Cold on Linux: `posix_fadvise(DONTNEED)` per file or drop caches as `--prepare`. Cold on macOS: `sudo purge`. Otherwise say "warm cache". |
| **Tiny inputs that fit in L1** | Everything is in the fastest cache, so the algorithm with more memory traffic looks free. | Test at realistic sizes, at least one larger than the last-level cache. |
| **Branch predictor training** | Repeating the same input lets the CPU learn every branch. | Randomise inputs, use several. |
| **Frequency and thermal effects** | One variant runs while the CPU is cool and boosting, the other after it has throttled. | Interleave; watch drift warnings; shorter runs with pauses. |
| **Cherry-picking** | Running until a good number shows up, or picking the best of several sessions. | Fix the number of runs before starting, report every run, publish the command. |

## 7. Microbenchmarks vs end to end

A microbenchmark isolates one function to find out which of two implementations is faster under controlled conditions. It cannot tell you whether the change matters. For that, measure the real operation end to end (a request, a job, a page load) and use a profiler to see what share of the time the function takes. If it is 5% of the total, making it twice as fast saves at most 2.5%. Report the micro result and the end-to-end result together, and do not let the first stand in for the second.

## 8. Reporting

A report someone can trust contains:
1. **The question** (what was measured, which statistic, cold or warm, which workload).
2. **A chart showing every run**, such as a dot plot with the median and middle half marked. A bar chart of means hides the spread that tells the reader whether the difference is real. bench.py writes an SVG with no dependencies, or a PNG if matplotlib is installed.
3. **The setup table** from `env_report.py`, with any warnings and what could not be controlled.
4. **The exact command to reproduce**, including seeds, input sizes and versions.
5. **One honest line** with the CI and the conditions, for example: "copysort and sorted show no clear difference (time ratio 1.10, 95% CI 0.85 to 1.32, 40 runs each, busy laptop on battery)."

Wording rules: say "faster" only when the CI excludes 1.0. Give speedups as ratios with their CI ("1.18x, 95% CI 1.09x to 1.27x"), not as "18% faster" alone. State the conditions in the same sentence. If the machine was noisy, say so; do not drop the warning to make the result look cleaner.

## 9. Tool notes

- **hyperfine**: `hyperfine -N --warmup 3 --runs 30 --export-json out.json 'a' 'b'`. `-N` skips the shell for commands without pipes. `--prepare` works like bench.py's. It reports mean +/- SD and a relative speed; for a CI, feed the same commands to bench.py, or compute one from its JSON.
- **pyperf**: `python3 -m pyperf timeit -s "setup" "stmt" -o new.json`, then `python3 -m pyperf compare_to old.json new.json --table`. It spawns worker processes, calibrates loops and warns about unstable systems (`python3 -m pyperf system tune` on Linux).
- **pytest-benchmark**: `pytest --benchmark-only --benchmark-autosave`, then `--benchmark-compare`. Use `benchmark.pedantic` with explicit rounds for slow functions.
- **mitata / tinybench**: put both variants in one file; use `do_not_optimize` (mitata) or return values so V8 cannot drop the work.
- **Go**: `go test -bench=. -count=20 -benchmem > new.txt`, then `benchstat old.txt new.txt`. benchstat reports a CI and a p-value and says "~" when there is no clear difference. Assign results to a package-level variable to avoid dead code elimination.
- **criterion (Rust)**: wrap inputs and outputs in `std::hint::black_box`. `cargo bench -- --save-baseline main`, switch branch, `cargo bench -- --baseline main`.
- **Load testing**: `wrk2 -R 2000` (fixed rate, corrects coordinated omission), `vegeta attack -rate=200/s`, `k6` with the `constant-arrival-rate` executor. Plain `wrk`, `ab` and `hey` are closed-loop by default.

## 10. Bundled scripts

- `scripts/bench.py`: process-level runner with warmups, shuffled rounds, wall and CPU time, peak RSS, floor comparison, output comparison, bootstrap CIs, Mann-Whitney, Cliff's delta, verdicts, JSON, markdown and chart output. `--help` lists every flag. Limitations: it times whole processes, so for work under about 5 ms use an in-process tool; peak RSS is per process tree as the OS reports it.
- `scripts/env_report.py`: machine and environment capture with warnings. `--redact` hides hostname, username and the names of other running programs.
- `scripts/latency_test.py`: open-loop HTTP or command load at a fixed rate, latency from intended start and service time from actual send, p50/p90/p99/p99.9/max, and a `--closed-loop` mode for showing coordinated omission. It is a Python thread pool, so it suits up to a few hundred requests per second; above that, or when it warns that the scheduler fell behind, use wrk2 or vegeta.
