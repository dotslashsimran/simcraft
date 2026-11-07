# Examples: naive result vs honest result

Every example here was run for real on an Apple M2 laptop with 8 GB of RAM, on battery, with heavy background load (load average between 9 and 55 during the runs). That is a bad machine for benchmarking, and the reports say so. It also shows what the tools do when conditions are poor: they widen the intervals and refuse to claim a winner, instead of printing a confident wrong number. Your numbers will differ. Run each command from this folder.

## 1. The lazy `map()` that looks 100,000x faster

Files: `lazy_work.py`, `naive_timeit.py`, outputs `naive_timeit.out.txt`, `lazy_work.out.txt`, `lazy_work.md`, `lazy_work.json`, `lazy_work.svg`.

```
python3 naive_timeit.py
python3 ../scripts/bench.py -n listcomp -n map-unused -n map-used \
  "python3 lazy_work.py listcomp" "python3 lazy_work.py map-unused" "python3 lazy_work.py map-used" \
  --floor "python3 lazy_work.py empty" --subtract-floor --same-output --runs 25 --warmup 3 --redact \
  --json lazy_work.json --md lazy_work.md --chart lazy_work.svg
```

- **Naive:** `timeit` says `map(str.upper, WORDS)` takes 0.6 us and the list comprehension 60 ms, so "map is 106,370x faster". `map()` returns a lazy iterator and never uppercases anything. The same script times `2 ** 64` at 3.9 ns, but `dis` shows CPython folded it into a constant at compile time.
- **Honest:** bench.py marks `map-unused` as **not a valid comparison** for two separate reasons: it is no slower than the empty floor program, and it printed different output from the baseline. For the real comparison (`map-used` vs `listcomp`), it reports no clear difference (time ratio 0.84 after subtracting startup, 95% CI 0.34 to 1.49), because startup and setup were most of the time and the machine was noisy.

## 2. Two identical sorts, and single runs that pick a different winner each time

Files: `two_sorts.py`, `naive_two_sorts.py`, outputs `naive_two_sorts.out.txt`, `two_sorts.out.txt`, `two_sorts.md`, `two_sorts.json`, `two_sorts.svg`.

```
python3 naive_two_sorts.py
python3 ../scripts/bench.py -n sorted -n copysort -n sorted-x4 \
  "python3 two_sorts.py sorted" "python3 two_sorts.py copysort" "python3 two_sorts.py sorted-x4" \
  --same-output --runs 40 --warmup 5 --redact --json two_sorts.json --md two_sorts.md --chart two_sorts.svg
```

- **Naive:** five attempts of "run each once" gave copysort 28% faster, sorted 36% faster, copysort 171% faster, copysort 24% faster, copysort 3% faster.
- **Honest:** `sorted` vs `copysort`: no clear difference (ratio 1.10, 95% CI 0.85 to 1.32, p=0.84). The positive control `sorted-x4` does one extra sort pass (a third more work) and was detected: 1.34x slower (95% CI 1.08x to 1.79x, p=0.0014). So the tool is not just saying "no difference" to everything. It also warned that the machine's load dropped during the run.

## 3. File reads where whichever variant runs first loses

Files: `file_read.py`, `naive_file_read.py`, outputs `naive_file_read.out.txt`, `file_read.out.txt`, `file_read.md`, `file_read.json`, `file_read.svg`.

```
python3 naive_file_read.py <a scratch folder> 1024
python3 file_read.py make data.bin 512
python3 ../scripts/bench.py -n buf64k -n buf1m \
  "python3 file_read.py read data.bin 65536" "python3 file_read.py read data.bin 1048576" \
  --same-output --runs 30 --warmup 3 --redact --json file_read.json --md file_read.md --chart file_read.svg
rm data.bin
```

- **Naive:** write a fresh 1 GiB file, time a 64 KiB buffer read then a 1 MiB buffer read. Trial 1: "1 MiB buffer is 3.3x faster". Swap the order on a new file. Trial 2: "64 KiB buffer is 3.7x faster". The first reader pays for the disk; the second reads from the page cache.
- **Honest:** with warmups and shuffled order both variants read from a warm cache, and bench.py reports no clear difference (ratio 0.84, 95% CI 0.45 to 1.62). It also flagged that times drifted upward during the run (Spearman rho 0.7), which on this machine was background load rising.
- **Cold reads:** to measure cold reads, pass a cache-eviction command as `--prepare`. On Linux, `python3 file_read.py evict data.bin` works without root. On macOS you need `sudo purge`. `F_NOCACHE` (the `read-nocache` mode) did not reliably make reads cold on this machine when the file was already cached, so it is not used for the results.

## 4. Coordinated omission: GC-like pauses that a closed-loop test hides

Files: `stall_server.py`, outputs `latency_closed_loop.md`, `latency_open_loop.md`.

```
python3 stall_server.py --port 8099 --period 2 --stall-ms 400 &
python3 ../scripts/latency_test.py --url http://127.0.0.1:8099/ --closed-loop --concurrency 1 --duration 20 --md latency_closed_loop.md
python3 ../scripts/latency_test.py --url http://127.0.0.1:8099/ --rate 200 --duration 20 --md latency_open_loop.md
kill %1
```

The server answers in about 1 ms but freezes for 400 ms every 2 seconds, so it is frozen 20% of the time.

| Mode | p50 | p90 | p99 | p99.9 |
|---|---|---|---|---|
| Closed loop (naive, waits for each reply) | 1.85 ms | 5.91 ms | 37.31 ms | 408.87 ms |
| Open loop at 200 req/s (honest) | 3.36 ms | 212.34 ms | 392.50 ms | 411.86 ms |

The closed loop sends only one request per freeze, so 20% frozen time shows up as about 0.2% slow samples and p99 looks like 37 ms. The open loop keeps sending on schedule, so every request that should have gone out during a freeze waits for it, and p90 and p99 show what users actually get. The open-loop report also warned that the scheduler fell behind by up to 75 ms on this overloaded laptop, which is the cue to use wrk2 or vegeta for anything that matters.
