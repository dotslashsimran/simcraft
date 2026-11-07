#!/usr/bin/env python3
"""Run a test command many times and measure how flaky it is.

Records pass or fail, duration and a normalized output hash for every run,
computes the failure rate with a Wilson confidence interval, groups distinct
failure messages, counts which test names failed, and can compare two runs
(before and after a fix) with a one-sided Fisher exact test.

Python 3.9+ standard library only.

Examples:
  flake_rate.py -n 50 -- python3 -m pytest tests/test_cart.py -q
  flake_rate.py -n 200 -j 8 --json before.json -- npm test
  flake_rate.py -n 300 --json after.json --compare before.json -- npm test
  flake_rate.py --plan 0.02            # clean runs needed to show rate < 2%
  flake_rate.py -n 30 -- go test ./pkg -run TestX -count=1 -shuffle={seed}

In the command, {run} becomes the run number and {seed} a fresh random seed
(recorded per run so a failing order can be replayed). FLAKE_RUN and
FLAKE_SEED are also set in the environment of every run.
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import random
import re
import shlex
import subprocess
import sys
import time

# ---------------------------------------------------------------- statistics


def z_for(conf):
    # two-sided normal quantile for common confidence levels, no scipy needed
    table = {0.80: 1.2816, 0.90: 1.6449, 0.95: 1.9600, 0.98: 2.3263, 0.99: 2.5758}
    if conf in table:
        return table[conf]
    # Acklam-style rational approximation of the inverse normal CDF
    p = 1 - (1 - conf) / 2
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
        (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def wilson(fails, n, conf=0.95):
    if n == 0:
        return (0.0, 1.0)
    z = z_for(conf)
    p = fails / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def zero_fail_upper(n, conf=0.95):
    """Exact one-sided upper bound on the rate after n runs with no failures."""
    if n == 0:
        return 1.0
    return 1 - (1 - conf) ** (1.0 / n)


def runs_needed(target, conf=0.95):
    """Clean runs in a row needed to claim rate < target at this confidence."""
    return math.ceil(math.log(1 - conf) / math.log(1 - target))


def fisher_one_sided(f1, n1, f2, n2):
    """P(after has this few failures or fewer | rates equal). Small p = real drop."""
    total_f = f1 + f2
    N = n1 + n2
    denom = math.comb(N, total_f)
    p = 0.0
    for k in range(0, f2 + 1):
        if total_f - k > n1 or k > n2:
            continue
        p += math.comb(n2, k) * math.comb(n1, total_f - k) / denom
    return min(1.0, p)


# ----------------------------------------------------------- output parsing

NOISE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "UUID"),
    (re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:?\d\d)?"), "TIMESTAMP"),
    (re.compile(r"(/tmp|/var/folders|/private/var)[^\s'\"]*"), "TMPPATH"),
    (re.compile(r"\b\d+(\.\d+)?\s*(ms|s|sec|seconds)\b"), "DURATION"),
    (re.compile(r"\d+"), "N"),
]

INTERESTING = re.compile(
    r"(Error\b|Exception\b|^FAIL|FAILED|panic:|assert|Assert|[Ee]xpected|Received|"
    r"Timeout|timed out|not ok|DATA RACE|EADDRINUSE|ECONNREFUSED|EMFILE|"
    r"Traceback|Unhandled|ERROR:|--- FAIL)"
)

# reporters: unittest, pytest, jest/vitest, go, node:test, cargo, maven/gradle
TEST_NAME_PATTERNS = [
    re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)"),           # unittest
    re.compile(r"^FAILED (\S+::\S+)"),                             # pytest -rf / summary
    re.compile(r"^\s*● (.+? › .+)$"),                              # jest
    re.compile(r"^\s*(?:×|✗|FAIL)\s+(\S+\.(?:test|spec)\.\w+ > .+)$"),  # vitest
    re.compile(r"^\s*--- FAIL: (\S+)"),                            # go
    re.compile(r"^\s*not ok \d+ - (.+?)(?:\s+#.*)?$"),             # node:test / TAP
    re.compile(r"^\s*✖ (.+?) \(\d"),                               # node:test spec reporter
    re.compile(r"^test (\S+) \.\.\. FAILED"),                      # cargo
    re.compile(r"^\[ERROR\]\s+(\S+)\s+.*<<< (?:FAILURE|ERROR)"),  # surefire
    re.compile(r"^(\S+ > .+) FAILED$"),                            # gradle
]


def normalize(line):
    line = line.strip()
    for rx, rep in NOISE:
        line = rx.sub(rep, line)
    return line[:200]


def signature(output):
    seen = []
    for line in output.splitlines():
        if INTERESTING.search(line):
            n = normalize(line)
            if n and n not in seen:
                seen.append(n)
        if len(seen) >= 4:
            break
    if not seen:
        tail = [normalize(l) for l in output.strip().splitlines()[-3:]]
        seen = [t for t in tail if t] or ["<no output>"]
    return " | ".join(seen)


def failed_tests(output):
    names = []
    for line in output.splitlines():
        for rx in TEST_NAME_PATTERNS:
            m = rx.search(line)
            if m:
                name = ".".join(reversed(m.groups())) if rx is TEST_NAME_PATTERNS[0] else m.group(1)
                name = name.strip()
                if name and name not in names and not name.startswith("tests ") and name not in ("tests", "suites"):
                    names.append(name)
                break
    return names


HINTS = [
    (r"DATA RACE|race detected|ThreadSanitizer", "race: concurrent access without synchronization"),
    (r"EADDRINUSE|address already in use|Address in use|bind\(\)", "port collision: hardcoded port or port not released"),
    (r"EMFILE|too many open files|ResourceWarning|unclosed", "resource leak: files, sockets or handles not closed"),
    (r"ECONNREFUSED|ECONNRESET|Connection refused|connection reset", "startup or network race: service not ready, or real network in test"),
    (r"after (the )?test (has )?ended|asynchronous activity|unhandled ?rejection|"
     r"Cannot log after tests are done|did not exit|open handles", "async leak: unawaited promise, timer or handle outliving the test"),
    (r"[Tt]ime ?out|timed out|deadline exceeded|exceeded timeout", "too slow or waiting on something that never happens (wait condition, deadlock, CPU starvation)"),
    (r"already exists|UNIQUE constraint|duplicate key|IntegrityError", "shared state: data left by another test or another run"),
    (r"FileExistsError|ENOENT|No such file|FileNotFoundError|EEXIST", "filesystem: shared temp dir, fixed file name or missing cleanup"),
    (r"day|month|year|date|Date|hour|timezone|TZ|DST|UTC|leap", "time: depends on current date, clock or timezone"),
    (r"seed|random|shuffle", "randomness: unseeded random data or order"),
    (r"MemoryError|out of memory|OOM|Killed|signal 9|SIGKILL", "resource: memory or process limits"),
]


def hints_for(sig):
    out = []
    for rx, text in HINTS:
        if re.search(rx, sig):
            out.append(text)
    return out


# ------------------------------------------------------------------ running


def build_cmd(cmd, run, seed):
    return [a.replace("{run}", str(run)).replace("{seed}", str(seed)) for a in cmd]


def run_once(i, args, base_env):
    seed = random.randrange(1, 2**31)
    argv = build_cmd(args.cmd, i, seed)
    env = dict(base_env)
    env["FLAKE_RUN"] = str(i)
    env["FLAKE_SEED"] = str(seed)
    t0 = time.monotonic()
    timed_out = False
    try:
        if args.shell:
            p = subprocess.run(" ".join(argv), shell=True, cwd=args.cwd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=args.timeout)
        else:
            p = subprocess.run(argv, cwd=args.cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=args.timeout)
        code = p.returncode
        out = p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        timed_out = True
        code = None
        out = (e.stdout or b"").decode("utf-8", "replace") + "\n[flake_rate] Timeout: run killed after %ss" % args.timeout
    dur = time.monotonic() - t0
    ok = (code == 0) and not timed_out
    if args.fail_pattern and re.search(args.fail_pattern, out, re.M):
        ok = False
    norm = "\n".join(normalize(l) for l in out.splitlines())
    rec = {
        "run": i, "seed": seed, "ok": ok, "exit": code, "timed_out": timed_out,
        "duration": round(dur, 3),
        "output_hash": hashlib.sha1(norm.encode()).hexdigest()[:12],
    }
    if not ok:
        rec["signature"] = signature(out)
        rec["failed_tests"] = failed_tests(out)
        rec["output_tail"] = "\n".join(out.splitlines()[-60:])
    if args.keep_output:
        os.makedirs(args.keep_output, exist_ok=True)
        with open(os.path.join(args.keep_output, "run-%04d-%s.log" % (i, "pass" if ok else "fail")), "w") as f:
            f.write("$ %s\n# FLAKE_SEED=%d\n\n%s" % (" ".join(map(shlex.quote, argv)), seed, out))
    return rec


def median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def pct(x):
    return "%.1f%%" % (100 * x)


def report(res, conf, target):
    runs = res["runs"]
    uses_seed = any("{seed}" in a for a in res["command"])
    n = len(runs)
    fails = [r for r in runs if not r["ok"]]
    f = len(fails)
    lo, hi = wilson(f, n, conf)
    print("\n== %s: %d runs, %d failed, rate %s (%d%% CI %s to %s)" % (
        res.get("label") or "result", n, f, pct(f / n if n else 0), int(conf * 100), pct(lo), pct(hi)))
    if f == 0:
        print("   no failures: rate is below %s with %d%% confidence (exact bound)" % (
            pct(zero_fail_upper(n, conf)), int(conf * 100)))
    passes = [r["duration"] for r in runs if r["ok"]]
    fdur = [r["duration"] for r in fails]
    if passes and fdur:
        mp, mf = median(passes), median(fdur)
        print("   median duration: pass %.2fs, fail %.2fs" % (mp, mf))
        if mf > 1.5 * mp:
            print("   failures are slower: suspect waits, timeouts, contention or deadlock")
        elif mf < 0.6 * mp:
            print("   failures are faster: suspect early crash, setup error or a short-circuited wait")
    hashes = {r["output_hash"] for r in runs if r["ok"]}
    if len(hashes) > 1 and len(passes) > 1:
        print("   passing runs produced %d distinct normalized outputs (nondeterministic output or warnings)" % len(hashes))
    if fails:
        groups = {}
        for r in fails:
            groups.setdefault(r["signature"], []).append(r)
        print("\n   distinct failures (%d):" % len(groups))
        for sig, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print("   [%dx] runs %s" % (len(rs), ",".join(str(r["run"]) for r in rs[:8]) + (" ..." if len(rs) > 8 else "")))
            print("        " + sig[:400])
            if uses_seed:
                print("        replay seeds: " + ", ".join(str(r["seed"]) for r in rs[:3]))
            for h in hints_for(sig):
                print("        hint: " + h)
        tests = {}
        for r in fails:
            for t in r.get("failed_tests", []):
                tests[t] = tests.get(t, 0) + 1
        if tests:
            print("\n   failing tests (failures out of %d runs):" % n)
            for t, c in sorted(tests.items(), key=lambda kv: -kv[1]):
                print("   %4d  %s" % (c, t))
        if uses_seed:
            seeds = [str(r["seed"]) for r in fails[:5]]
            print("\n   seeds of failing runs (replay with {seed}): " + ", ".join(seeds))
    if target:
        need = runs_needed(target, conf)
        print("\n   to claim rate < %s at %d%% confidence you need %d clean runs in a row" % (
            pct(target), int(conf * 100), need))


def compare(before, after, conf):
    nb = len(before["runs"]); fb = sum(1 for r in before["runs"] if not r["ok"])
    na = len(after["runs"]); fa = sum(1 for r in after["runs"] if not r["ok"])
    p = fisher_one_sided(fb, nb, fa, na)
    print("\n== before vs after")
    print("   before: %d/%d failed (%s)  after: %d/%d failed (%s)" % (
        fb, nb, pct(fb / nb if nb else 0), fa, na, pct(fa / na if na else 0)))
    print("   one-sided Fisher exact p = %.4g" % p)
    if p < 1 - conf:
        print("   the drop is real at %d%% confidence" % int(conf * 100))
    else:
        print("   NOT proven: the difference could be luck. Run more 'after' runs.")
    if fa == 0 and fb:
        rate = fb / nb
        need = runs_needed(rate, conf) if rate < 1 else 1
        print("   (with the old rate %s, %d clean runs alone would rule it out at %d%%)" % (
            pct(rate), need, int(conf * 100)))
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", "--runs", type=int, default=30)
    ap.add_argument("-j", "--jobs", type=int, default=1, help="parallel runs (parallelism is itself a stressor)")
    ap.add_argument("--timeout", type=float, default=600, help="seconds per run before it counts as a failure")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--env", action="append", default=[], metavar="K=V")
    ap.add_argument("--shell", action="store_true", help="run the command through the shell")
    ap.add_argument("--fail-pattern", help="regex in output that also counts as failure (e.g. a warning)")
    ap.add_argument("--until-fail", action="store_true", help="stop at the first failure (repro mode)")
    ap.add_argument("--confidence", type=float, default=0.95)
    ap.add_argument("--target", type=float, help="rate you want to prove you are under, e.g. 0.01")
    ap.add_argument("--plan", type=float, metavar="RATE", help="only print clean runs needed for RATE and exit")
    ap.add_argument("--label", default="")
    ap.add_argument("--json", help="write all run records here (evidence log)")
    ap.add_argument("--compare", help="earlier --json file to compare against")
    ap.add_argument("--keep-output", metavar="DIR", help="save every run's full output here")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    conf = args.confidence

    if args.plan:
        print("to claim rate < %s at %d%% confidence: %d clean runs in a row" % (
            pct(args.plan), int(conf * 100), runs_needed(args.plan, conf)))
        for r in (0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.001):
            print("   rate %-6s -> %6d runs" % (pct(r), runs_needed(r, conf)))
        return 0

    if args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    if not args.cmd:
        ap.error("give a command after --")

    env = dict(os.environ)
    for kv in args.env:
        k, _, v = kv.partition("=")
        env[k] = v

    runs = []
    t0 = time.time()
    try:
        if args.jobs <= 1 or args.until_fail:
            for i in range(1, args.runs + 1):
                r = run_once(i, args, env)
                runs.append(r)
                if not args.quiet:
                    sys.stderr.write("." if r["ok"] else "F"); sys.stderr.flush()
                if args.until_fail and not r["ok"]:
                    break
        else:
            with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
                futs = [ex.submit(run_once, i, args, env) for i in range(1, args.runs + 1)]
                for fu in cf.as_completed(futs):
                    r = fu.result()
                    runs.append(r)
                    if not args.quiet:
                        sys.stderr.write("." if r["ok"] else "F"); sys.stderr.flush()
    except KeyboardInterrupt:
        sys.stderr.write("\n[interrupted, reporting %d runs]\n" % len(runs))
    sys.stderr.write("\n")
    runs.sort(key=lambda r: r["run"])

    res = {
        "label": args.label, "command": args.cmd, "cwd": args.cwd or os.getcwd(),
        "jobs": args.jobs, "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0)),
        "wall_seconds": round(time.time() - t0, 1),
        "env_overrides": args.env, "runs": runs,
    }
    if not runs:
        return 2
    report(res, conf, args.target)
    if args.until_fail and runs and not runs[-1]["ok"]:
        print("\n   first failure at run %d, output tail:\n" % runs[-1]["run"])
        print(runs[-1]["output_tail"])
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(res, fh, indent=1)
        print("\n   evidence written to " + args.json)
    if args.compare:
        with open(args.compare) as fh:
            before = json.load(fh)
        compare(before, res, conf)
    return 1 if any(not r["ok"] for r in runs) else 0


if __name__ == "__main__":
    sys.exit(main())
