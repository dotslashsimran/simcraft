#!/usr/bin/env python3
"""Find the test that breaks (or secretly props up) a victim test.

Give it the ordered list of tests that ran before the victim in a failing
run, the victim's id, and a command template that runs a list of tests in
the given order. It confirms the victim passes alone and fails after the
others, then shrinks the list with delta debugging (binary search that also
handles polluters that only bite in combination) until it is minimal.

If the victim instead FAILS alone and PASSES after the others, it searches
for the test the victim depends on (a "state setter"), which is the same bug
seen from the other side.

Python 3.9+ standard library only.

Command template placeholders:
  {tests}       the test ids, shell-quoted, space separated, victim last
  {tests_file}  path to a file with one test id per line, victim last

Examples:
  # unittest, ids from a failing shuffled run
  find_polluter.py --tests order.txt --victim tests.test_b.T.test_x \\
      --cmd "python3 -m unittest {tests}"
  # pytest (add -p no:randomly so pytest keeps your order)
  find_polluter.py --tests order.txt --victim "tests/test_b.py::test_x" \\
      --cmd "python3 -m pytest -p no:randomly -q {tests}"
  # jest, file level: run files in band in the listed order
  find_polluter.py --tests files.txt --victim src/b.test.js \\
      --cmd "npx jest --runInBand --runTestsByPath {tests}"

Flaky probes: --repeat K runs every probe K times and counts it as failing
if any run fails. Use it when the victim does not fail every time.
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time

LOG = []


def read_tests(path):
    src = sys.stdin if path == "-" else open(path)
    out = []
    for line in src:
        line = line.strip()
        if line and not line.startswith("#") and line not in out:
            out.append(line)
    return out


class Prober:
    def __init__(self, args):
        self.args = args
        self.cache = {}
        self.calls = 0
        self.runs = 0

    def run(self, tests):
        """Return True if the victim failed in this ordering."""
        a = self.args
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("\n".join(tests) + "\n")
            listfile = fh.name
        cmd = a.cmd.replace("{tests_file}", shlex.quote(listfile)) \
                   .replace("{tests}", " ".join(shlex.quote(t) for t in tests))
        self.runs += 1
        try:
            p = subprocess.run(cmd, shell=True, cwd=a.cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=a.timeout)
            out = p.stdout.decode("utf-8", "replace")
            failed = p.returncode != 0
            if a.fail_pattern:
                failed = re.search(a.fail_pattern, out, re.M) is not None
        except subprocess.TimeoutExpired:
            out, failed = "timeout", True
        finally:
            os.unlink(listfile)
        if a.verbose:
            sys.stderr.write("    $ %s -> %s\n" % (cmd[:160], "FAIL" if failed else "pass"))
        return failed, out

    def victim_fails(self, subset, repeat=None, use_cache=True):
        """Victim fails after `subset` (in original order) on any of K repeats."""
        key = tuple(subset)
        if use_cache and key in self.cache:
            return self.cache[key]
        self.calls += 1
        k = repeat or self.args.repeat
        failed = False
        for _ in range(k):
            f, _ = self.run(list(subset) + [self.args.victim])
            if f:
                failed = True
                break
        self.cache[key] = failed
        LOG.append((len(subset), failed))
        sys.stderr.write("  probe %3d: %4d tests before victim -> victim %s\n" % (
            self.calls, len(subset), "FAILS" if failed else "passes"))
        return failed


def split(items, n):
    size, rem = divmod(len(items), n)
    out, start = [], 0
    for i in range(n):
        end = start + size + (1 if i < rem else 0)
        if end > start:
            out.append(items[start:end])
        start = end
    return out


def ddmin(items, interesting):
    """Zeller's delta debugging. Returns a 1-minimal interesting subset."""
    n = 2
    while len(items) >= 2:
        chunks = split(items, n)
        reduced = False
        for c in chunks:
            if interesting(c):
                items, n, reduced = c, 2, True
                break
        if not reduced and n > 2:
            for c in chunks:
                cs = set(c)
                comp = [x for x in items if x not in cs]
                if interesting(comp):
                    items, n, reduced = comp, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(items):
                break
            n = min(len(items), n * 2)
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tests", required=True, help="file with test ids in run order (or - for stdin)")
    ap.add_argument("--victim", required=True)
    ap.add_argument("--cmd", required=True, help="command template with {tests} or {tests_file}")
    ap.add_argument("--all", action="store_true",
                    help="search every listed test, not only those before the victim")
    ap.add_argument("--repeat", type=int, default=1, help="runs per probe (any failure = fail)")
    ap.add_argument("--confirm", type=int, default=3, help="runs used for the opening checks")
    ap.add_argument("--fail-pattern", help="regex that marks the VICTIM as failed (default: nonzero exit)")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--cwd")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    tests = read_tests(args.tests)
    if args.victim in tests and not args.all:
        cands = tests[:tests.index(args.victim)]
    else:
        cands = [t for t in tests if t != args.victim]
    if not cands:
        print("no tests run before the victim; nothing to search")
        return 2
    pr = Prober(args)
    t0 = time.time()

    print("victim: %s\ncandidates: %d tests\n" % (args.victim, len(cands)))
    print("step 1: victim alone (%d runs)" % args.confirm)
    alone_fail = pr.victim_fails([], repeat=args.confirm)
    print("step 2: all candidates then victim (%d runs)" % args.confirm)
    full_fail = pr.victim_fails(cands, repeat=args.confirm)

    if alone_fail and full_fail:
        print("\nThe victim fails alone too. This is not test pollution: the victim is flaky or broken")
        print("on its own. Measure it alone with flake_rate.py and look at time, randomness and races.")
        return 3
    if not alone_fail and not full_fail:
        print("\nCould not reproduce: the victim passed both alone and after all %d candidates." % len(cands))
        print("Try --repeat 5, check the order really matches the failing run, or include")
        print("tests from other workers/files (--all). If it only fails in parallel, the shared")
        print("thing is a resource (port, file, database), not in-process state.")
        return 4

    if not alone_fail and full_fail:
        mode = "polluter"
        interesting = lambda s: pr.victim_fails(s)
        print("\nvictim passes alone and fails after the others: searching for the POLLUTER\n")
    else:
        mode = "setter"
        interesting = lambda s: not pr.victim_fails(s)
        print("\nvictim FAILS alone and passes after the others: searching for the STATE SETTER")
        print("it depends on (the victim is missing its own setup)\n")

    found = ddmin(list(cands), interesting)

    # verify the final answer with extra runs
    verify_fail = pr.victim_fails(found, repeat=max(args.confirm, args.repeat), use_cache=False)
    ok = verify_fail if mode == "polluter" else not verify_fail

    print("\n== result (%d probes, %d runs, %.0fs)" % (pr.calls, pr.runs, time.time() - t0))
    label = "polluter" if mode == "polluter" else "state setter"
    if len(found) == 1:
        print("%s: %s" % (label, found[0]))
    else:
        print("minimal set of %d tests that together act as the %s (order matters):" % (len(found), label))
        for t in found:
            print("  " + t)
    print("victim:   %s" % args.victim)
    print("verified: %s" % ("yes" if ok else "NO, result unstable; rerun with a higher --repeat"))
    seq = found + [args.victim]
    print("\nreproduce with:\n  " + args.cmd.replace("{tests}", " ".join(shlex.quote(t) for t in seq))
          .replace("{tests_file}", "<file listing: %s>" % ", ".join(seq)))
    if mode == "polluter":
        print("\nnext: find what the polluter changes and does not restore (global/module state, env vars,")
        print("singletons, caches, mocks left installed, files, DB rows, cwd, locale, timezone).")
        print("Fix the polluter to clean up AND make the victim set up the state it needs.")
    else:
        print("\nnext: the victim relies on setup done by the setter. Give the victim its own fixture.")
    return 0 if ok else 5


if __name__ == "__main__":
    sys.exit(main())
