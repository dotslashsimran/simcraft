# Bisect anything: reference

## How git bisect run reads exit codes

| Exit code | Meaning |
|---|---|
| 0 | good (old) |
| 1 to 127, except 125 | bad (new) |
| 125 | skip: this commit cannot be tested |
| 128 and above, or killed by a signal | **git stops the whole bisect** |

The last row is a trap. A shell that runs a program which segfaults exits 139, and git aborts. `bisect.py` wraps the oracle and turns every crash, signal and timeout into a plain 1, so a crash counts as bad instead of ending the run. If you call `git bisect run` yourself, do the same mapping.

## The oracle

Rules that save the most time:

1. **Test the one symptom, nothing else.** A broad check (the whole suite, a full page diff) finds the first commit that changed anything.
2. **Keep it outside the repo.** Old commits do not have your new script or test. Put scripts in a temp folder. For a test file that must live inside the tree, use `--overlay /tmp/test_bug.py:tests/test_bug.py`; it is copied in before each test and removed afterwards so checkouts do not fail.
3. **Separate "cannot test" from "bad".** A build failure, a missing tool, a server that never started, or an import error in unrelated code is 125, never 1. If you return 1 for those, bisect blames the first unbuildable commit.
4. **Prove it on both ends.** `bisect.py` runs the oracle on `--good` and `--bad` before it starts and stops if either is wrong. Do not skip this with `--no-verify` unless you have just checked by hand.
5. **Make it fast.** Bisect runs it about log2(N) times, plus repeats. Shrink the input, run one test, skip unrelated build targets.

### Failing test

```
--oracle "python -m pytest -x -q tests/test_invoice.py::test_rounding"
--oracle "npx jest src/cart.test.ts -t 'applies discount'"
--oracle "go test ./pkg/cart -run TestDiscount -count=1"
```
`-count=1` (Go) and similar flags turn off test result caches, which would otherwise hide changes. If the test was written after the bug, write it outside the repo and use `--overlay`.

Watch for tests that fail at old commits for a different reason (a fixture that changed name). Make the test print the actual value and look at the step log tails.

### Output changed (golden diff)

```
golden_oracle.py --record --golden /tmp/golden.txt --cmd "./report data.csv" --ignore "generated at .*"
golden_oracle.py --golden /tmp/golden.txt --cmd "./report data.csv" --ignore "generated at .*" --ignore "v\d+\.\d+\.\d+"
```
Record on a good commit. Blank out everything that changes on every run or every commit: timestamps, version strings, temp paths, process ids, random ids, durations. Use `--sort-lines` when the order is not stable (hash maps, parallel output). In the self test, the same oracle without `--ignore` blames a version bump commit, which shows why this matters.

### Got slower (benchmark)

Timing is noisy, and a single noisy step sends the bisect down the wrong half. Do it in this order:

1. Measure both ends: `bench_oracle.py --measure --cmd "..."` on the good and on the bad commit (`--repeats 9` if the spread is above 10%).
2. If the medians are less than about 20% apart, stop. Make the benchmark hit the slow path harder (bigger input, loop the hot call) until the gap is clear.
3. Bisect with `--good-median A --bad-median B`. The threshold is their geometric mean. Results within `--band` (10%) of the threshold return 125 rather than a guess.

Choices that matter:
- **Median, not mean.** One slow run from a background task moves a mean a lot and a median hardly at all.
- **Warm up.** The first run fills disk and JIT caches. The default is one untimed run.
- **`--metric cpu`** (user plus system time of the command) is far steadier on a busy laptop. Use wall time only when the slowdown is waiting: sleeps, network, locks, disk.
- **Close other heavy apps**, keep the laptop on power, and do not run two bisects at once.
- A gradual slowdown (many commits each a bit slower) has no single first bad commit. Bisect finds where the total crosses the threshold. Run it again with a lower threshold to find the earlier steps.

### Crash

