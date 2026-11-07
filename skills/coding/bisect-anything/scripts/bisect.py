#!/usr/bin/env python3
"""Find the commit that caused a regression, without touching your checkout.

Creates a separate git worktree, checks that the oracle really says "good" on
the old commit and "bad" on the new one, then runs `git bisect run` with a
wrapper that handles repeats, majority vote, timeouts, unbuildable commits
(exit 125) and cached dependency installs. Prints a report at the end.

Example:
  bisect.py --good v1.4.0 --bad main \\
      --install "npm ci" --build "npm run build" \\
      --oracle "node /tmp/repro.js" --repeats 3 --timeout 60

Standard library only, Python 3.9+, git 2.29+ (for --first-parent).
"""
import os
import sys

# This folder holds a file named bisect.py, which would shadow the standard
# library's bisect module (used by random and tempfile). Look here last.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _HERE] + [_HERE]

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import time

from _common import BAD, DEP_FILES, GOOD, SKIP, dep_hash, git, log, run_once, summarise, vote  # noqa: E402

CODES = {GOOD: 0, BAD: 1, SKIP: 125}


# ---------------------------------------------------------------- one step

def apply_overlays(cfg):
    wt = cfg["worktree"]
    for src, dest in cfg["overlays"]:
        full = os.path.join(wt, dest)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        shutil.copyfile(src, full)


def remove_overlays(cfg):
    """Put overlaid paths back the way the commit has them, so the next
    checkout does not fail with 'untracked file would be overwritten'."""
    wt = cfg["worktree"]
    for _src, dest in cfg["overlays"]:
        tracked = subprocess.run(["git", "cat-file", "-e", "HEAD:" + dest], cwd=wt,
                                 stderr=subprocess.DEVNULL).returncode == 0
        if tracked:
            git(["checkout", "-q", "HEAD", "--", dest], wt)
        elif os.path.exists(os.path.join(wt, dest)):
            os.remove(os.path.join(wt, dest))


def load_state(cfg):
    try:
        with open(cfg["state"]) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(cfg, state):
    with open(cfg["state"], "w") as fh:
        json.dump(state, fh)


def step(cfg, label="bisect"):
    """Test the commit currently checked out in the worktree. Returns 0, 1 or 125."""
    wt = cfg["worktree"]
    sha = git(["rev-parse", "HEAD"], wt).strip()
    rec = {"label": label, "commit": sha, "installed": False, "time": time.time()}
    env = dict(os.environ, BISECT_COMMIT=sha, BISECT_WORKTREE=wt)
    verdict = SKIP
    try:
        if cfg["clean"]:
            args = ["clean", "-fdqx"]
            for k in cfg["keep"]:
                args += ["-e", k]
            git(args, wt)
        apply_overlays(cfg)

        if cfg["install"]:
            state = load_state(cfg)
            h = dep_hash(wt, cfg["dep_files"])
            if state.get("dep_hash") != h:
                r = run_once(cfg["install"], wt, env, cfg["setup_timeout"])
                rec["installed"] = True
                if r["verdict"] != GOOD:
                    rec.update(reason="install failed (%s)" % r["rc"], tail=r["tail"])
                    save_state(cfg, {})
                    return finish(cfg, rec, SKIP)
                save_state(cfg, {"dep_hash": h})

        if cfg["build"]:
            r = run_once(cfg["build"], wt, env, cfg["setup_timeout"])
            rec["build_seconds"] = r["seconds"]
            if r["verdict"] != GOOD:
                rec.update(reason="build failed (%s)" % r["rc"], tail=r["tail"])
                return finish(cfg, rec, SKIP)

        verdict, runs = vote(cfg["oracle"], wt, env, cfg["repeats"], cfg["vote"],
                             cfg["timeout"], cfg["timeout_means"])
        rec["runs"] = [{"verdict": r["verdict"], "rc": r["rc"], "seconds": r["seconds"]} for r in runs]
        rec["votes"] = summarise(runs)
        if verdict == SKIP:
            rec["reason"] = "oracle said skip or the vote was a tie"
        if verdict != GOOD:
            rec["tail"] = runs[-1]["tail"]
        return finish(cfg, rec, verdict)
    finally:
        remove_overlays(cfg)


