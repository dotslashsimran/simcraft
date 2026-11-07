#!/usr/bin/env python3
"""Read a git repo's history into a compact JSON file for the timelapse renderer.

    python3 extract.py <repo> -o history.json [--since 2025-01-01] [--until 2025-06-30]
        [--path src] [--exclude 'docs/**'] [--include-default-noise] [--anonymize]
        [--max-events 1500] [--rev main]

It only reads the repo (git log / git diff / git rev-list). It follows the first-parent
chain, so line counts stay exact even with merged branches, and prints a short story
report (busy stretches, big folders, bulk commits worth excluding).
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
BINARY_WEIGHT = 8  # binary files have no line count; give them a small fixed tile size

# Noise skipped by default. Matched against the full path and against every path suffix.
DEFAULT_EXCLUDES = [
    # lockfiles
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock", "npm-shrinkwrap.json",
    "Cargo.lock", "poetry.lock", "Pipfile.lock", "uv.lock", "pdm.lock", "Gemfile.lock", "composer.lock",
    "go.sum", "mix.lock", "pubspec.lock", "Podfile.lock", "packages.lock.json", "flake.lock",
    # vendored, generated and build output
    "node_modules/**", "vendor/**", "vendors/**", "third_party/**", "third-party/**", "external/**",
    "dist/**", "build/**", "out/**", ".next/**", ".nuxt/**", ".svelte-kit/**", "target/**",
    "__pycache__/**", ".venv/**", "venv/**", "site-packages/**", "coverage/**", ".cache/**",
    "*.min.js", "*.min.css", "*.map", "*.pb.go", "*_pb2.py", "*.snap", "*.lock",
    "*.ipynb_checkpoints/**", ".DS_Store",
    # recorded test fixtures (HTTP cassettes, snapshots)
    "cassettes/**", "__snapshots__/**", "*.snap.*", "*.generated.*", "*_generated.*",
]

LANGS = [  # (name, extensions or exact file names)
    ("TypeScript", [".ts", ".tsx", ".mts", ".cts"]),
    ("JavaScript", [".js", ".jsx", ".mjs", ".cjs"]),
    ("Python", [".py", ".pyi", ".pyx"]),
    ("Notebook", [".ipynb"]),
    ("Go", [".go"]),
    ("Rust", [".rs"]),
    ("Java", [".java"]),
    ("Kotlin", [".kt", ".kts"]),
    ("Swift", [".swift"]),
    ("C", [".c", ".h"]),
    ("C++", [".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"]),
    ("C#", [".cs"]),
    ("Ruby", [".rb", ".erb", "Gemfile", "Rakefile"]),
    ("PHP", [".php"]),
    ("Dart", [".dart"]),
    ("Elixir", [".ex", ".exs"]),
    ("Lua", [".lua"]),
    ("Zig", [".zig"]),
    ("Scala", [".scala"]),
    ("R", [".r", ".R"]),
    ("Shell", [".sh", ".bash", ".zsh", ".fish", ".ps1"]),
    ("HTML", [".html", ".htm"]),
    ("CSS", [".css", ".scss", ".sass", ".less", ".styl"]),
    ("Vue", [".vue"]),
    ("Svelte", [".svelte"]),
    ("Astro", [".astro"]),
    ("SQL", [".sql", ".prisma"]),
    ("GLSL", [".glsl", ".frag", ".vert", ".wgsl"]),
    ("Markdown", [".md", ".mdx", ".rst", ".txt", ".adoc"]),
    ("Config", [".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env", ".xml", ".lock",
                "Dockerfile", "Makefile", ".gitignore", ".dockerignore", ".editorconfig", ".npmrc"]),
    ("Asset", [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".mp4", ".mov", ".mp3", ".wav",
               ".woff", ".woff2", ".ttf", ".otf", ".pdf", ".avif"]),
]
EXT_LANG = {}
for _name, _exts in LANGS:
    for _e in _exts:
        EXT_LANG[_e] = _name


def lang_of(path):
    base = path.rsplit("/", 1)[-1]
    if base in EXT_LANG:
        return EXT_LANG[base]
    if "." in base:
        ext = "." + base.rsplit(".", 1)[-1]
        return EXT_LANG.get(ext, EXT_LANG.get(ext.lower(), "Other"))
    return "Other"


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=off", *args],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        sys.exit("git %s failed: %s" % (args[0], r.stderr.decode("utf-8", "replace").strip()))
    return r.stdout.decode("utf-8", "replace")


def make_filter(paths, excludes):
    prefixes = [p.strip("/") + "/" for p in paths if p.strip("/")]
    rx = re.compile("|".join("(?:%s)" % fnmatch.translate(p) for p in excludes)) if excludes else None
    cache = {}

    def ok(path):
        if path not in cache:
            cache[path] = check(path)
        return cache[path]

    def check(path):
        if prefixes and not any(path.startswith(p) or path + "/" == p for p in prefixes):
            return False
        if not rx:
            return True
        parts = path.split("/")
        # match against every path suffix, so "dist/**" also hits "web/dist/x.js"
        return not any(rx.match("/".join(parts[i:])) for i in range(len(parts)))
    return ok


def split_rename(path):
    """'src/{a => b}/x.py' or 'a.py => b.py' -> (old, new); plain paths -> (None, path)."""
    m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", path)
    if m:
        pre, o, n, post = m.groups()
        return re.sub("/+", "/", pre + o + post), re.sub("/+", "/", pre + n + post)
    if " => " in path:
        o, n = path.split(" => ", 1)
        return o, n
    return None, path


def parse_numstat_line(line):
    a, d, path = line.split("\t", 2)
    if a == "-":
        return path, None, None
    return path, int(a), int(d)


NOISE_MSG = re.compile(r"^(wip|fix|fixes|update|updates|changes|minor|tweak|tweaks|cleanup|\.+|-+|test|tmp|save|commit|asdf|stuff|misc)\W*$", re.I)


def message_score(msg):
    """Higher is a better caption. Short or generic messages score low."""
    m = msg.strip()
    if not m or NOISE_MSG.match(m):
        return 0
    if m.lower().startswith(("merge branch", "merge remote", "merge pull request")):
        return 1
    return min(len(m), 60) + 5


def clean_message(subject, body):
    s = subject.strip()
    m = re.match(r"Merge pull request #\d+ from \S+", s)
    if m:  # GitHub merge: the PR title is the first body line
        first = next((l.strip() for l in body.splitlines() if l.strip()), "")
        if first:
            s = first
    s = re.sub(r"\s+", " ", s)
    return s[:140]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo")
    ap.add_argument("-o", "--out", default="history.json")
    ap.add_argument("--rev", default="HEAD", help="branch or commit to follow (default HEAD)")
    ap.add_argument("--since", help="start date, e.g. 2025-01-01")
    ap.add_argument("--until", help="end date, e.g. 2025-06-30")
    ap.add_argument("--path", action="append", default=[], help="only this folder (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], help="glob to skip, e.g. 'docs/**' (repeatable)")
    ap.add_argument("--include-default-noise", action="store_true",
                    help="keep lockfiles, vendored and build folders (skipped by default)")
    ap.add_argument("--anonymize", action="store_true", help="replace author names with Contributor 1, 2, ...")
    ap.add_argument("--max-events", type=int, default=1500,
                    help="squash neighbouring commits when there are more than this (default 1500)")
    ap.add_argument("--name", help="project name for the title card (default: folder name)")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    git(repo, "rev-parse", "--git-dir")
    excludes = ([] if args.include_default_noise else DEFAULT_EXCLUDES) + args.exclude
    keep = make_filter(args.path, excludes)
    pathspec = ["--"] + [p for p in args.path] if args.path else []

    # Baseline: the tree as it was just before the range starts.
    base_rev = None
    if args.since:
        base_rev = git(repo, "rev-list", "-1", "--first-parent", "--before=" + args.since, args.rev).strip() or None

    sizes = {}  # path -> lines (None for binary)
    if base_rev:
        for line in git(repo, "diff", "--numstat", "--no-renames", EMPTY_TREE, base_rev, *pathspec).splitlines():
            if not line.strip():
                continue
            path, a, _ = parse_numstat_line(line)
            if keep(path):
                sizes[path] = a
    baseline = dict(sizes)

    log_args = ["log", "--reverse", "--first-parent", "--diff-merges=first-parent", "-M",
                "--format=%x1e%H%x1f%ct%x1f%cI%x1f%an%x1f%s%x1f%b%x1d", "--numstat", "--summary"]
    if args.since:
        log_args.append("--since=" + args.since)
    if args.until:
        log_args.append("--until=" + args.until)
    log_args.append(args.rev)
    raw = git(repo, *log_args, *pathspec)

    commits = []
    for rec in raw.split("\x1e"):
        if not rec.strip():
            continue
        head, _, rest = rec.partition("\x1d")
        h, ct, ci, an, subj, body = (head.split("\x1f") + [""] * 6)[:6]
        tz = 0
        mt = re.search(r"([+-])(\d\d):(\d\d)$", ci.strip())
        if mt:
            tz = (1 if mt.group(1) == "+" else -1) * (int(mt.group(2)) * 60 + int(mt.group(3)))
        created, deleted, nums = set(), set(), []
        for line in rest.splitlines():
            if not line.strip():
                continue
            if line.startswith(" create mode "):
                created.add(line.split(" ", 4)[4])
            elif line.startswith(" delete mode "):
                deleted.add(line.split(" ", 4)[4])
            elif "\t" in line:
                nums.append(parse_numstat_line(line))
        commits.append(dict(h=h, t=int(ct), tz=tz, a=an, m=clean_message(subj, body),
                            created=created, deleted=deleted, nums=nums))

    if not commits:
        sys.exit("No commits in that range / path.")

    # Replay numstat onto the tree. Exact along first-parent history.
    events = []
    for c in commits:
        changes = []  # [path, lines_after, kind, added, deleted]
        add_total = del_total = 0
        for path, a, d in c["nums"]:
            src, path = split_rename(path)
            moved = None
            if src is not None:  # rename: carry the old size over so the tile can fly to its new folder
                if keep(src) and src in sizes:
                    old = sizes.pop(src)
                    if keep(path):
                        moved = src
                        sizes[path] = old
                    else:
                        changes.append([src, 0, "D", 0, old or 0, None])
                        del_total += old or 0
                        continue
            if not keep(path):
                continue
            if src is not None and not moved and a is not None:
                # renamed in from a skipped or unknown path: ask git for the real size
                out_ = git(repo, "diff", "--numstat", "--no-renames", EMPTY_TREE, c["h"], "--", path).split("\t")
                if len(out_) > 1 and out_[0] != "-":
                    sizes.pop(path, None)
                    a, d = int(out_[0]), 0
            if moved:
                changes.append([moved, 0, "D", 0, 0, None])
            if path in c["deleted"]:
                if path in sizes:
                    del sizes[path]
                changes.append([path, 0, "D", 0, d or 0, None])
                del_total += d or 0
                continue
            if a is None:  # binary
                kind = "A" if (path not in sizes or moved) else "M"
                sizes[path] = None
                changes.append([path, None, kind, 0, 0, moved])
                continue
            kind = "A" if (moved or path in c["created"] or path not in sizes) else "M"
            prev = sizes.get(path) or 0
            sizes[path] = max(0, prev + a - d)
            changes.append([path, sizes[path], kind, a, d, moved])
            add_total += a
            del_total += d
        if not changes:
            continue
        events.append(dict(h=c["h"][:7], t=c["t"], tz=c["tz"], a=c["a"], m=c["m"],
                           add=add_total, dele=del_total, ch=changes, n=1))

    if not events:
        sys.exit("Every commit in range was filtered out. Check --path / --exclude.")

    # Check: does the replayed tree match the real tree at the last commit?
    last_full = commits[-1]["h"]
    real = {}
    for line in git(repo, "diff", "--numstat", "--no-renames", EMPTY_TREE, last_full, *pathspec).splitlines():
        if line.strip():
            p, a, _ = parse_numstat_line(line)
            if keep(p):
                real[p] = a
    drift = sum(abs((sizes.get(p) or 0) - (real.get(p) or 0)) for p in set(sizes) | set(real))
    real_total = sum(v or 0 for v in real.values()) or 1

    raw_count = len(events)
    if len(events) > args.max_events:
        events = squash(events, args.max_events)

    # Authors
    author_names = []
    author_idx = {}
    if args.anonymize:
        order = [n for n, _ in Counter(e["a"] for e in events).most_common()]
        alias = {n: "Contributor %d" % (i + 1) for i, n in enumerate(order)}
    for e in events:
        name = alias[e["a"]] if args.anonymize else e["a"]
        if name not in author_idx:
            author_idx[name] = len(author_names)
            author_names.append(name)
        e["a"] = author_idx[name]

    # File table
    file_idx = {}
    files = []

    def fid(p):
        if p not in file_idx:
            file_idx[p] = len(files)
            files.append([p, lang_of(p)])
        return file_idx[p]

    base_out = [[fid(p), BINARY_WEIGHT if v is None else v, 1 if v is None else 0] for p, v in sorted(baseline.items())]
    ev_out = []
    for e in events:
        ch = []
        for path, lines, kind, a, d, mv in e["ch"]:
            binary = lines is None and kind != "D"
            row = [fid(path), BINARY_WEIGHT if binary else lines, kind, 1 if binary else 0]
            if mv:
                row.append(fid(mv))  # moved from this file
            ch.append(row)
        ev_out.append(dict(t=e["t"], tz=e["tz"], h=e["h"], a=e["a"], m=e["m"], add=e["add"], del_=e["dele"],
                           n=e["n"], s=message_score(e["m"]), ch=ch))
    for e in ev_out:
        e["del"] = e.pop("del_")

    name = args.name or os.path.basename(repo.rstrip("/"))
    if args.path and not args.name:
        name = name + "/" + args.path[0].strip("/")
    out = dict(version=1, name=name, rev=args.rev, since=args.since, until=args.until, paths=args.path,
               anonymized=args.anonymize, authors=author_names, files=files, baseline=base_out, events=ev_out,
               commits=len(commits), squashed=raw_count != len(events))
    with open(args.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))

    report(out, events, raw_count, drift, real_total, args)


def squash(events, limit):
    """Merge runs of neighbouring commits so there are at most `limit` events."""
    import math
    k = math.ceil(len(events) / limit)
    out = []
    for i in range(0, len(events), k):
        group = events[i:i + k]
        merged = {}
        for e in group:
            for path, lines, kind, a, d, mv in e["ch"]:
                if path in merged:
                    prev = merged[path]
                    if prev[2] == "A" and kind == "D":
                        del merged[path]
                        continue
                    kind = "A" if prev[2] == "A" else kind
                    merged[path] = [path, lines, kind, prev[3] + a, prev[4] + d, prev[5] or mv]
                else:
                    merged[path] = [path, lines, kind, a, d, mv]
        if not merged:
            continue
        best = max(group, key=lambda e: (message_score(e["m"]), e["add"] + e["dele"]))
        last = group[-1]
        out.append(dict(h=last["h"], t=last["t"], tz=last["tz"], a=best["a"], m=best["m"],
                        add=sum(e["add"] for e in group), dele=sum(e["dele"] for e in group),
                        ch=list(merged.values()), n=sum(e["n"] for e in group)))
    return out


def fmt_day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def report(out, events, raw_count, drift, real_total, args):
    ev = out["events"]
    t0, t1 = ev[0]["t"], ev[-1]["t"]
    days = max(1, (t1 - t0) // 86400)
    total = sum(v for _, v, _ in out["baseline"])
    sizes = {}
    for f, v, b in out["baseline"]:
        sizes[f] = (v, b)
    for e in ev:
        for f, v, k, b, *_ in e["ch"]:
            if k == "D":
                sizes.pop(f, None)
            else:
                sizes[f] = (v, b)
    final_lines = sum(v for v, b in sizes.values() if not b)
    print("\n%s: %d commits (%d with changes kept%s), %s to %s, %d days" % (
        out["name"], out["commits"], raw_count,
        ", squashed to %d events" % len(ev) if out["squashed"] else "", fmt_day(t0), fmt_day(t1), days))
    print("Now: %s lines in %d files. Replay check: %s" % (
        "{:,}".format(final_lines), len(sizes),
        "exact" if drift == 0 else "off by %d lines (%.2f%%)" % (drift, 100.0 * drift / real_total)))

    # Activity by month
    churn = defaultdict(int)
    for e in ev:
        churn[datetime.fromtimestamp(e["t"], timezone.utc).strftime("%Y-%m")] += e["add"] + e["del"]
    if churn:
        peak = max(churn.values()) or 1
        print("\nActivity by month (lines added + removed):")
        for mth in sorted(churn):
            bar = "#" * max(1 if churn[mth] else 0, round(30 * churn[mth] / peak))
            print("  %s %-30s %s" % (mth, bar, "{:,}".format(churn[mth])))

    # Busiest stretch: the 30-day window with the most churn
    best, best_lo = 0, t0
    lo = 0
    acc = 0
    for hi, e in enumerate(ev):
        acc += e["add"] + e["del"]
        while ev[lo]["t"] < e["t"] - 30 * 86400:
            acc -= ev[lo]["add"] + ev[lo]["del"]
            lo += 1
        if acc > best:
            best, best_lo = acc, ev[lo]["t"]
    total_churn = sum(e["add"] + e["del"] for e in ev) or 1
    if days > 45:
        s = datetime.fromtimestamp(best_lo, timezone.utc)
        print("\nBusiest 30 days: %s to %s (%d%% of all changes). Try --since %s --until %s" % (
            fmt_day(best_lo), fmt_day(best_lo + 30 * 86400), round(100 * best / total_churn),
            (s - timedelta(days=1)).strftime("%Y-%m-%d"), (s + timedelta(days=31)).strftime("%Y-%m-%d")))

    # Top folders now
    by_dir = Counter()
    files = out["files"]
    for f, (v, b) in sizes.items():
        parts = files[f][0].split("/")
        by_dir[parts[0] if len(parts) > 1 else "(root files)"] += v
    print("\nBiggest top-level folders now:")
    for d, v in by_dir.most_common(8):
        print("  %-28s %s lines" % (d, "{:,}".format(v)))

    # Bulk commits: one commit that adds a big share of the project is usually an import or generated code
    bulky = sorted(ev, key=lambda e: e["add"], reverse=True)[:5]
    flagged = [e for e in bulky if e["add"] > max(2000, 0.2 * max(final_lines, 1))]
    if flagged:
        print("\nBulk commits (often vendored, generated or imported code; consider --exclude):")
        for e in flagged:
            top = Counter()
            for f, v, k, b, *_ in e["ch"]:
                parts = files[f][0].split("/")
                top["/".join(parts[:2]) if len(parts) > 2 else files[f][0]] += 1
            where = ", ".join("%s (%d files)" % (p, n) for p, n in top.most_common(3))
            print("  %s %s +%s  \"%s\"\n      in: %s" % (e["h"], fmt_day(e["t"]), "{:,}".format(e["add"]), e["m"][:60], where))

    lang = Counter()
    for f, (v, b) in sizes.items():
        if not b:
            lang[files[f][1]] += v
    if lang:
        print("\nLanguages now: " + ", ".join("%s %d%%" % (l, round(100 * v / max(1, final_lines))) for l, v in lang.most_common(6)))
    n = len(ev)
    print("\nSuggested --duration: %ds  (history.json: %d events, %d files)\nWrote %s" % (
        max(15, min(60, round(12 + n * 0.04))), n, len(files), args.out))


if __name__ == "__main__":
    main()