Return bad only for this crash, so an unrelated failure is not blamed:
```
./app --load sample.bin; rc=$?
[ $rc -ge 128 ] && exit 1        # killed by a signal (segfault, abort)
exit 0
```
Or match the message:
```
out=$(./app --load sample.bin 2>&1); echo "$out" | grep -q "index out of range" && exit 1; exit 0
```
For memory bugs that do not always crash, build with a sanitizer (`-fsanitize=address`) in `--build`, and check its report.

### Visual change (screenshot diff)

1. Render the same view the same way every time: fixed window size, device scale 1, animations off, fixed fonts, fixed data, a fixed date if dates show.
2. Take a screenshot (for a web page, Playwright: `page.screenshot(path="shot.png")` after `page.wait_for_load_state("networkidle")`).
3. Compare with a reference from a good commit:
```
img_diff.py shot.png /tmp/ref.png --tolerance 8 --max-diff-ratio 0.001 --mask 0,0,200,40 --diff-out /tmp/diff.png
```
`--tolerance` absorbs anti-aliasing and colour rounding. `--mask` hides clocks, cursors and avatars. Look at `--diff-out` on the found commit to confirm it is the change you meant. The self test plants a one-step colour nudge earlier in history, which this setup correctly ignores.

### Behaviour in a running server

```
serve_oracle.py --cmd "python manage.py runserver 127.0.0.1:{port}" \
  --url "http://127.0.0.1:{port}/api/cart/7" --expect '"total": 30' --ready-timeout 90
```
It picks a free port (so a leftover server from a previous step cannot answer), waits until the server answers, checks status and body, and kills the whole process group afterwards. A server that never starts is 125. For a multi-step check (log in, then call), use `--probe "./check.sh {port}"`. If the server needs a database, reset it to a known state in `--build`; old commits may need old migrations.

## Unbuildable commits (125)

`bisect.py` returns 125 when install or build fails. git then tests a nearby commit. If the answer sits next to skipped commits, git cannot tell which one it is and says so. The report then lists the candidates. Do not guess: either test those commits by hand, or make them buildable (for example, a build command that patches a known broken file only at those commits) and run again.

A long stretch of unbuildable commits often means the build itself changed (a new tool version). Then the build command must depend on the commit, for example `if [ -f pnpm-lock.yaml ]; then pnpm i; else npm ci; fi`.

## Flaky oracles

| Situation | Settings |
|---|---|
| The check fails at random sometimes, whatever the commit | `--repeats 5` (or 7), majority vote |
| The bug itself shows only sometimes | `--repeats 10 --vote any-bad` |

Why it matters: with a flake rate of p and k bisect steps, the chance that every step is right is roughly (1 - p) to the power k. With p = 0.1 and 7 steps that is under 50%. A majority of 5 brings the per-step error from 10% to under 1%.

With `any-bad`, pick repeats so a bad commit almost surely shows the bug. If it shows in a fraction f of runs, n repeats miss it with chance (1 - f) to the power n; for f = 0.3, n = 10 gives about 3%.

Majority voting stops early once one side has more than half, so good commits that pass every time cost little. Better still, remove the flakiness at the source if you can (fixed random seeds, no real time, no network).

## Build caching and dependencies

- **Install only when needed.** `bisect.py --install` reruns only when a lockfile or manifest changes (package-lock.json, yarn.lock, pnpm-lock.yaml, poetry.lock, uv.lock, requirements*.txt, Cargo.lock, go.sum, Gemfile.lock and their manifests). Add more with `--dep-file`. In the self test, 7 steps needed 3 installs.
- **Keep the worktree between steps.** Untracked folders like `node_modules`, `.venv` and `target/` stay, so incremental builds work.
- **Stale output.** If old build artifacts can survive into the next commit (generated code, a copied bundle), use `--clean --keep node_modules --keep .venv`. It runs `git clean -fdx` but keeps those folders.
- **Speed up compiled languages** with ccache or sccache in `--build`. They key on file contents, so jumping between commits reuses work.
- **Old commits need old tools.** Pin the right Node, Python or compiler per commit inside `--build`, for example read `.nvmrc` or `.tool-versions`.
- **Submodules:** add `git submodule update --init --recursive` to `--build`.

