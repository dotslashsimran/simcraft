# Flaky test killer: reference

## 1. Cause taxonomy

Find the row whose signature matches, confirm it with the stressor, then apply the fix. The "wrong fix" column is what people usually do instead.

| Cause | Signature | Stressor that exposes it | Root fix | Wrong fix |
|---|---|---|---|---|
| **Test pollution (order dependence)** | Passes alone, fails in the suite or with a shuffled order. Value is the one another test set (`'€19.99' != '$19.99'`, row count off by one, "already exists"). | Random order with a seed; `find_polluter.py` | Make the polluter restore what it changes (`patch.dict`, `monkeypatch`, `t.Cleanup`, `afterEach`, transaction rollback). Also make the victim set up the state it needs. Both, so the next polluter cannot hurt it either. | Pin the test order |
| **State setter (hidden dependency)** | Fails alone, passes in the suite. | Run the test alone; `find_polluter.py` finds the setter | Give the victim its own fixture | Run it in the suite only |
| **Sleep instead of wait** | Assertion that something "is done" fails; fail rate is steady; often worse on slow CI. | Run alone many times; `--cpu` | Wait on the real condition: an Event, `join`, a returned promise, a poll with a deadline (`waitFor`, `assert eventually`). | Longer sleep |
| **Unawaited promise or async leak** | Node "asynchronous activity after the test ended", "Cannot log after tests are done", Jest "did not exit", assertion runs before the write. | Run alone many times; `--detectOpenHandles` | `await` the call, return the promise, close servers and timers in `after`/`afterEach`. Turn on lint rules `no-floating-promises` / `require-await`. | `setTimeout` in the test |
| **Data race** | Wrong counts or corrupted values under load; Go `DATA RACE`; fails more on many cores. | `go test -race`, ThreadSanitizer, `-j` and `--cpu` | Lock, channel, atomic, or remove the shared mutable state | Lower parallelism |
| **Clock and date** | Fails on certain days or hours. Month arithmetic (Jan 31 + 1 month), "tomorrow" crossing a month or year, DST hour that does not exist or happens twice, leap day, 2038. | `stress.py --clock month-end` (and the other presets) | Inject the clock (pass `now` in, or use a fake clock library) and test the edge dates explicitly. Fix the arithmetic (clamp the day, use a date library). | Mark it "known flaky on the 31st" |
| **Timezone** | "Passes in CI, fails locally" (CI runs in UTC). Off by hours or by one day. | `stress.py --tz Pacific/Kiritimati` and `--tz Asia/Kolkata` (half-hour offset) | Decide: store and compare in UTC, convert only for display, and pass the zone explicitly. | Set `TZ=UTC` in CI only |
| **Unseeded randomness** | Fails for some generated inputs. | Run many times; record and replay the seed | Seed it and print the seed on failure; if the random input finds a real bug, fix the bug and add that input as a fixed case. Property tests should save failing examples. | Remove the randomness |
| **Port collision** | `EADDRINUSE`, "address already in use"; fails only in parallel or after a crashed run. | `flake_rate.py -j 8` | Bind to port 0 and read the assigned port; never hardcode ports. | Retry on a different fixed port |
| **Service not ready** | `ECONNREFUSED` at startup, first test in a file fails. | `--cpu`, cold start | Wait on a health check or readiness signal with a deadline. | Sleep 2 seconds |
| **Real network** | Timeouts, DNS errors, rate limits, different payloads. | Block the network (see section 3) | Fake the service at the HTTP boundary, or record and replay responses. | Retry |
| **Filesystem and temp dir reuse** | `FileExistsError`, `EEXIST`, `ENOENT`, leftover files, tests passing only on a dirty checkout. | `stress.py --tmp fresh`; parallel runs | Unique temp dir per test (`tmp_path`, `t.TempDir()`, `mkdtemp`), clean up in teardown. | `rm -rf` at the start of the suite |
| **Resource leak** | `EMFILE`, "too many open files", `ResourceWarning: unclosed`, slowdown over the suite. | `stress.py --nofile 64`; `python -X dev -W error::ResourceWarning` | Close in `finally` / context managers / `defer`; `goleak` for goroutines. | Raise the ulimit |
| **Shared external state** | Database rows, cache keys, queues shared between tests or between CI jobs. | Parallel runs, two CI jobs at once | Unique names per test (random suffix), a transaction per test, or a database per worker. | Serialize the whole suite |
| **Floating point and ordering** | Map or set iteration order, float sums in a different order, sort that is not stable. | Different `PYTHONHASHSEED`, different Go runs | Sort before comparing, compare with a tolerance, compare as sets. | Change the expected value |
| **Locale** | Uppercase or lowercase of `i` in Turkish, `1.234,5` number format, date formats. | `stress.py --locale tr_TR.UTF-8` | Use locale-independent functions for machine strings; set locale explicitly where it matters. | Set locale in CI only |
| **Too tight timeouts or perf asserts** | "took 120ms, expected < 100ms"; fails under load. | `--cpu` | Do not assert wall-clock speed in unit tests; move to a benchmark. For real deadlines, use a fake clock. | Raise the limit |
| **Test framework or cache state** | Fails after a code change until cache is cleared; mocks leak across tests. | Run with caches disabled | Reset mocks in `afterEach` (`restoreMocks: true`), `-count=1` in Go, `-p no:cacheprovider` in pytest. | Clear caches in CI |

