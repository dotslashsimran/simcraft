#!/usr/bin/env python3
"""End-to-end test: build the demo repo, then find every planted regression
with the real scripts and check the answers.

  python3 selftest.py            # uses a new temp folder
  python3 selftest.py --keep     # leave the temp folder for poking around

Takes about one to two minutes. Standard library only.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
ORACLES = os.path.join(SCRIPTS, "oracles")
PY = sys.executable

TEST_MEDIAN = '''import unittest
import mathlib


class TestMedian(unittest.TestCase):
    def test_even(self):
        self.assertEqual(mathlib.median([1, 2, 3, 4]), 2.5)

    def test_odd(self):
        self.assertEqual(mathlib.median([3, 1, 2]), 2)


if __name__ == "__main__":
    unittest.main()
'''

# Fails about 30% of the time, but the same way for the same commit and run
# number, so the test is reproducible.
FLAKY = '''import hashlib, os, sys
key = (os.environ.get("BISECT_COMMIT", "") + os.environ.get("BISECT_RUN", "")).encode()
sys.exit(1 if hashlib.sha256(key).digest()[0] < 77 else 0)
'''

results = []


def run(cmd, cwd=None, env=None):
    p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def subject(repo, sha):
    return run(["git", "log", "-1", "--format=%s", sha], repo)[1].strip()


def sha_of(repo, subj):
    return run(["git", "log", "--all", "--format=%H", "--fixed-strings", "--grep", subj], repo)[1].split()[0]


def check(name, ok, detail=""):
    results.append((name, ok))
    print("%s  %s  %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)


def bisect(repo, work, name, extra, env=None):
    out_json = os.path.join(work, name + ".json")
    args = [PY, os.path.join(SCRIPTS, "bisect.py"), "--repo", repo, "--good", ROOT,
            "--bad", "HEAD", "--workdir", os.path.join(work, "run-" + name), "--json", out_json] + extra
    rc, out = run(args, env=env)
    try:
        with open(out_json) as fh:
            res = json.load(fh)
    except OSError:
        print(out)
        return None, out
    return res, out


def main():
    keep = "--keep" in sys.argv
    work = tempfile.mkdtemp(prefix="bisect-selftest-")
    repo = os.path.join(work, "demo")
    rc, out = run([PY, os.path.join(HERE, "make_demo_repo.py"), repo])
    expected = json.loads(out)
    global ROOT
    ROOT = run(["git", "rev-list", "--max-parents=0", "HEAD"], repo)[1].strip()
    head_before = run(["git", "rev-parse", "HEAD"], repo)[1]
    status_before = run(["git", "status", "--porcelain"], repo)[1]
    n_commits = int(run(["git", "rev-list", "--count", "HEAD"], repo)[1])
    print("demo repo: %s (%d commits)" % (repo, n_commits))

    install_log = os.path.join(work, "installs.log")
    env = dict(os.environ, DEMO_INSTALL_LOG=install_log)
    test_file = os.path.join(work, "test_median.py")
    with open(test_file, "w") as fh:
        fh.write(TEST_MEDIAN)
    common = ["--install", PY + " install.py", "--build", PY + " build.py",
              "--overlay", test_file + ":test_median_overlay.py"]
    unit = PY + " -m unittest -q test_median_overlay"

    # 1. failing test, with install caching and unbuildable commits
    res, out = bisect(repo, work, "test", common + ["--oracle", unit], env)
    got = subject(repo, res["commit"]) if res and res.get("commit") else None
    check("failing test finds the bug commit", got == expected["bug"], got or out[-800:])
    installs = sum(1 for _ in open(install_log))
    steps = sum(1 for _ in open(os.path.join(work, "run-test", "steps.jsonl")))
    check("installs only when the lockfile changes", installs < steps, "%d installs over %d steps" % (installs, steps))

    # 2. first-parent: which merge brought it in
    res, out = bisect(repo, work, "firstparent", common + ["--oracle", unit, "--first-parent"], env)
    got = subject(repo, res["commit"]) if res and res.get("commit") else None
    check("--first-parent finds the merge", got == expected["bug_first_parent"], got or out[-800:])

    # 3. golden output, with the version and timestamp lines ignored
    golden = os.path.join(work, "golden.txt")
    wt = os.path.join(work, "root-wt")
    run(["git", "worktree", "add", "-q", "--detach", wt, ROOT], repo)
    ignore = ["--ignore", r"generated at .*", "--ignore", r"version \S+"]
    run([PY, os.path.join(ORACLES, "golden_oracle.py"), "--record", "--golden", golden,
         "--cmd", PY + " cli.py data.txt"], wt)
    ref_png = os.path.join(work, "reference.png")
    run([PY, "render.py", ref_png], wt)
    run(["git", "worktree", "remove", "--force", wt], repo)
    oracle = "%s %s --golden %s --cmd '%s cli.py data.txt' %s" % (
        PY, os.path.join(ORACLES, "golden_oracle.py"), golden, PY, " ".join("'%s'" % x for x in ignore))
    res, out = bisect(repo, work, "golden", common + ["--oracle", oracle], env)
    got = subject(repo, res["commit"]) if res and res.get("commit") else None
    check("golden diff finds the bug commit", got == expected["bug"], got or out[-800:])
    # the same oracle WITHOUT --ignore should blame a version bump instead
    naive = "%s %s --golden %s --cmd '%s cli.py data.txt'" % (PY, os.path.join(ORACLES, "golden_oracle.py"),
                                                             golden, PY)
    res, out = bisect(repo, work, "golden-naive", common + ["--oracle", naive, "--no-verify"], env)
    got = subject(repo, res["commit"]) if res and res.get("commit") else None
    check("(control) golden diff without --ignore blames the wrong commit", got != expected["bug"], str(got))

    # 4. slowdown: measure both ends, then bisect with a threshold between them
    bench = os.path.join(ORACLES, "bench_oracle.py")
    medians = []
    for ref in (ROOT, "HEAD"):
        wt = os.path.join(work, "bench-wt")
        run(["git", "worktree", "add", "-q", "--detach", wt, ref], repo)
        _, o = run([PY, bench, "--measure", "--metric", "cpu", "--cmd", PY + " bench.py"], wt)
        medians.append(float(re.search(r"median ([\d.]+)s", o).group(1)))
        run(["git", "worktree", "remove", "--force", wt], repo)
    oracle = "%s %s --metric cpu --cmd '%s bench.py' --good-median %s --bad-median %s" % (
        PY, bench, PY, medians[0], medians[1])
    res, out = bisect(repo, work, "bench", common + ["--oracle", oracle], env)
    got = subject(repo, res["commit"]) if res and res.get("commit") else None
    check("benchmark finds the slowdown", got == expected["slowdown"],
          "%s (good %.3fs, bad %.3fs)" % (got, medians[0], medians[1]))

    # 5. visual change: screenshot diff with tolerance ignores the 1-step colour nudge
    oracle = "%s render.py out.png && %s %s out.png %s --tolerance 8" % (
        PY, PY, os.path.join(ORACLES, "img_diff.py"), ref_png)
    res, out = bisect(repo, work, "visual", common + ["--oracle", oracle], env)
    got = subject(repo, res["commit"]) if res and res.get("commit") else None
    check("screenshot diff finds the visual change", got == expected["visual"], got or out[-800:])

    # 6. running server
    oracle = ("%s %s --cmd '%s server.py --port {port}' --url 'http://127.0.0.1:{port}/median?xs=1,2,3,4' "
              "--expect '\"median\": 2.5'") % (PY, os.path.join(ORACLES, "serve_oracle.py"), PY)
    res, out = bisect(repo, work, "server", common + ["--oracle", oracle, "--timeout", "60"], env)
    got = subject(repo, res["commit"]) if res and res.get("commit") else None
    check("server probe finds the bug commit", got == expected["bug"], got or out[-800:])
    for _ in range(10):
        leftover, _ = run(["pgrep", "-f", "server.py --port"])
        if leftover != 0:
            break
        time.sleep(0.5)
    check("no server processes left running", leftover != 0)

    # 7. flaky oracle (fails ~30% of runs at random): majority of 7 still finds it
    flaky = os.path.join(work, "flaky.py")
    with open(flaky, "w") as fh:
        fh.write(FLAKY)
    oracle = "%s %s && %s" % (PY, flaky, unit)
    res, out = bisect(repo, work, "flaky", common + ["--oracle", oracle, "--repeats", "7"], env)
    got = subject(repo, res["commit"]) if res and res.get("commit") else None
    check("flaky oracle + majority of 7 finds the bug commit", got == expected["bug"], got or out[-800:])

    # 8. shrink the bug commit to the one hunk that matters
    bug_sha = run(["git", "log", "--all", "--format=%H", "--grep", "^" + expected["bug"] + "$"], repo)[1].split()[0]
    rc, out = run([PY, os.path.join(SCRIPTS, "hunk_minimise.py"), "--repo", repo, "--commit", bug_sha,
                   "--install", PY + " install.py", "--build", PY + " build.py",
                   "--oracle", "cp %s test_median_overlay.py && %s; r=$?; rm -f test_median_overlay.py; exit $r"
                   % (test_file, unit), "--workdir", os.path.join(work, "hunks")], env=env)
    m = re.search(r"(\d+) of (\d+) hunks", out)
    ok = bool(m) and m.group(1) == "1" and "+    return s[mid]" in out
    check("hunk_minimise isolates the one median hunk", ok, m.group(0) if m else out[-800:])

    # 9. bisect_list: ordered list with untestable items, and a subset minimise
    bl = os.path.join(SCRIPTS, "bisect_list.py")
    rc, out = run([PY, bl, "--range", "1", "500", "--oracle",
                   "[ {} -ge 330 ] && [ {} -le 345 ] && exit 125; [ {} -lt 337 ]"])
    check("bisect_list with skipped items reports an honest range",
          rc == 2 and "'337'" in out and "'346'" in out, out.strip().splitlines()[-1])
    rc, out = run([PY, bl, "--range", "1", "500", "--oracle", "[ {} -lt 337 ]"])
    check("bisect_list finds the first bad item", "FIRST BAD: 337" in out, out.strip().splitlines()[-2])
    flags = os.path.join(work, "flags.txt")
    with open(flags, "w") as fh:
        fh.write("".join("FLAG_%s\n" % c for c in "ABCDEFGHIJKLMNOPQRST"))
    rc, out = run([PY, bl, "--minimise", "--items-file", flags, "--oracle",
                   "! (grep -qx FLAG_C {} && grep -qx FLAG_Q {})"])
    lines = [l.strip() for l in out.splitlines() if l.startswith("  FLAG_")]
    check("bisect_list --minimise finds the two flags that clash", lines == ["FLAG_C", "FLAG_Q"], str(lines))

    # 10. an answer hidden behind an unbuildable commit must be reported as a range
    oracle = "test ! -f extra.py"
    rc, out = run([PY, os.path.join(SCRIPTS, "bisect.py"), "--repo", repo, "--good", sha_of(repo, "Nudge badge green for contrast"),
                   "--bad", sha_of(repo, "Document the CLI"),
                   "--workdir", os.path.join(work, "run-ambiguous")] + common + ["--oracle", oracle], env=env)
    check("skipped commit next to the answer gives an honest 'one of these'",
          rc == 2 and "Start extra module" in out and "Fix extra module syntax" in out,
          out[out.find("NOT CONCLUSIVE"):].splitlines()[0] if "NOT CONCLUSIVE" in out else out[-600:])
    skipped = set()
    for d in os.listdir(work):
        sp = os.path.join(work, d, "steps.jsonl")
        if d.startswith("run-") and os.path.exists(sp):
            for line in open(sp):
                st = json.loads(line)
                if st["verdict"] == "skip":
                    skipped.add(subject(repo, st["commit"]))
    check("only unbuildable commits were ever skipped", skipped <= set(expected["unbuildable"]), str(sorted(skipped)))

    # 11. the user's checkout was never touched
    check("checkout untouched", run(["git", "rev-parse", "HEAD"], repo)[1] == head_before
          and run(["git", "status", "--porcelain"], repo)[1] == status_before)
    wts = run(["git", "worktree", "list"], repo)[1].strip().splitlines()
    check("no worktrees left behind", len(wts) == 1, str(len(wts)))

    failed = [n for n, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    if keep:
        print("kept: " + work)
    else:
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