## Worktrees

`bisect.py` runs `git worktree add --detach` into a temp folder (or `--workdir`) and bisects there. Bisect state is kept per worktree, so the user's branch, staged changes and stashes are never touched, and they can keep working while it runs. The worktree is removed at the end unless `--keep-worktree`. If a run is killed, clean up with `git worktree prune` after deleting the folder.

## Merge-heavy history

In a repo with many merged branches, plain bisect walks into the branches, where commits can be old, half-finished or unbuildable. `--first-parent` (git 2.29+) only tests the main line, so the answer is the merge (the PR) that brought the problem in. That is often what a team wants. To go deeper, run again inside that merge: `--good <merge>^1 --bad <merge>`, without `--first-parent`. The report prints this command when it finds a merge.

A bug that only appears when two branches meet is caused by the merge itself, not by any commit on either side. Then the merge is the true answer.

## Bisecting things that are not commits

`bisect_list.py` bisects any ordered list. The oracle gets the item as `{}` (shell quoted) and `$BISECT_ITEM`.

- **Dependency versions:**
  ```
  pip index versions somelib     # or: npm view somelib versions --json
  bisect_list.py --items-file versions.txt \
    --oracle 'pip install -q "somelib=={}" && python repro.py'
  ```
  Use a fresh virtual env (or `npm i somelib@{}` in a scratch folder). Put versions in release order, not string order.
- **Nightly builds or dates:** list the build ids or dates in order.
- **Config values or sizes:** `--range 1 4096` finds the smallest size that fails.
- **Data:** to find which row in a big input breaks a parser, use `--minimise` with the rows as items; the oracle reads the subset file `{}`.
- **Sets of flags, plugins or env vars:** `--minimise` finds the smallest combination that still fails. Removing any one item from the answer makes it pass.

Ordered mode assumes the list flips from good to bad once. If it flips back and forth, the answer is one flip, not the only one; check the neighbours by hand.

## From commit to exact hunk

Big commits hide the real change. `hunk_minimise.py` takes the commit's diff (zero context lines, so each change is its own hunk), applies subsets of the hunks onto the parent commit in a worktree, and uses delta debugging (ddmin) to find the smallest set that still fails. Subsets that do not build count as passing, so hunks that need each other (a new function and its caller) come out together.

- `--by file` works per file, for huge commits or generated files.
- Positional paths limit the search: hunks outside those paths are always applied.
- Cost is about n log n oracle runs in the worst case for n hunks, often much fewer.
- It writes `minimal.patch` in its work folder.

When done by hand, the same idea is: `git revert --no-commit <sha>`, then `git checkout <sha> -- file` (or `git add -p` in reverse) to bring back hunks one at a time, testing each time.

## Explaining the result

A good answer to "what broke this" has:
1. The commit: short hash, title, author, date, and the PR if the message names one.
2. The exact hunk, quoted.
3. The mechanism: why that line causes the symptom, checked by reading the code around it, not guessed from the title.
4. Confidence: number of steps, skipped commits, repeats, and any flakiness seen.
5. A next step: the smallest fix or a revert, and the oracle turned into a regression test.

## Common mistakes

- Good and bad swapped, or good not an ancestor of bad. `bisect.py` checks both.
- The oracle depends on the current tree (imports a helper that only exists now).
- Caches survive between steps: test result caches, build caches keyed on time, a running dev server.
- Leftover servers answering on a fixed port. Use a free port.
- Bisecting a symptom that also depends on the environment (a date, a remote API). Freeze it, or the answer moves.
- Treating "not conclusive" as an answer.
- A script named `bisect.py` shadows Python's standard `bisect` module for any script in the same folder. The scripts here guard against it; your own oracle scripts should not live in `scripts/`.
