#!/usr/bin/env python3
"""Benchmark shell commands honestly and compare them with real statistics.

Standard library only (Python 3.9+). matplotlib is used for PNG charts if it is
installed; SVG charts need nothing.

    python3 bench.py "python3 old.py" "python3 new.py"
    python3 bench.py -n old -n new "python3 old.py" "python3 new.py" \\
        --runs 40 --warmup 5 --floor "python3 -c pass" \\
        --json out.json --md out.md --chart out.svg

What it does:
  * warmup runs per command, then measured runs in shuffled rounds (each round
    runs every command once, in a new random order) so drift, thermal changes
    and background noise hit all variants equally
  * records wall time, user+system CPU time and peak RSS for every run
  * optional --prepare command before every measured run (e.g. evict a cache)
  * optional --floor command: an "empty" version of your program, to show how
    much of the time is just startup, and to catch work that never ran
  * medians, percentiles, MAD, bootstrap confidence intervals
  * the first command is the baseline; every other command is compared to it
    with a bootstrap CI of the ratio of medians, a Mann-Whitney U test and
    Cliff's delta, then a one-line verdict
  * warnings for drift, unfinished warmup, high spread, results near the floor,
    and commands whose output differs (with --same-output)
"""
import argparse
import hashlib
import json
import math
import os
import platform
import random
import shlex
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---------------------------------------------------------------- statistics