Reading `flake_rate.py` output:
- **Fail duration much longer than pass duration**: the test is waiting for something (timeout, deadlock, slow service).
- **Fail duration much shorter**: setup crashed or a check ran too early.
- **Several distinct failure groups**: more than one bug. Fix and measure them one at a time.
- **Passing runs with different normalized outputs**: nondeterministic output (warnings, ordering). Often a clue.

## 2. Per-ecosystem commands

Wrap any of these with `flake_rate.py -n N -- ...` to measure, and with `stress.py` to stress. Use `{seed}` where the tool takes a seed.

### pytest
- Single test many times: `pytest tests/test_x.py::test_y -p no:cacheprovider -q` inside `flake_rate.py`, or with pytest-repeat `pytest --count=200 -x tests/test_x.py::test_y` (`-x` stops at the first failure).
- Random order: install `pytest-randomly` (it shuffles by default and also reseeds `random`). Replay: `pytest -p randomly --randomly-seed=1234`. Turn it off for bisecting: `-p no:randomly`.
- Under flake_rate: `flake_rate.py -n 100 -- pytest -q -p randomly --randomly-seed={seed}`.
- Polluter: `find_polluter.py --cmd "pytest -q -p no:randomly {tests}"`. Get the order from `pytest --collect-only -q` with the failing seed, or `-v` output.
- Parallel: `pytest-xdist` `-n 8`. A test that fails only under xdist shares a resource.
- Leaks: `python -X dev -W error::ResourceWarning -m pytest`. Hangs: `pytest-timeout --timeout=30` gives a stack trace instead of a stuck job.
- Network: `pytest-socket --disable-socket`.
- Avoid as a fix: `pytest-rerunfailures`, `@flaky`.

### unittest
- `python -m unittest a.B.test_c d.E.test_f` runs ids in the given order, which makes it easy to bisect. No built-in shuffle: list ids and shuffle them yourself (see `examples/make_examples.py`, `run_tests.py`).

### Jest
- `--runInBand` (or `-i`) runs files in one process; a failure that disappears with it means files share something outside the process (port, file, database) or it is load related.
- Random order inside files: `--randomize --seed=1234` (`--showSeed` to print it). There is no repeat flag, so loop with `flake_rate.py`.
- Polluter at file level: `jest --runInBand --runTestsByPath a.test.js b.test.js` keeps the order you give.
- Leaks: `--detectOpenHandles`. Mock leaks: `restoreMocks: true` in config.
- Avoid as a fix: `jest.retryTimes`.

### Vitest
- Shuffle: `--sequence.shuffle --sequence.seed=1234`.
- Repeat one test: `test("x", { repeats: 100 }, fn)`, or loop with `flake_rate.py`.
- Isolation: `--no-file-parallelism`, `--pool=forks`. A test that fails only with `--isolate=false` (or `isolate: false`) depends on module state.
- Avoid as a fix: `--retry`.

### node:test
- `node --test --test-concurrency=1` runs files one at a time. `--test-name-pattern="name"` selects tests.
- There is no shuffle flag (Node 24). Shuffle file order yourself by passing files in order.
- `--test-isolation=none` runs every file in one process, which exposes module state shared between files.
- The runner reports "generated asynchronous activity after the test ended" for unawaited work. Treat it as a failure.

