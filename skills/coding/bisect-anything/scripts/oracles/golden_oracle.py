#!/usr/bin/env python3
"""Pass/fail oracle for "the output changed".

Runs a command and compares its output with a saved "golden" copy, after
blanking out the parts that change on every run or every commit (timestamps,
versions, temp paths, ids). Without that, every commit looks bad.

Record the golden output on a good commit:
  golden_oracle.py --record --golden /tmp/golden.txt --cmd "./report data.csv" --ignore "generated at .*"
Then bisect with the same --ignore flags:
  golden_oracle.py --golden /tmp/golden.txt --cmd "./report data.csv" --ignore "generated at .*"

Exit codes: 0 same, 1 different (prints the first differences), 125 the
command could not run at all (exit 127, command not found).
"""
import argparse
import difflib
import re
import subprocess
import sys


def normalise(text, a):
    text = text.replace("\r\n", "\n")
    for pat in a.ignore:
        text = re.sub(pat, "<IGNORED>", text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    if a.sort_lines:
        lines.sort()
    while lines and not lines[-1]:
        lines.pop()
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cmd", required=True)
    ap.add_argument("--golden", required=True, help="file with the expected output")
    ap.add_argument("--record", action="store_true", help="write the golden file from this run")
    ap.add_argument("--ignore", action="append", default=[], help="regex to blank out (repeatable)")
    ap.add_argument("--stderr", action="store_true", help="compare stdout and stderr together")
    ap.add_argument("--sort-lines", action="store_true", help="ignore line order (hash maps, parallel output)")
    ap.add_argument("--any-exit", action="store_true", help="do not treat a non-zero exit as bad")
    ap.add_argument("--timeout", type=float)
    a = ap.parse_args()

    p = subprocess.run(a.cmd, shell=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT if a.stderr else subprocess.PIPE, timeout=a.timeout)
    if p.returncode == 127:
        print("golden: command not found at this commit, skipping", file=sys.stderr)
        sys.exit(125)
    out = p.stdout.decode("utf-8", "replace")
    if a.record:
        with open(a.golden, "w") as fh:
            fh.write(out)
        print("golden: recorded %d lines to %s" % (out.count("\n"), a.golden))
        sys.exit(0)
    with open(a.golden) as fh:
        want = normalise(fh.read(), a)
    got = normalise(out, a)
    if p.returncode != 0 and not a.any_exit:
        print("golden: command exited %d" % p.returncode, file=sys.stderr)
        sys.exit(1)
    if got == want:
        sys.exit(0)
    diff = list(difflib.unified_diff(want, got, "golden", "this commit", lineterm="", n=1))
    print("\n".join(diff[:40]), file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