def percentile(sorted_xs, q):
    """Linear interpolation percentile, q in [0, 100]. Input must be sorted."""
    if not sorted_xs:
        return float("nan")
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = (len(sorted_xs) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    frac = pos - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac


def median(xs):
    return percentile(sorted(xs), 50)


def mad(xs):
    """Median absolute deviation, scaled to match the standard deviation for normal data."""
    m = median(xs)
    return 1.4826 * median([abs(x - m) for x in xs])


def bootstrap(stat, samples, n_boot, rng, conf):
    """Percentile bootstrap CI for stat(*resampled_samples)."""
    vals = []
    for _ in range(n_boot):
        res = [rng.choices(s, k=len(s)) for s in samples]
        v = stat(*res)
        if v is not None and not math.isnan(v):
            vals.append(v)
    vals.sort()
    if len(vals) < 0.99 * n_boot:
        return float("nan"), float("nan")  # too many resamples had no valid value
    alpha = (1 - conf) / 2
    return percentile(vals, 100 * alpha), percentile(vals, 100 * (1 - alpha))


def rank_with_ties(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    ties = []
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        if j > i:
            ties.append(j - i + 1)
        i = j + 1
    return ranks, ties


def mann_whitney(x, y):
    """Two-sided Mann-Whitney U test (normal approximation, tie and continuity corrected).

    Returns (U for x, p value, Cliff's delta). Cliff's delta is P(x > y) - P(x < y):
    +1 means every x is larger than every y.
    """
    n1, n2 = len(x), len(y)
    ranks, ties = rank_with_ties(list(x) + list(y))
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0
    n = n1 + n2
    mu = n1 * n2 / 2.0
    tie_term = sum(t ** 3 - t for t in ties) / float(n * (n - 1)) if n > 1 else 0
    var = n1 * n2 / 12.0 * ((n + 1) - tie_term)
    if var <= 0:
        p = 1.0
    else:
        z = (abs(u1 - mu) - 0.5) / math.sqrt(var)
        p = math.erfc(max(z, 0) / math.sqrt(2))
    delta = 2.0 * u1 / (n1 * n2) - 1.0
    return u1, min(1.0, p), delta


def spearman(xs, ys):
    rx, _ = rank_with_ties(xs)
    ry, _ = rank_with_ties(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def summarize(xs, n_boot, rng, conf):
    s = sorted(xs)
    m = percentile(s, 50)
    spread = mad(s)
    mean = sum(s) / len(s)
    sd = math.sqrt(sum((x - mean) ** 2 for x in s) / (len(s) - 1)) if len(s) > 1 else 0.0
    lo, hi = bootstrap(lambda a: median(a), [s], n_boot, rng, conf) if len(s) > 2 else (m, m)
    outliers = sum(1 for x in s if spread > 0 and abs(x - m) / spread > 5)
    return {
        "n": len(s), "median": m, "median_ci": [lo, hi], "mean": mean, "stdev": sd,
        "mad": spread, "cv_robust": spread / m if m else float("nan"),
        "min": s[0], "max": s[-1], "p5": percentile(s, 5), "p25": percentile(s, 25),
        "p75": percentile(s, 75), "p95": percentile(s, 95), "outliers_5mad": outliers,
    }

# ---------------------------------------------------------------- running


def run_once(cmd, use_shell, capture=False):
    """Run one command. Returns (wall_s, cpu_s, maxrss_bytes, exit_code, stdout_bytes, stderr_tail)."""
    argv = cmd if use_shell else shlex.split(cmd)
    err = tempfile.TemporaryFile()
    stdout = subprocess.PIPE if capture else subprocess.DEVNULL
    t0 = time.perf_counter()
    p = subprocess.Popen(argv, shell=use_shell, stdout=stdout, stderr=err,
                         stdin=subprocess.DEVNULL)
    out = p.stdout.read() if capture else b""
    _, status, ru = os.wait4(p.pid, 0)
    t1 = time.perf_counter()
    p.returncode = os.waitstatus_to_exitcode(status)
    if capture:
        p.stdout.close()
    rss = ru.ru_maxrss if platform.system() == "Darwin" else ru.ru_maxrss * 1024
    tail = b""
    if p.returncode != 0:
        err.seek(0)
        tail = err.read()[-2000:]
    err.close()
    return t1 - t0, ru.ru_utime + ru.ru_stime, rss, p.returncode, out, tail


def run_prepare(cmd, use_shell):
    if cmd:
        r = subprocess.run(cmd if use_shell else shlex.split(cmd), shell=use_shell,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0:
            sys.exit("prepare command failed (%d): %s\n%s" % (
                r.returncode, cmd, r.stderr.decode("utf-8", "replace")[-1000:]))

# ---------------------------------------------------------------- formatting


def fmt_t(s):
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return "n/a"
    a = abs(s)
    if a >= 1:
        return "%.3f s" % s
    if a >= 1e-3:
        return "%.2f ms" % (s * 1e3)
    if a >= 1e-6:
        return "%.1f us" % (s * 1e6)
    return "%.0f ns" % (s * 1e9)


def fmt_b(b):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(b) < 1024 or unit == "GiB":
            return "%.1f %s" % (b, unit) if unit != "B" else "%d B" % b
        b /= 1024.0

# ---------------------------------------------------------------- verdict


def compare(base, cand, base_name, cand_name, floor, args, rng):
    """Compare candidate times to baseline times. Ratio < 1 means candidate is faster."""
    a, b = base, cand
    if floor:
        def ratio(x, y, f):
            fm = median(f)
            den, num = median(x) - fm, median(y) - fm
            return num / den if den > 0 and num > 0 else None
        point = ratio(a, b, floor)
        lo, hi = bootstrap(ratio, [a, b, floor], args.bootstrap, rng, args.confidence)
    else:
        def ratio(x, y):
            return median(y) / median(x)
        point = ratio(a, b)
        lo, hi = bootstrap(ratio, [a, b], args.bootstrap, rng, args.confidence)
    _, p, delta = mann_whitney(b, a)  # delta > 0: candidate tends to be slower
    prob_faster = (1 - delta) / 2.0
    t = args.min_effect
    alpha = 1 - args.confidence
    pct = int(round(args.confidence * 100))
    ci_txt = "%d%% CI %.3f to %.3f" % (pct, lo, hi)
    if point is None or math.isnan(lo) or math.isnan(hi):
        verdict = ("%s vs %s: no stable ratio after subtracting the floor, because one of "
                   "them is not reliably slower than the floor command." % (cand_name, base_name))
        point = point if point is not None else float("nan")
        kind = "invalid"
    elif hi < 1 and p < alpha:
        sp, sp_lo, sp_hi = 1 / point, 1 / hi, 1 / lo
        kind = "faster" if hi < 1 - t else "faster-trivial"
        verdict = "%s is %.2fx faster than %s (%d%% CI %.2fx to %.2fx, Mann-Whitney p=%.2g, " \
                  "faster in %.0f%% of pairs)." % (cand_name, sp, base_name, pct, sp_lo, sp_hi, p,
                                                    100 * prob_faster)
        if kind == "faster-trivial":
            verdict += " The gain may be below the %.0f%% you said matters." % (100 * t)
    elif lo > 1 and p < alpha:
        kind = "slower" if lo > 1 + t else "slower-trivial"
        verdict = "%s is %.2fx slower than %s (%d%% CI %.2fx to %.2fx, Mann-Whitney p=%.2g)." % (
            cand_name, point, base_name, pct, lo, hi, p)
        if kind == "slower-trivial":
            verdict += " The loss may be below the %.0f%% you said matters." % (100 * t)
    elif lo >= 1 - t and hi <= 1 + t:
        kind = "equivalent"
        verdict = "%s and %s are the same speed within +/-%.0f%% (time ratio %.3f, %s)." % (
            cand_name, base_name, 100 * t, point, ci_txt)
    else:
        kind = "unclear"
        verdict = ("No clear difference between %s and %s: time ratio %.3f, %s includes 1.0 "
                   "or the test disagrees (p=%.2g). Do not claim a speedup. More runs or a "
                   "quieter machine may narrow it." % (cand_name, base_name, point, ci_txt, p))
    return {"baseline": base_name, "candidate": cand_name, "time_ratio": point,
            "ratio_ci": [lo, hi], "floor_subtracted": bool(floor), "mann_whitney_p": p,
            "cliffs_delta": delta, "prob_candidate_faster": prob_faster, "kind": kind,
            "verdict": verdict}

# ---------------------------------------------------------------- checks


def checks(name, rec, summ, floor_summ, floor_wall, args):
    w = []
    times = rec["wall"]
    n = len(times)
    if n >= 10:
        rho = spearman(rec["seq"], times)
        if abs(rho) >= 0.5:
            w.append("%s: times %s steadily during the run (Spearman rho=%.2f). Something "
                     "drifted (thermal throttling, caches, background work). Interleaving "
                     "keeps the comparison fair, but the absolute numbers are suspect." % (
                         name, "rise" if rho > 0 else "fall", rho))
        k = max(3, n // 4)
        first, last = times[:k], times[-k:]
        _, p, delta = mann_whitney(first, last)
        if p < 0.01 and delta > 0.5 and median(first) > 1.05 * median(last):
            w.append("%s: the first %d measured runs are %.0f%% slower than the last %d. "
                     "Either warmup was not enough (raise --warmup) or background load "
                     "dropped during the run." % (
                         name, k, 100 * (median(first) / median(last) - 1), k))
    if summ["cv_robust"] > 0.10:
        w.append("%s: robust spread is %.0f%% of the median. The machine is noisy or the "
                 "work is bimodal; look at the chart before trusting small differences." % (
                     name, 100 * summ["cv_robust"]))
    if summ["outliers_5mad"]:
        w.append("%s: %d run(s) were more than 5 MADs from the median. They are kept (they may "
                 "be real pauses such as GC); medians are not moved much by them." % (
                     name, summ["outliers_5mad"]))
    if floor_summ:
        _, p_floor, d_floor = mann_whitney(rec["wall"], floor_wall)
        clearly_slower = p_floor < 0.01 and d_floor > 0
        if not clearly_slower or summ["median"] < 1.05 * floor_summ["median"]:
            w.append("%s: median is within noise of the floor command (%s vs %s). The work "
                     "you meant to measure may not be running at all: check for unused "
                     "results, lazy evaluation, dead code elimination or constant folding." % (
                         name, fmt_t(summ["median"]), fmt_t(floor_summ["median"])))
    if summ["median"] < 0.005:
        w.append("%s: median is under 5 ms. Process startup and timer overhead are a large "
                 "share; prefer an in-process tool (pyperf, criterion, go test -bench)." % name)
    return w

# ---------------------------------------------------------------- reports


def svg_chart(names, data, unit_s, path, title):
    """Dot plot with box (IQR) and median line per command. Stdlib SVG."""
    W, row_h, left, right, top = 840, 64, 170, 50, 50
    H = top + row_h * len(names) + 50
    allv = [v for xs in data for v in xs]
    lo, hi = min(allv), max(allv)
    pad = (hi - lo) * 0.06 or hi * 0.05 or 1e-9
    lo, hi = max(0, lo - pad), hi + pad
    sx = lambda v: left + (W - left - right) * (v - lo) / (hi - lo)
    scale, unit = (1, "s") if unit_s >= 1 else (1e3, "ms") if unit_s >= 1e-3 else (1e6, "us")
    colors = ["#2a6fdb", "#d1495b", "#2e933c", "#edae49", "#7a5195", "#00798c"]
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'font-family="Helvetica, Arial, sans-serif" font-size="13">' % (W, H),
           '<rect width="100%" height="100%" fill="#ffffff"/>',
           '<text x="%d" y="28" font-size="15" font-weight="bold" fill="#222">%s</text>' % (
               left, _esc(title))]
    rng = random.Random(1)
    for i, (name, xs) in enumerate(zip(names, data)):
        cy = top + row_h * i + row_h / 2
        c = colors[i % len(colors)]
        s = sorted(xs)
        q1, q2, q3 = percentile(s, 25), percentile(s, 50), percentile(s, 75)
        out.append('<text x="%d" y="%.1f" text-anchor="end" fill="#222">%s</text>' % (
            left - 12, cy + 4, _esc(name[:22])))
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="30" fill="%s" '
                   'fill-opacity="0.15" stroke="%s"/>' % (sx(q1), cy - 15, max(1, sx(q3) - sx(q1)), c, c))
        out.append('<line x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f" stroke="%s" stroke-width="3"/>' % (
            sx(q2), sx(q2), cy - 18, cy + 18, c))
        for v in xs:
            out.append('<circle cx="%.1f" cy="%.1f" r="2.6" fill="%s" fill-opacity="0.65"/>' % (
                sx(v), cy + rng.uniform(-11, 11), c))
    ay = top + row_h * len(names) + 8
    out.append('<line x1="%d" x2="%d" y1="%d" y2="%d" stroke="#888"/>' % (left, W - right, ay, ay))
    for k in range(6):
        v = lo + (hi - lo) * k / 5
        x = sx(v)
        out.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="#888"/>' % (x, x, ay, ay + 5))
        sv = v * scale
        out.append('<text x="%.1f" y="%d" text-anchor="middle" fill="#555">%s</text>' % (
            x, ay + 20, ("%.0f" % sv) if abs(sv) >= 100 else ("%.3g" % sv)))
    out.append('<text x="%d" y="%d" text-anchor="middle" fill="#555">wall time (%s); box = middle '
               'half of runs, thick line = median, dots = every run</text>' % (
                   (left + W - right) / 2, ay + 40, unit))
    out.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(out))


def png_chart(names, data, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 1 + 0.8 * len(names)))
    ax.boxplot(data, vert=False, labels=names, showfliers=False)
    rng = random.Random(1)
    for i, xs in enumerate(data):
        ax.scatter(xs, [i + 1 + rng.uniform(-0.15, 0.15) for _ in xs], s=8, alpha=0.6)
    ax.set_xlabel("wall time (s)")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=150)


