#!/usr/bin/env python3
"""Shrink a bad commit to the smallest set of hunks that still causes the problem.

Bisect gives you a commit. If that commit touched 30 places, this tells you
which one (or which two together) actually broke it. It checks out the
commit's parent in a separate worktree, applies subsets of the commit's hunks
on top, and runs the oracle on each, using delta debugging (ddmin). A subset
that does not build counts as "does not fail", so hunks that need each other
end up together in the answer.

  hunk_minimise.py --commit abc123 --build "make" --oracle "./repro.sh"

Writes minimal.patch in the work folder and prints the hunks.
Standard library only, Python 3.9+.
"""
import os
import sys

# This folder holds a file named bisect.py, which would shadow the standard
# library's bisect module (used by random and tempfile). Look here last.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE] + [_HERE]

import argparse
import re
import subprocess
import tempfile

from _common import BAD, DEP_FILES, GOOD, SKIP, dep_hash, git, log, run_once, summarise, vote, ddmin  # noqa: E402

HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def show(repo, rev, path):
    p = subprocess.run(["git", "show", "%s:%s" % (rev, path)], cwd=repo,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout if p.returncode == 0 else None


def collect_units(repo, parent, commit, by, paths):
    names = git(["-c", "core.quotepath=false", "diff", "--no-renames", "--name-only", "-z",
                 parent, commit, "--"] + paths, repo).split("\0")
    files, units = {}, []
    for path in [n for n in names if n]:
        old, new = show(repo, parent, path), show(repo, commit, path)
        files[path] = {"old": old, "new": new}
        diff = git(["diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff", "--text",
                    parent, commit, "--", path], repo, binary=True)
        binary = b"\nBinary files " in diff or b"GIT binary patch" in diff
        hunks = [] if binary or by == "file" else parse_hunks(diff)
        if not hunks:
            label = "%s (whole file%s)" % (path, ", added" if old is None else ", deleted" if new is None else "")
            units.append({"path": path, "kind": "file", "label": label,
                          "text": diff.decode("utf-8", "replace")})
            continue
        for h in hunks:
            h["path"] = path
            h["kind"] = "hunk"
            h["label"] = "%s %s" % (path, h["header"])
            units.append(h)
    return files, units


def parse_hunks(diff):
    hunks, cur, last = [], None, None
    for line in diff.splitlines(keepends=True):
        m = HUNK_RE.match(line)
        if m:
            cur = {"old_start": int(m.group(1)),
                   "old_count": 1 if m.group(2) is None else int(m.group(2)),
                   "new_lines": [], "header": line.rstrip(b"\n").decode("utf-8", "replace"),
                   "raw": [line]}
            hunks.append(cur)
            continue
        if cur is None:
            continue
        cur["raw"].append(line)
        if line.startswith(b"+"):
            cur["new_lines"].append(line[1:])
            last = "+"
        elif line.startswith(b"-"):
            last = "-"
        elif line.startswith(b"\\"):
            if last == "+" and cur["new_lines"]:
                cur["new_lines"][-1] = cur["new_lines"][-1].rstrip(b"\n")
    for h in hunks:
        h["text"] = b"".join(h.pop("raw")).decode("utf-8", "replace")
    return hunks


def write(wt, path, data):
    full = os.path.join(wt, path)
    if data is None:
        if os.path.lexists(full):
            os.remove(full)
        return
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)


def apply_subset(wt, files, units, subset):
    by_path = {}
    for u in subset:
        by_path.setdefault(u["path"], []).append(u)
    total = {}
    for u in units:
        total[u["path"]] = total.get(u["path"], 0) + 1
    for path, chosen in by_path.items():
        f = files[path]
        if chosen[0]["kind"] == "file" or len(chosen) == total[path]:
            write(wt, path, f["new"])  # all hunks of a file: exactly the commit's version
            continue
        lines = (f["old"] or b"").splitlines(keepends=True)
        for h in sorted(chosen, key=lambda x: x["old_start"], reverse=True):
            idx = h["old_start"] if h["old_count"] == 0 else h["old_start"] - 1
            lines[idx:idx + h["old_count"]] = h["new_lines"]
        write(wt, path, b"".join(lines))


