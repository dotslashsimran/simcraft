"""Shared helpers for bisect.py, bisect_list.py and hunk_minimise.py.

Standard library only, Python 3.9+.

Exit code contract for every oracle (the same one git bisect run uses):
  0        good (the regression is NOT present)
  125      skip (this candidate cannot be tested, for example it does not build)
  anything else, including crashes by signal and timeouts: bad
"""
import fnmatch
import hashlib
import os
import signal
import subprocess
import sys
import time

GOOD, BAD, SKIP = "good", "bad", "skip"

# Files whose change means dependencies must be reinstalled. Manifests are
# included on purpose: a package.json without a lockfile still pins deps.
DEP_FILES = [
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock",
    "package.json", "poetry.lock", "uv.lock", "Pipfile.lock", "pyproject.toml",
    "requirements*.txt", "requirements*.lock", "setup.py", "setup.cfg",
    "Cargo.lock", "Cargo.toml", "go.sum", "go.mod", "Gemfile.lock", "Gemfile",
    "composer.lock", "mix.lock", "pubspec.lock", "Podfile.lock", "gradle.lockfile",
]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def classify(rc):
    if rc == 0:
        return GOOD
    if rc == 125:
        return SKIP
    return BAD


def run_once(cmd, cwd, env=None, timeout=None, timeout_means=BAD, tail_lines=15):
    """Run a shell command in its own process group.

    Returns dict(verdict, rc, seconds, tail). On timeout the whole process
    group is killed, so servers and child processes do not leak.
    """
    start = time.monotonic()
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    timed_out = False
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = p.communicate()
    secs = round(time.monotonic() - start, 3)
    text = out.decode("utf-8", "replace")
    tail = "\n".join(text.rstrip().splitlines()[-tail_lines:])
    if timed_out:
        return {"verdict": timeout_means, "rc": "timeout", "seconds": secs, "tail": tail}
    rc = p.returncode
    return {"verdict": classify(rc), "rc": rc, "seconds": secs, "tail": tail}


def vote(cmd, cwd, env=None, repeats=1, mode="majority", timeout=None, timeout_means=BAD):
    """Run the oracle up to `repeats` times and combine the verdicts.

    majority: stop as soon as good or bad has more than half of the runs.
    any-bad:  one bad run is enough (for bugs that only show up sometimes);
              all runs must pass for good.
    Returns (verdict, runs).
    """
    runs = []
    need = repeats // 2 + 1
    for i in range(repeats):
        e = dict(env or os.environ)
        e["BISECT_RUN"] = str(i)
        r = run_once(cmd, cwd, e, timeout, timeout_means)
        runs.append(r)
        n_good = sum(x["verdict"] == GOOD for x in runs)
        n_bad = sum(x["verdict"] == BAD for x in runs)
        n_skip = sum(x["verdict"] == SKIP for x in runs)
        if mode == "any-bad":
            if n_bad:
                return BAD, runs
        elif n_good >= need or n_bad >= need:
            break
        if n_skip >= need:
            break
    n_good = sum(x["verdict"] == GOOD for x in runs)
    n_bad = sum(x["verdict"] == BAD for x in runs)
    if mode == "any-bad":
        return (GOOD if n_good else SKIP), runs
    if n_bad > n_good:
        return BAD, runs
    if n_good > n_bad:
        return GOOD, runs
    return SKIP, runs


def summarise(runs):
    g = sum(r["verdict"] == GOOD for r in runs)
    b = sum(r["verdict"] == BAD for r in runs)
    s = sum(r["verdict"] == SKIP for r in runs)
    return "%dg/%db/%ds" % (g, b, s)


def git(args, cwd, check=True, binary=False):
    p = subprocess.run(["git"] + list(args), cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args),
                                                  p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def dep_hash(worktree, patterns=None, extra_paths=()):
    """Hash the content of every dependency file in the worktree.

    Uses the files on disk (not the index) so it also works while hunks are
    being applied by hunk_minimise.py.
    """
    patterns = patterns or DEP_FILES
    files = git(["ls-files", "-z"], worktree).split("\0")
    chosen = set(p for p in extra_paths)
    for f in files:
        if not f:
            continue
        base = os.path.basename(f)
        if any(fnmatch.fnmatch(base, pat) or fnmatch.fnmatch(f, pat) for pat in patterns):
            chosen.add(f)
    h = hashlib.sha256()
    for f in sorted(chosen):
        full = os.path.join(worktree, f)
        if not any(fnmatch.fnmatch(os.path.basename(f), pat) or fnmatch.fnmatch(f, pat)
                   for pat in patterns):
            continue
        h.update(f.encode() + b"\0")
        if os.path.isfile(full):
            with open(full, "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
        else:
            h.update(b"<missing>")
    return h.hexdigest()


def ddmin(units, fails, progress=None):
    """Zeller's delta debugging: smallest subset of `units` for which fails() is True.

    fails(subset) must return True when the problem shows up, and False when it
    does not or the subset cannot be tested. The result is 1-minimal: removing
    any single unit from it makes the problem go away.
    """
    cache = {}

    def test(idx):
        key = tuple(idx)
        if key not in cache:
            cache[key] = fails([units[i] for i in idx])
            if progress:
                progress([units[i] for i in idx], cache[key])
        return cache[key]

    items = list(range(len(units)))
    n = 2
    while len(items) >= 2:
        size = len(items)
        chunks = [items[i * size // n:(i + 1) * size // n] for i in range(n)]
        chunks = [c for c in chunks if c]
        reduced = False
        for c in chunks:
            if test(c):
                items, n, reduced = c, 2, True
                break
        if not reduced and n > 2:
            for c in chunks:
                cset = set(c)
                comp = [x for x in items if x not in cset]
                if test(comp):
                    items, n, reduced = comp, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(items):
                break
            n = min(len(items), n * 2)
    return [units[i] for i in items], len(cache)