### Go
- `go test -run '^TestX$' -count=500 -failfast ./pkg` (`-count=1` also bypasses the test cache).
- `-race` always, on any concurrency flake. `-shuffle=on` prints the seed; replay with `-shuffle=1234`. Under flake_rate: `go test -count=1 -shuffle={seed} ./...`.
- `-cpu 1,2,8` runs with different GOMAXPROCS. `-parallel N` for `t.Parallel()` tests.
- `golang.org/x/tools/cmd/stress` runs a compiled test binary in a loop under load: `go test -c -o x.test && stress ./x.test -test.run TestX`.
- Leaks: `go.uber.org/goleak`. Temp dirs: `t.TempDir()`. Cleanup: `t.Cleanup`.

### Rust (cargo)
- Tests run as threads in one process by default, so globals and env vars are shared. `cargo test name -- --exact --test-threads=1` serializes; a flake that disappears points to shared process state.
- Shuffle (nightly only): `cargo test -- -Z unstable-options --shuffle --shuffle-seed 1234`.
- `cargo nextest run` runs each test in its own process, which hides in-process pollution. Compare both runners.
- `std::env::set_var` in tests is a common polluter.

### JUnit 5 (Maven or Gradle)
- Random method order: `junit.jupiter.testmethod.order.default=org.junit.jupiter.api.MethodOrderer$Random` in `junit-platform.properties`; random class order: `junit.jupiter.testclass.order.default=org.junit.jupiter.api.ClassOrderer$Random`; fix the seed with `junit.jupiter.execution.order.random.seed=1234`.
- Repeat: `@RepeatedTest(200)`.
- Maven: `mvn test -Dtest='OrderTest#saves'`, `-Dsurefire.runOrder=random`. Gradle: `gradle test --tests 'OrderTest.saves' --rerun`.
- Timezone: the JVM reads `TZ`; also try `-Duser.timezone=Pacific/Kiritimati`.
- The research tool iDFlakies automates order-dependence detection for Maven projects.
- Avoid as a fix: `surefire.rerunFailingTestsCount`.

## 3. Stressors the scripts do not cover

- **Slow or lossy network.** Linux, as root: `tc qdisc add dev lo root netem delay 200ms 50ms loss 1%` (remove with `tc qdisc del dev lo root`). Anywhere: put Toxiproxy between the test and the service and add latency or cut the connection. macOS: Network Link Conditioner.
- **No network at all.** Linux: `unshare -rn <cmd>`. Python: `pytest-socket`. Any test that fails is making a real network call.
- **Memory pressure.** Run in a container with a memory limit (`docker run -m 512m`).
- **Slow disk.** Put the temp dir on a slow or network volume, or fill the disk to near full.
- **Other clocks.** The clock shims move wall-clock time only. `time.monotonic`, `performance.now` and timers keep real time, which is what you want for timeouts. For other languages `stress.py` uses `faketime` (libfaketime) if it is installed. On macOS, libfaketime cannot hook system-protected binaries.

## 4. Statistics

- **Flake rate** = failures / runs. The Wilson interval is used because the normal interval is wrong near 0% and 100%, which is where flake rates live.
- **Proving a low rate.** With zero failures in n runs, the 95% upper bound on the rate is `1 - 0.05^(1/n)`, about `3/n` (the "rule of three"). So: 30 clean runs proves under 10%, 300 under 1%, 3000 under 0.1%. `flake_rate.py --plan RATE` prints the table.
- **Proving a fix.** Compare before and after with a one-sided Fisher exact test (`--compare`). If the old rate was p, about `3/p` clean runs after the fix are enough: 12 for a 25% flake, 300 for a 1% flake. A 1% flake needs hundreds of runs before AND after; parallel runs (`-j`) make that cheap, but run the "before" and "after" with the same `-j`, because parallelism changes the rate.
- **Sweep flags.** `stress.py --sweep` flags a stressor only when a one-sided Fisher test gives p < 0.05 against the baseline. With `-k 5`, only a jump like 0/5 to 4/5 counts, so raise `-k` for low-rate flakes.

## 5. When the fix really is in the test

Fixes that change only the test are fine when the test was wrong, for example a test that used `new Date()` where it should have used fixed edge dates. Fixes that change only the test are not fine when the test found a real bug, for example `nextMonth(Jan 31)` returning March 3. Fix the code too, and keep the edge case as a fixed test.

Quarantine (skip the test with an issue link) is acceptable only as a short stop while the root cause is being fixed, with an owner and a date. It is never the fix.