def restore(wt, files):
    for path, f in files.items():
        write(wt, path, f["old"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--commit", required=True, help="the bad commit (from bisect)")
    ap.add_argument("--parent", help="compare against this instead of the first parent")
    ap.add_argument("--oracle", required=True, help="exit 0 good, 125 cannot test, other bad")
    ap.add_argument("--build", help="build command; failure means this subset cannot be tested")
    ap.add_argument("--install", help="dependency install, rerun only when dependency files change")
    ap.add_argument("--by", choices=["hunk", "file"], default="hunk", help="unit of minimising")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--vote", choices=["majority", "any-bad"], default="majority")
    ap.add_argument("--timeout", type=float)
    ap.add_argument("--timeout-means", choices=[BAD, SKIP], default=BAD)
    ap.add_argument("--workdir", help="folder for the worktree and minimal.patch")
    ap.add_argument("--keep-worktree", action="store_true")
    ap.add_argument("paths", nargs="*", help="only minimise hunks in these paths (others are always applied)")
    a = ap.parse_args()

    repo = git(["rev-parse", "--show-toplevel"], os.path.abspath(a.repo)).strip()
    commit = git(["rev-parse", "--verify", a.commit + "^{commit}"], repo).strip()
    parent = git(["rev-parse", "--verify", (a.parent or commit + "^1") + "^{commit}"], repo).strip()
    run_dir = os.path.abspath(a.workdir) if a.workdir else tempfile.mkdtemp(prefix="hunk-min-")
    os.makedirs(run_dir, exist_ok=True)
    wt = os.path.join(run_dir, "worktree")

    files, units = collect_units(repo, parent, commit, a.by, [])
    fixed = []
    if a.paths:  # hunks outside the given paths are always applied, never minimised
        fixed = [u for u in units if not any(u["path"] == p or u["path"].startswith(p.rstrip("/") + "/")
                                             for p in a.paths)]
        units_to_min = [u for u in units if u not in fixed]
    else:
        units_to_min = units
    log("[hunk_minimise] %d units in %d files (%s), worktree %s"
        % (len(units_to_min), len(files), a.by, wt))
    if not units_to_min:
        sys.exit("hunk_minimise.py: the commit has no changes to minimise")

    git(["worktree", "add", "-q", "--detach", wt, parent], repo)
    state = {"dep_hash": None}
    env = dict(os.environ, BISECT_WORKTREE=wt, BISECT_COMMIT=commit)

    def fails(subset):
        restore(wt, files)
        apply_subset(wt, files, units, fixed + list(subset))
        try:
            if a.install:
                h = dep_hash(wt, DEP_FILES, files.keys())
                if h != state["dep_hash"]:
                    r = run_once(a.install, wt, env)
                    state["dep_hash"] = h if r["verdict"] == GOOD else None
                    if r["verdict"] != GOOD:
                        return False
            if a.build:
                r = run_once(a.build, wt, env)
                if r["verdict"] != GOOD:
                    log("[hunk_minimise] %3d units: does not build" % len(subset))
                    return False
            verdict, runs = vote(a.oracle, wt, env, max(1, a.repeats), a.vote, a.timeout, a.timeout_means)
            log("[hunk_minimise] %3d units: %-4s %s" % (len(subset), verdict, summarise(runs)))
            return verdict == BAD
        finally:
            restore(wt, files)

    try:
        if not fails(units_to_min):
            sys.exit("hunk_minimise.py: the whole commit applied on its parent does not fail. "
                     "The oracle may be flaky (try --repeats), or --parent is wrong.")
        if fails([]):
            sys.exit("hunk_minimise.py: the parent already fails without any of the hunks. "
                     "This commit is not the cause; check the bisect.")
        result, n_tests = ddmin(units_to_min, fails)
    finally:
        if not a.keep_worktree:
            subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=repo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "worktree", "prune"], cwd=repo)

    patch_path = os.path.join(run_dir, "minimal.patch")
    with open(patch_path, "w") as fh:
        for u in result:
            if u["kind"] == "hunk":
                fh.write("--- a/%s\n+++ b/%s\n" % (u["path"], u["path"]))
            fh.write(u["text"] if u["text"].endswith("\n") else u["text"] + "\n")
    print("")
    print("=" * 72)
    print("%d of %d hunks are enough to cause it (%d tests):" % (len(result), len(units_to_min), n_tests))
    for u in result:
        print("")
        print("# " + u["label"])
        print(u["text"].rstrip("\n"))
    print("")
    print("Removing any one of these makes the problem go away.")
    if len(result) > 1:
        print("Several hunks may be needed only because one does not build without the other.")
    print("Patch: " + patch_path)
    print("=" * 72)


if __name__ == "__main__":
    main()
