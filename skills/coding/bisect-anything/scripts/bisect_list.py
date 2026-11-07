#!/usr/bin/env python3
"""Bisect anything that is not a git history: dependency versions, nightly
builds, config values, dates, data files, line counts.

Ordered mode (default): the candidates are in order, the first one is good and
the last one is bad, and somewhere in between it flips once. Finds the first
bad candidate in about log2(N) oracle runs.

  bisect_list.py --items-file versions.txt \\
      --oracle 'pip install -q "somelib=={}" && python repro.py'

Minimise mode (--minimise): the candidates are a set, not a line (flags,
input records, plugins). Finds the smallest subset that still fails, so you
learn that "flags A and C together" break it.

  bisect_list.py --minimise --items-file flags.txt --oracle './app --flags-file {}'

In the oracle, {} is replaced by the shell-quoted item (ordered mode) or the
path of a file holding the chosen items, one per line (minimise mode). The
same values are in $BISECT_ITEM and $BISECT_ITEMS_FILE.

Oracle exit codes: 0 good, 125 cannot test this one, anything else bad.
Standard library only, Python 3.9+.
"""
import os
import sys

# This folder holds a file named bisect.py, which would shadow the standard
# library's bisect module (used by random and tempfile). Look here last.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE] + [_HERE]

import argparse
import shlex
import tempfile

from _common import BAD, GOOD, SKIP, ddmin, log, summarise, vote  # noqa: E402


def load_items(a):
    if a.items:
        return list(a.items)
    if a.items_file:
        with open(a.items_file) as fh:
            return [ln.rstrip("\n") for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    if a.range:
        lo, hi = a.range
        return [str(i) for i in range(lo, hi + 1, a.step)]
    sys.exit("bisect_list.py: give --items, --items-file or --range")


def make_tester(a):
    cache = {}

    def test(label, cmd, extra_env, key=None):
        key = key or cmd
        if key in cache:
            return cache[key]
        env = dict(os.environ, **extra_env)
        verdict, runs = vote(cmd, a.cwd, env, a.repeats, a.vote, a.timeout, a.timeout_means)
        if a.invert and verdict != SKIP:
            verdict = BAD if verdict == GOOD else GOOD
        log("[bisect_list] %-30s %-5s %s" % (label[:30], verdict, summarise(runs)))
        if verdict != GOOD and a.show_output and runs:
            log(runs[-1]["tail"])
        cache[key] = verdict
        return verdict

    return test


def ordered(a, items):
    test = make_tester(a)

    def check(i):
        cmd = a.oracle.replace("{}", shlex.quote(items[i]))
        return test(items[i], cmd, {"BISECT_ITEM": items[i], "BISECT_INDEX": str(i)})

    good_word, bad_word = ("bad", "good") if a.invert else ("good", "bad")
    n = len(items)
    if n < 2:
        sys.exit("bisect_list.py: need at least two items")
    if not a.no_verify:
        if check(0) != GOOD:
            sys.exit("bisect_list.py: the first item %r is not %s. Fix the oracle or the list." % (items[0], good_word))
        if check(n - 1) != BAD:
            sys.exit("bisect_list.py: the last item %r is not %s. Fix the oracle or the list." % (items[-1], bad_word))
    lo, hi, skipped, tests = 0, n - 1, set(), 0
    while True:
        cands = [i for i in range(lo + 1, hi) if i not in skipped]
        if not cands:
            break
        mid = (lo + hi) / 2.0
        c = min(cands, key=lambda i: (abs(i - mid), i))
        v = check(c)
        tests += 1
        if v == GOOD:
            lo = c
        elif v == BAD:
            hi = c
        else:
            skipped.add(c)
    print("")
    between = [items[i] for i in range(lo + 1, hi)]
    if between:
        print("NOT CONCLUSIVE: last %s is %r; the first %s is one of %s (these could not be tested)."
              % (good_word, items[lo], bad_word, between + [items[hi]]))
        return 2
    print("LAST %s:  %s" % (good_word.upper(), items[lo]))
    print("FIRST %s: %s" % (bad_word.upper(), items[hi]))
    print("(%d tests in the middle, %d items)" % (tests, n))
    return 0


def minimise(a, items):
    test = make_tester(a)
    tmpdir = tempfile.mkdtemp(prefix="bisect-list-")
    counter = [0]

    def fails(subset):
        counter[0] += 1
        path = os.path.join(tmpdir, "items-%d.txt" % counter[0])
        with open(path, "w") as fh:
            fh.write("".join(x + "\n" for x in subset))
        cmd = a.oracle.replace("{}", shlex.quote(path))
        label = "%d items" % len(subset)
        return test(label, cmd, {"BISECT_ITEMS_FILE": path}, key=("subset",) + tuple(subset)) == BAD

    if not a.no_verify:
        if not fails(items):
            sys.exit("bisect_list.py: the full set does not fail, nothing to minimise.")
        if fails([]):
            sys.exit("bisect_list.py: the empty set already fails; the problem is not in these items.")
    result, n_tests = ddmin(items, fails)
    print("")
    print("SMALLEST FAILING SUBSET (%d of %d items, %d tests):" % (len(result), len(items), n_tests))
    for x in result:
        print("  " + x)
    print("Removing any one of these makes it pass.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--items", nargs="+", help="the candidates, in order")
    src.add_argument("--items-file", help="one candidate per line (# comments ignored)")
    src.add_argument("--range", nargs=2, type=int, metavar=("FIRST", "LAST"), help="integers FIRST..LAST")
    ap.add_argument("--step", type=int, default=1, help="step for --range")
    ap.add_argument("--oracle", required=True, help="shell command; {} is the item (or the subset file)")
    ap.add_argument("--cwd", default=".", help="folder to run the oracle in")
    ap.add_argument("--minimise", "--minimize", action="store_true", help="find the smallest failing subset")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--vote", choices=["majority", "any-bad"], default="majority")
    ap.add_argument("--timeout", type=float)
    ap.add_argument("--timeout-means", choices=[BAD, SKIP], default=BAD)
    ap.add_argument("--invert", action="store_true", help="find the first item where the oracle PASSES")
    ap.add_argument("--no-verify", action="store_true", help="do not test both ends first")
    ap.add_argument("--show-output", action="store_true", help="print the oracle output for bad results")
    a = ap.parse_args()
    a.repeats = max(1, a.repeats)
    items = load_items(a)
    sys.exit(minimise(a, items) if a.minimise else ordered(a, items))


if __name__ == "__main__":
    main()
