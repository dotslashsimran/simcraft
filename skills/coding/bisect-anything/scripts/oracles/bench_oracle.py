#!/usr/bin/env python3
"""Pass/fail oracle for "it got slower".

Runs a command several times, takes the median, and compares it with a
threshold. Medians ignore the odd slow run caused by the machine doing
something else. Results too close to the threshold return 125 (skip) instead
of guessing, because a wrong guess sends the whole bisect the wrong way.

Step 1, measure both ends (prints median and spread, always exits 0):
  bench_oracle.py --measure --cmd "python bench.py"
Step 2, bisect with a threshold between the two medians:
  bench_oracle.py --cmd "python bench.py" --threshold 0.35

Or let it pick: --good-median 0.12 --bad-median 0.80 uses the geometric mean.

--metric cpu measures user+system CPU time of the command and its children,
which is much less noisy than wall time on a busy laptop. Use wall time when
the slowdown is waiting (sleep, network, locks), because CPU time misses that.

Exit codes: 0 fast enough, 1 too slow, 125 cannot tell or the command failed.
"""
import argparse
import math
import resource
import statistics
import subprocess
import sys
import time


def measure_once(cmd, metric, timeout):
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    p = subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
    wall = time.perf_counter() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    if p.returncode != 0:
        sys.stderr.write(p.stderr.decode("utf-8", "replace")[-2000:])
        return None
    return cpu if metric == "cpu" else wall


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cmd", required=True)
    ap.add_argument("--repeats", type=int, default=5, help="timed runs (default 5; use more when noisy)")
    ap.add_argument("--warmup", type=int, default=1, help="untimed runs first, to fill caches")
    ap.add_argument("--metric", choices=["wall", "cpu"], default="wall")
    ap.add_argument("--threshold", type=float, help="seconds; median above this is bad")
    ap.add_argument("--good-median", type=float, help="median measured on the good end")
    ap.add_argument("--bad-median", type=float, help="median measured on the bad end")
    ap.add_argument("--band", type=float, default=0.10,
                    help="medians within this fraction of the threshold return 125 (default 0.10)")
    ap.add_argument("--timeout", type=float, help="seconds per run; a timeout counts as too slow")
    ap.add_argument("--measure", action="store_true", help="only print the numbers")
    a = ap.parse_args()

    threshold = a.threshold
    if threshold is None and a.good_median and a.bad_median:
        if a.bad_median < a.good_median * 1.2:
            sys.exit("bench_oracle.py: good and bad medians are less than 20% apart; "
                     "a bisect on this is mostly noise. Make the benchmark target the slow path.")
        threshold = math.sqrt(a.good_median * a.bad_median)
    if threshold is None and not a.measure:
        sys.exit("bench_oracle.py: give --threshold, or --good-median and --bad-median, or --measure")

    try:
        for _ in range(a.warmup):
            if measure_once(a.cmd, a.metric, a.timeout) is None:
                print("bench: command failed, cannot measure", file=sys.stderr)
                sys.exit(125)
        times = []
        for _ in range(a.repeats):
            t = measure_once(a.cmd, a.metric, a.timeout)
            if t is None:
                print("bench: command failed, cannot measure", file=sys.stderr)
                sys.exit(125)
            times.append(t)
    except subprocess.TimeoutExpired:
        print("bench: timed out, counting as slow", file=sys.stderr)
        sys.exit(1)

    med = statistics.median(times)
    mad = statistics.median([abs(t - med) for t in times])
    spread = mad / med if med else 0.0
    line = "bench: median %.4fs  min %.4fs  max %.4fs  spread(MAD) %.1f%%  n=%d  metric=%s" % (
        med, min(times), max(times), 100 * spread, len(times), a.metric)
    if a.measure:
        print(line)
        if spread > 0.10:
            print("bench: the spread is high; raise --repeats, close other apps, or try --metric cpu")
        sys.exit(0)
    print(line + "  threshold %.4fs" % threshold, file=sys.stderr)
    if med > threshold * (1 + a.band):
        sys.exit(1)
    if med < threshold * (1 - a.band):
        sys.exit(0)
    print("bench: too close to the threshold to call, skipping", file=sys.stderr)
    sys.exit(125)


if __name__ == "__main__":
    main()