def finish(cfg, rec, verdict):
    if cfg["find_fix"] and verdict != SKIP:
        # old term = broken, new term = fixed. A passing oracle means "fixed".
        verdict = BAD if verdict == GOOD else GOOD
    rec["verdict"] = verdict
    with open(cfg["steps"], "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    shown = {GOOD: cfg["term_old"], BAD: cfg["term_new"], SKIP: "skip"}[verdict]
    extra = rec.get("votes") or rec.get("reason") or ""
    log("[bisect] %s %s  %s%s" % (rec["commit"][:10], shown, extra,
                                  "  (deps reinstalled)" if rec["installed"] else ""))
    return CODES[verdict]


# ---------------------------------------------------------------- driver

def parse_args():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repository to bisect (default: current folder)")
    ap.add_argument("--good", "--old", dest="old", help="a commit where it works (or, with --find-fix, where it is broken)")
    ap.add_argument("--bad", "--new", dest="new", default="HEAD", help="a commit where it is broken (default HEAD)")
    ap.add_argument("--oracle", help="shell command run in the worktree: exit 0 good, 125 skip, other bad")
    ap.add_argument("--build", help="build command run on every commit; failure means skip (125)")
    ap.add_argument("--install", help="dependency install command; only rerun when dependency files change")
    ap.add_argument("--dep-file", action="append", dest="dep_files",
                    help="glob of files that trigger a reinstall (repeatable; default: common lockfiles and manifests)")
    ap.add_argument("--repeats", type=int, default=1, help="run the oracle up to N times per commit")
    ap.add_argument("--vote", choices=["majority", "any-bad"], default="majority",
                    help="majority (flaky test) or any-bad (bug that only shows up sometimes)")
    ap.add_argument("--timeout", type=float, help="seconds per oracle run")
    ap.add_argument("--timeout-means", choices=[BAD, SKIP], default=BAD,
                    help="what a timed out oracle run counts as (default bad: hangs are regressions too)")
    ap.add_argument("--setup-timeout", type=float, default=1800, help="seconds for install or build")
    ap.add_argument("--overlay", action="append", default=[], metavar="SRC:DEST",
                    help="copy a file into the worktree before each test (for a test that does not exist in old commits)")
    ap.add_argument("--clean", action="store_true", help="git clean -fdx before each build, keeping --keep paths")
    ap.add_argument("--keep", action="append", default=[], help="path kept by --clean (repeatable), e.g. node_modules")
    ap.add_argument("--first-parent", action="store_true", help="only walk the main line; answers 'which merge'")
    ap.add_argument("--find-fix", action="store_true", help="find the first commit where the oracle passes")
    ap.add_argument("--no-verify", action="store_true", help="skip checking the oracle on both ends first")
    ap.add_argument("--workdir", help="folder for the worktree and logs (default: a new temp folder)")
    ap.add_argument("--keep-worktree", action="store_true", help="leave the worktree in place afterwards")
    ap.add_argument("--json", help="also write the result as JSON to this file")
    ap.add_argument("--verbose", action="store_true", help="show git bisect's own output")
    ap.add_argument("paths", nargs="*", help="only consider commits touching these paths")
    ap.add_argument("--_step", help=argparse.SUPPRESS)
    return ap.parse_args()


def resolve(repo, ref):
    try:
        return git(["rev-parse", "--verify", "-q", ref + "^{commit}"], repo).strip()
    except RuntimeError:
        sys.exit("bisect.py: cannot resolve %r to a commit" % ref)


def commit_info(repo, sha):
    fmt = "%H%n%an <%ae>%n%ad%n%P%n%s%n%n%b"
    raw = git(["show", "-s", "--date=iso", "--format=" + fmt, sha], repo)
    lines = raw.split("\n")
    stat = git(["show", "--stat", "--format=", "-m", "--first-parent", sha], repo).rstrip()
    return {"sha": lines[0], "author": lines[1], "date": lines[2], "parents": lines[3].split(),
            "subject": lines[4], "body": "\n".join(lines[6:]).strip(), "stat": stat}


def main():
    a = parse_args()
    if a._step:
        with open(a._step) as fh:
            cfg = json.load(fh)
        try:
            sys.exit(step(cfg))
        except Exception as e:  # never let a wrapper bug abort the whole bisect
            log("[bisect] wrapper error, skipping commit: %s" % e)
            sys.exit(125)

    if not a.oracle or not a.old:
        sys.exit("bisect.py: --good and --oracle are required")
    repo = git(["rev-parse", "--show-toplevel"], os.path.abspath(a.repo)).strip()
    old, new = resolve(repo, a.old), resolve(repo, a.new)
    if subprocess.run(["git", "merge-base", "--is-ancestor", old, new], cwd=repo).returncode != 0:
        sys.exit("bisect.py: --good %s is not an ancestor of --bad %s. Swap them, or use --find-fix "
                 "if you are looking for the commit that fixed something." % (a.old, a.new))

    run_dir = os.path.abspath(a.workdir) if a.workdir else tempfile.mkdtemp(prefix="bisect-")
    os.makedirs(run_dir, exist_ok=True)
    wt = os.path.join(run_dir, "worktree")
    overlays = []
    for spec in a.overlay:
        src, _, dest = spec.partition(":")
        if not dest or not os.path.isfile(src):
            sys.exit("bisect.py: --overlay wants SRC:DEST with an existing SRC file, got %r" % spec)
        overlays.append([os.path.abspath(src), dest])

    term_old, term_new = ("broken", "fixed") if a.find_fix else ("good", "bad")
    cfg = {
        "worktree": wt, "oracle": a.oracle, "build": a.build, "install": a.install,
        "dep_files": a.dep_files or DEP_FILES, "repeats": max(1, a.repeats), "vote": a.vote,
        "timeout": a.timeout, "timeout_means": a.timeout_means, "setup_timeout": a.setup_timeout,
        "overlays": overlays, "clean": a.clean, "keep": a.keep, "find_fix": a.find_fix,
        "term_old": term_old, "term_new": term_new,
        "state": os.path.join(run_dir, "state.json"), "steps": os.path.join(run_dir, "steps.jsonl"),
    }
    cfg_path = os.path.join(run_dir, "config.json")
    with open(cfg_path, "w") as fh:
        json.dump(cfg, fh, indent=1)
    open(cfg["steps"], "w").close()

    log("[bisect] worktree %s (your checkout is not touched)" % wt)
    git(["worktree", "add", "-q", "--detach", wt, new], repo)
    started = time.time()
    result = {"status": "error"}
    try:
        if not a.no_verify:
            for sha, want, name in ((old, GOOD, a.old), (new, BAD, a.new)):
                git(["checkout", "-q", "--detach", sha], wt)
                code = step(cfg, label="verify")
                got = {0: GOOD, 1: BAD, 125: SKIP}[code]
                if got != want:
                    want_word = term_old if want == GOOD else term_new
                    got_word = {GOOD: term_old, BAD: term_new, SKIP: "skip"}[got]
                    raise SystemExit(
                        "bisect.py: the oracle says %r on %s, but that end should be %r.\n"
                        "Fix the oracle (or the refs) before bisecting; a bisect with a wrong end "
                        "always ends on a wrong commit.\nLast output:\n%s"
                        % (got_word, name, want_word, last_tail(cfg)))

        start = ["bisect", "start"]
        if a.first_parent:
            start.append("--first-parent")
        if a.find_fix:
            start += ["--term-old=broken", "--term-new=fixed"]
        start += [new, old, "--"] + a.paths
        git(start, wt)
        cmd = ["git", "bisect", "run", sys.executable, os.path.abspath(__file__), "--_step", cfg_path]
        p = subprocess.run(cmd, cwd=wt, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode("utf-8", "replace")
        if a.verbose:
            print(out)
        result = parse_bisect_output(out, term_new)
        result["bisect_log"] = git(["bisect", "log"], wt, check=False)
        git(["bisect", "reset", "-q"], wt, check=False)
    finally:
        if not a.keep_worktree:
            subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=repo,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "worktree", "prune"], cwd=repo)

    result["seconds"] = round(time.time() - started, 1)
    result["run_dir"] = run_dir
    report(repo, cfg, result, a)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(result, fh, indent=1)
    sys.exit(0 if result["status"] == "found" else 2)