def _clean(o):
    """Replace NaN with None so the JSON is standard."""
    if isinstance(o, float) and math.isnan(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown(result):
    a = result["settings"]
    L = ["# Benchmark report", ""]
    for c in result["comparisons"]:
        L.append("**Verdict:** " + c["verdict"])
        L.append("")
    if result["warnings"]:
        L += ["## Warnings", ""] + ["- " + w for w in result["warnings"]] + [""]
    L += ["## Results (wall time)", "",
          "| Command | Median | %d%% CI of median | p5 | p95 | Min | Max | MAD | CPU time (median) | Peak RSS (median) |" % int(100 * a["confidence"]),
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in result["commands"]:
        s = r["wall_stats"]
        L.append("| `%s` | %s | %s to %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["name"], fmt_t(s["median"]), fmt_t(s["median_ci"][0]), fmt_t(s["median_ci"][1]),
            fmt_t(s["p5"]), fmt_t(s["p95"]), fmt_t(s["min"]), fmt_t(s["max"]), fmt_t(s["mad"]),
            fmt_t(r["cpu_median"]), fmt_b(r["rss_median"])))
    if result.get("floor"):
        s = result["floor"]["wall_stats"]
        L.append("| floor: `%s` | %s | %s to %s | %s | %s | %s | %s | %s | | |" % (
            result["floor"]["command"], fmt_t(s["median"]), fmt_t(s["median_ci"][0]),
            fmt_t(s["median_ci"][1]), fmt_t(s["p5"]), fmt_t(s["p95"]), fmt_t(s["min"]),
            fmt_t(s["max"]), fmt_t(s["mad"])))
    if result["comparisons"]:
        L += ["", "## Comparisons", "",
              "| Candidate vs baseline | Time ratio | CI | Mann-Whitney p | Cliff's delta | P(candidate faster) | Floor subtracted |",
              "|---|---|---|---|---|---|---|"]
        for c in result["comparisons"]:
            L.append("| %s vs %s | %s | %.3f to %.3f | %.3g | %.2f | %.0f%% | %s |" % (
                c["candidate"], c["baseline"],
                "%.3f" % c["time_ratio"] if c["time_ratio"] is not None else "n/a",
                c["ratio_ci"][0], c["ratio_ci"][1], c["mann_whitney_p"], c["cliffs_delta"],
                100 * c["prob_candidate_faster"], "yes" if c["floor_subtracted"] else "no"))
        L += ["", "Time ratio = candidate median / baseline median. Below 1.0 means the "
              "candidate is faster. CI is a percentile bootstrap (%d resamples)." % a["bootstrap"]]
    L += ["", "## Commands", ""]
    for r in result["commands"]:
        L.append("- `%s`: `%s`" % (r["name"], r["command"]))
    L += ["", "## Method", "",
          "%d warmup and %d measured runs per command, order: %s, seed %d%s." % (
              a["warmup"], a["runs"], a["order"], a["seed"],
              ", prepare before each run: `%s`" % a["prepare"] if a["prepare"] else ""),
          "", "## Reproduce", "", "```", result["reproduce"], "```"]
    if result.get("environment_md"):
        L += ["", "## Environment", "", result["environment_md"]]
    return "\n".join(L) + "\n"

# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("commands", nargs="+", help="commands to benchmark; the first is the baseline")
    ap.add_argument("-n", "--name", action="append", default=[], help="name for each command, in order")
    ap.add_argument("-r", "--runs", type=int, default=30, help="measured runs per command (default 30)")
    ap.add_argument("-w", "--warmup", type=int, default=3, help="unmeasured warmup runs per command (default 3)")
    ap.add_argument("--order", choices=["shuffled", "roundrobin", "sequential"], default="shuffled",
                    help="shuffled rounds (default), fixed round-robin, or all runs of one "
                         "command then the next (sequential; only to show why it is a bad idea)")
    ap.add_argument("--prepare", help="command run before every measured run, not timed (e.g. evict a cache)")
    ap.add_argument("--floor", help="an 'empty' version of the program (e.g. \"python3 -c pass\"), "
                                    "measured the same way, to expose startup cost and work that never ran")
    ap.add_argument("--subtract-floor", action="store_true",
                    help="compare (median - floor median) instead of raw medians")
    ap.add_argument("--same-output", action="store_true",
                    help="capture stdout of each command once and warn if they differ (catches unequal work)")
    ap.add_argument("--shell", action="store_true", help="run commands through /bin/sh -c")
    ap.add_argument("--confidence", type=float, default=0.95)
    ap.add_argument("--bootstrap", type=int, default=5000, help="bootstrap resamples (default 5000)")
    ap.add_argument("--min-effect", type=float, default=0.02,
                    help="smallest relative difference that matters to you (default 0.02 = 2%%)")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--json", help="write full results (every sample) as JSON")
    ap.add_argument("--md", help="write a markdown report")
    ap.add_argument("--chart", help="write a chart: .svg (no dependencies) or .png (needs matplotlib)")
    ap.add_argument("--no-env", action="store_true", help="skip the environment capture")
    ap.add_argument("--redact", action="store_true", help="hide hostname and username in the environment section")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    cmds = args.commands
    names = args.name + [None] * (len(cmds) - len(args.name))
    names = [n or ("cmd%d" % (i + 1)) for i, n in enumerate(names)]
    if len(set(names)) != len(names):
        sys.exit("command names must be unique")
    if args.runs < 5:
        sys.exit("--runs must be at least 5; 20 or more is better")
    rng = random.Random(args.seed)
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr, flush=True))

    env = None
    if not args.no_env:
        try:
            import env_report
            env = env_report.collect(redact=args.redact)
        except Exception as e:  # never fail a benchmark over the environment report
            log("environment capture failed: %s" % e)

    all_names = names + (["__floor__"] if args.floor else [])
    all_cmds = cmds + ([args.floor] if args.floor else [])
    recs = {n: {"wall": [], "cpu": [], "rss": [], "seq": []} for n in all_names}
    outputs = {}

    # Warmup, interleaved. The first warmup also captures output for --same-output.
    log("warmup: %d run(s) per command" % args.warmup)
    for w in range(max(args.warmup, 1 if args.same_output else 0)):
        order = list(range(len(all_cmds)))
        rng.shuffle(order)
        for i in order:
            run_prepare(args.prepare, args.shell)
            wall, cpu, rss, code, out, tail = run_once(all_cmds[i], args.shell,
                                                       capture=(args.same_output and w == 0))
            if code != 0:
                sys.exit("command failed with exit code %d: %s\n%s" % (
                    code, all_cmds[i], tail.decode("utf-8", "replace")))
            if args.same_output and w == 0:
                outputs[all_names[i]] = hashlib.sha256(out).hexdigest()

    # Build the measured schedule.
    idx = list(range(len(all_cmds)))
    if args.order == "sequential":
        schedule = [i for i in idx for _ in range(args.runs)]
    else:
        schedule = []
        for _ in range(args.runs):
            r = idx[:]
            if args.order == "shuffled":
                rng.shuffle(r)
            schedule += r
    log("measuring: %d runs per command, %s order" % (args.runs, args.order))
    t_start = time.time()
    for seq, i in enumerate(schedule):
        run_prepare(args.prepare, args.shell)
        wall, cpu, rss, code, _, tail = run_once(all_cmds[i], args.shell)
        if code != 0:
            sys.exit("command failed with exit code %d: %s\n%s" % (
                code, all_cmds[i], tail.decode("utf-8", "replace")))
        rec = recs[all_names[i]]
        rec["wall"].append(wall)
        rec["cpu"].append(cpu)
        rec["rss"].append(rss)
        rec["seq"].append(seq)
        if not args.quiet and (seq + 1) % max(1, len(schedule) // 10) == 0:
            log("  %d/%d" % (seq + 1, len(schedule)))
    elapsed = time.time() - t_start

    summ = {n: summarize(recs[n]["wall"], args.bootstrap, rng, args.confidence) for n in all_names}
    floor_s = summ.get("__floor__")
    warnings = []
    if env:
        warnings += ["environment: " + w for w in env.get("warnings", [])]
    for n in names:
        warnings += checks(n, recs[n], summ[n], floor_s,
                           recs["__floor__"]["wall"] if args.floor else None, args)
    if args.same_output:
        hashes = {outputs[n] for n in names}
        if len(hashes) > 1:
            groups = {}
            for n in names:
                groups.setdefault(outputs[n][:12], []).append(n)
            warnings.append("Commands printed different output (%s). They may not be doing the "
                            "same work, so their times are not comparable." % "; ".join(
                                "%s: %s" % (h, ", ".join(v)) for h, v in groups.items()))
    if args.order == "sequential":
        warnings.append("Sequential order: each command ran in its own block of time, so any "
                        "drift or cache state left by the previous command is mixed into the "
                        "comparison. Use the default shuffled order for real results.")

    comparisons = []
    for n in names[1:]:
        comparisons.append(compare(recs[names[0]]["wall"], recs[n]["wall"], names[0], n,
                                   recs["__floor__"]["wall"] if (args.floor and args.subtract_floor) else None,
                                   args, rng))

    # A comparison with a variant that did no work, or different work, is not a result.
    suspect = {}
    if floor_s:
        for n in names:
            if any(w.startswith(n + ": median is within noise of the floor") for w in warnings):
                suspect[n] = "%s is no slower than the empty floor command, so its work " \
                             "probably never ran" % n
    if args.same_output:
        for n in names[1:]:
            if outputs[n] != outputs[names[0]]:
                suspect.setdefault(n, "%s printed different output from %s, so they did "
                                      "not do the same work" % (n, names[0]))
    for c in comparisons:
        reasons = [suspect[x] for x in (c["baseline"], c["candidate"]) if x in suspect]
        if reasons:
            c["raw_verdict"] = c["verdict"]
            c["kind"] = "invalid"
            c["verdict"] = "NOT A VALID COMPARISON: %s. (The raw numbers said: %s)" % (
                "; ".join(reasons), c["raw_verdict"])

    argv = [os.path.basename(sys.argv[0])] + sys.argv[1:]
    result = {
        "tool": "bench.py",
        "settings": {"runs": args.runs, "warmup": args.warmup, "order": args.order,
                     "prepare": args.prepare, "floor": args.floor, "seed": args.seed,
                     "confidence": args.confidence, "bootstrap": args.bootstrap,
                     "min_effect": args.min_effect, "shell": args.shell,
                     "timer": "time.perf_counter (resolution %.0f ns)" % (
                         time.get_clock_info("perf_counter").resolution * 1e9)},
        "elapsed_s": elapsed,
        "commands": [{
            "name": n, "command": c, "wall_stats": summ[n],
            "cpu_median": median(recs[n]["cpu"]), "rss_median": median(recs[n]["rss"]),
            "samples": recs[n],
        } for n, c in zip(names, cmds)],
        "floor": ({"command": args.floor, "wall_stats": floor_s, "samples": recs["__floor__"]}
                  if args.floor else None),
        "comparisons": comparisons,
        "warnings": warnings,
        "reproduce": "python3 " + " ".join(shlex.quote(x) for x in argv),
        "environment": env,
    }
    if env:
        import env_report
        result["environment_md"] = env_report.to_markdown(env)

    # Console summary.
    print()
    w = max(len(n) for n in all_names)
    for n in all_names:
        s = summ[n]
        label = "floor" if n == "__floor__" else n
        print("%-*s  median %-10s  CI %s to %s  p5 %-10s p95 %-10s MAD %s" % (
            w, label, fmt_t(s["median"]), fmt_t(s["median_ci"][0]), fmt_t(s["median_ci"][1]),
            fmt_t(s["p5"]), fmt_t(s["p95"]), fmt_t(s["mad"])))
    if warnings:
        print("\nWARNINGS")
        for x in warnings:
            print("  - " + x)
    if comparisons:
        print("\nVERDICT")
        for c in comparisons:
            print("  " + c["verdict"])

    if args.json:
        with open(args.json, "w") as f:
            json.dump(_clean(result), f, indent=2)
    if args.md:
        with open(args.md, "w") as f:
            f.write(markdown(result))
    if args.chart:
        data = [recs[n]["wall"] for n in all_names]
        labels = [("floor" if n == "__floor__" else n) for n in all_names]
        title = "%d runs each, %s order" % (args.runs, args.order)
        path = args.chart
        if path.endswith(".png"):
            try:
                png_chart(labels, data, path, title)
            except ImportError:
                path = path[:-4] + ".svg"
                log("matplotlib not installed; writing %s instead" % path)
                svg_chart(labels, data, median(data[0]), path, title)
        else:
            svg_chart(labels, data, median(data[0]), path, title)


if __name__ == "__main__":
    main()