def last_tail(cfg):
    try:
        with open(cfg["steps"]) as fh:
            return json.loads(fh.read().strip().splitlines()[-1]).get("tail", "")
    except (OSError, ValueError, IndexError):
        return ""


def parse_bisect_output(out, term_new):
    m = re.search(r"\b([0-9a-f]{40}) is the first %s commit" % re.escape(term_new), out)
    if m:
        return {"status": "found", "commit": m.group(1)}
    if "only 'skip'ped commits left" in out:
        tail = out.split("could be any of:", 1)[-1]
        shas = re.findall(r"^([0-9a-f]{40})\b", tail, re.M)
        return {"status": "ambiguous", "candidates": shas}
    return {"status": "error", "output": out[-3000:]}


def report(repo, cfg, result, a):
    steps = []
    with open(cfg["steps"]) as fh:
        for line in fh:
            if line.strip():
                steps.append(json.loads(line))
    p = print
    p("")
    p("=" * 72)
    term_new = cfg["term_new"]
    if result["status"] == "found":
        info = commit_info(repo, result["commit"])
        result["info"] = info
        p("FIRST %s COMMIT: %s" % (term_new.upper(), info["sha"]))
        p("  %s" % info["subject"])
        p("  %s, %s" % (info["author"], info["date"]))
        if len(info["parents"]) > 1:
            p("  This is a MERGE commit. To find the commit inside the merged branch, rerun with")
            p("  --good %s --bad %s (without --first-parent)." % (info["parents"][0][:12], info["sha"][:12]))
        p("")
        p(info["stat"])
    elif result["status"] == "ambiguous":
        p("NOT CONCLUSIVE: the first %s commit is one of these (the others in between were skipped):" % term_new)
        for s in result["candidates"]:
            p("  " + git(["log", "-1", "--format=%h %s", s], repo).strip())
        p("Make the skipped commits testable (a smaller build, a patched oracle), or test them by hand.")
    else:
        p("BISECT FAILED. git said:")
        p(result.get("output", ""))
    p("")
    p("Steps (%d, %.0fs):" % (len(steps), result["seconds"]))
    for s in steps:
        word = {GOOD: cfg["term_old"], BAD: cfg["term_new"], SKIP: "skip"}[s["verdict"]]
        p("  %-6s %s  %-6s %-9s %s%s" % (s["label"], s["commit"][:10], word, s.get("votes", ""),
                                       s.get("reason", ""), "  reinstalled deps" if s.get("installed") else ""))
    skipped = [s["commit"][:10] for s in steps if s["verdict"] == SKIP]
    if skipped:
        p("Skipped (could not be tested): " + ", ".join(skipped))
    p("Dependency installs: %d" % sum(1 for s in steps if s.get("installed")))
    p("Logs: %s" % cfg["steps"])
    if result["status"] == "found":
        p("")
        p("Next: read the diff (git show %s), then shrink it to the exact hunk:" % result["commit"][:12])
        p("  hunk_minimise.py --commit %s --oracle ... (same --build/--install)" % result["commit"][:12])
    p("=" * 72)


if __name__ == "__main__":
    main()
