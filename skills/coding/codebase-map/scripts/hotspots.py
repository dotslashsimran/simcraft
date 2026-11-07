#!/usr/bin/env python3
"""Mine a repo's git history for the places that matter: hotspots (churn x complexity), files that change
together (hidden coupling), who knows what (ownership and knowledge at risk), bug magnets, and code age.

Usage:
  python3 hotspots.py <repo> [--days 365] [--top 20] [--under lib/core] [--max-files 20]
                       [--min-shared 3] [--min-degree 0.3] [--deps deps.json] [--anonymize]
                       [--json out.json]

Standard library + git only (Python 3.9+). Read-only: it only runs `git log` and reads files.
"""
import argparse
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import CODE_EXT, git, is_git_repo, is_test_path, list_files, md_table, read_text, skipped  # noqa: E402

CONFIG_EXT = {".json", ".yaml", ".yml", ".toml", ".prisma", ".graphql", ".gql", ".proto", ".sql"}
FIX_RE = re.compile(r"\b(fix(e[sd])?|bug|hotfix|revert|regression|patch|broken|crash)\b", re.I)
# Commits that touch many files for mechanical reasons. They are counted for churn but not for coupling,
# or every version file looks coupled to every other one.
NOISE_RE = re.compile(
    r"\b(bump(ed|s)?|release[sd]?|changelog|versions? to|prettier|reformat\w*|formatting|lint(ing)?|typos?|"
    r"merge (branch|pull)|sync (with|from)|chore\((deps|release)\)|deps?:|upgrade deps|update dependencies)\b", re.I)
RENAME_BRACE = re.compile(r"^(.*?)\{(.*?) => (.*?)\}(.*)$")


def ext(path):
    name = path.rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def parse_numstat_path(raw):
    """Return (old, new) for a numstat path, handling `a/{b => c}/d` and `old => new` rename forms."""
    m = RENAME_BRACE.match(raw)
    if m:
        pre, old, new, post = m.groups()
        join = lambda mid: re.sub(r"/+", "/", (pre + mid + post)).lstrip("/")
        return join(old), join(new)
    if " => " in raw:
        old, new = raw.split(" => ", 1)
        return old, new
    return raw, raw


def read_log(repo, max_commits):
    fmt = "%x1e%H%x1f%aN%x1f%at%x1f%s"
    out = git(repo, "log", "--no-merges", "-M", "--numstat", f"--format={fmt}", f"-n{max_commits}")
    commits = []
    for block in out.split("\x1e")[1:]:
        head, _, body = block.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 4:
            continue
        sha, author, ts, subject = parts[0], parts[1], int(parts[2]), parts[3]
        files = []
        for line in body.splitlines():
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            add, dele, raw = cols
            lines = (int(add) if add.isdigit() else 0) + (int(dele) if dele.isdigit() else 0)
            old, new = parse_numstat_path(raw)
            files.append((old, new, lines))
        commits.append({"sha": sha, "author": author, "ts": ts, "subject": subject, "files": files})
    return commits  # newest first


def complexity(text):
    """Indentation complexity: sum of logical indent levels over non-blank, non-comment lines.
    It tracks nesting (branches, loops, callbacks) well and works for any language."""
    lines = [l.expandtabs(4) for l in text.splitlines()]
    code = [l for l in lines if l.strip() and not re.match(r"\s*(#|//|/\*|\*|--|<!--)", l)]
    if not code:
        return 0, 0, 0
    widths = [len(l) - len(l.lstrip(" ")) for l in code]
    steps = Counter(b - a for a, b in zip(widths, widths[1:]) if 0 < b - a <= 8)
    unit = steps.most_common(1)[0][0] if steps else 4
    levels = [w / unit for w in widths]
    return len(code), round(sum(levels)), round(max(levels))


def fmt_date(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "-"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--days", type=int, default=365, help="window for churn, coupling and ownership (0 = all history)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--under", default="", help="only look at files under this folder")
    ap.add_argument("--max-files", type=int, default=20, help="ignore commits touching more files than this for coupling")
    ap.add_argument("--min-shared", type=int, default=3, help="minimum shared commits for a coupling pair")
    ap.add_argument("--min-degree", type=float, default=0.3, help="minimum coupling degree (shared / average revisions)")
    ap.add_argument("--max-commits", type=int, default=20000)
    ap.add_argument("--all-files", action="store_true", help="rank docs and data files too, not just code")
    ap.add_argument("--tests", action="store_true", help="include test files in the hotspot and risk tables")
    ap.add_argument("--deps", help="deps.json from deps.py, to mark coupling pairs that have no import link")
    ap.add_argument("--anonymize", action="store_true", help="replace author names with Dev A, Dev B, ...")
    ap.add_argument("--json", help="also write all results as JSON to this path")
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    if not is_git_repo(repo):
        sys.exit(f"{repo} is not a git repository")
    commits = read_log(repo, a.max_commits)
    if not commits:
        sys.exit("no commits found")
    head_ts = commits[0]["ts"]
    since = head_ts - a.days * 86400 if a.days else 0

    current = set(list_files(repo, a.under))
    keep = lambda p: p in current and (a.all_files or ext(p) in CODE_EXT) and (a.tests or not is_test_path(p))
    couple_ok = lambda p: p in current and (ext(p) in CODE_EXT or ext(p) in CONFIG_EXT)

    # Walk newest to oldest. When a commit renames old -> new, older commits that mention `old`
    # are really about today's file, so alias old to whatever new resolves to now.
    alias = {}
    resolve = lambda p: alias.get(p, p)
    revs, revs_all, churn, fixes = Counter(), Counter(), Counter(), Counter()
    first, last = {}, {}
    own_win, own_all = defaultdict(Counter), defaultdict(Counter)
    author_last, author_commits = {}, Counter()
    pair_shared = Counter()
    win_commits = noise_commits = 0
    for c in commits:
        in_win = c["ts"] >= since
        win_commits += in_win
        author_last.setdefault(c["author"], c["ts"])
        author_commits[c["author"]] += 1
        touched = []
        for old, new, lines in c["files"]:
            path = resolve(new)
            if old != new:
                alias[old] = path
            if skipped(path):
                continue
            if old == new or lines:  # a pure move is not a real change to the file
                touched.append(path)
            revs_all[path] += 1
            own_all[path][c["author"]] += max(lines, 1)
            first[path] = c["ts"]
            last.setdefault(path, c["ts"])
            if in_win:
                revs[path] += 1
                churn[path] += lines
                own_win[path][c["author"]] += max(lines, 1)
                if FIX_RE.search(c["subject"]):
                    fixes[path] += 1
        if in_win and NOISE_RE.search(c["subject"]):
            noise_commits += 1
        elif in_win:
            cf = sorted({p for p in touched if couple_ok(p)})
            if 2 <= len(cf) <= a.max_files:
                for x, y in combinations(cf, 2):
                    pair_shared[(x, y)] += 1

    # Anonymize authors by total commits if asked.
    names = {}
    if a.anonymize:
        for i, (au, _) in enumerate(author_commits.most_common()):
            names[au] = "Dev " + (chr(65 + i) if i < 26 else str(i + 1))
    who = lambda au: names.get(au, au)
    active_cut = head_ts - 90 * 86400
    active = {au for au, ts in author_last.items() if ts >= active_cut}

    # Hotspots: revisions in window x indentation complexity, both normalised to 0..1.
    rows = []
    for p in current:
        if not keep(p):
            continue
        text = read_text(repo / p)
        if text is None:
            continue
        loc, cx, depth = complexity(text)
        rows.append({"path": p, "revs": revs[p], "churn": churn[p], "loc": loc, "complexity": cx,
                     "max_depth": depth, "fixes": fixes[p], "first": first.get(p, 0), "last": last.get(p, 0)})
    max_r = max((r["revs"] for r in rows), default=1) or 1
    max_c = max((r["complexity"] for r in rows), default=1) or 1
    for r in rows:
        r["score"] = round((r["revs"] / max_r) * (r["complexity"] / max_c) * 100, 1)
        o = own_win[r["path"]] or own_all[r["path"]]
        tot = sum(o.values()) or 1
        top = o.most_common(1)[0] if o else ("-", 0)
        r["owner"], r["owner_share"] = who(top[0]), round(100 * top[1] / tot)
        r["authors"] = len(own_all[r["path"]])
        main_all = own_all[r["path"]].most_common(1)
        r["owner_active"] = bool(main_all) and main_all[0][0] in active
    hot = sorted((r for r in rows if r["revs"]), key=lambda r: -r["score"])[: a.top]

    # Coupling: degree = shared commits / average revisions of the two files.
    dep_edges = set()
    if a.deps:
        d = json.load(open(a.deps))
        for e in d.get("file_edges", []):
            dep_edges.add((e["from"], e["to"]))
            dep_edges.add((e["to"], e["from"]))
    pairs = []
    for (x, y), shared in pair_shared.items():
        if shared < a.min_shared:
            continue
        degree = shared / ((revs[x] + revs[y]) / 2)
        if degree < a.min_degree:
            continue
        stem = lambda p: re.sub(r"^(test_)|(_test|\.test|\.spec)$", "", p.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        if (is_test_path(x) != is_test_path(y)) and stem(x) == stem(y):
            kind = "test pair"
        elif is_test_path(x) != is_test_path(y):
            kind = "code + test"
        elif x.rsplit("/", 1)[0] == y.rsplit("/", 1)[0]:
            kind = "same folder"
        else:
            kind = "cross folder"
        link = "-" if not a.deps else ("yes" if (x, y) in dep_edges else "NO")
        pairs.append({"a": x, "b": y, "shared": shared, "degree": round(degree, 2), "kind": kind, "import_link": link})
    order = {"cross folder": 0, "same folder": 1, "code + test": 2, "test pair": 3}
    pairs.sort(key=lambda p: (order[p["kind"]], -p["shared"] * p["degree"]))

    # Ownership by folder (two levels deep, or under --under).
    base_depth = len(a.under.strip("/").split("/")) if a.under else 0
    folder_own = defaultdict(Counter)
    for p, o in own_win.items():
        if keep(p):
            parts = p.split("/")
            folder = "/".join(parts[: min(len(parts) - 1, base_depth + 2)]) or "(root)"
            folder_own[folder].update(o)
    folders = []
    for f, o in sorted(folder_own.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(o.values())
        if not tot:
            continue
        top = o.most_common(2)
        folders.append({"folder": f, "lines": tot, "authors": len(o),
                        "top": ", ".join(f"{who(n)} {round(100 * v / tot)}%" for n, v in top)})

    at_risk = [r for r in sorted(rows, key=lambda r: -r["complexity"])
               if r["authors"] and not r["owner_active"]][: a.top]
    bug_magnets = sorted((r for r in rows if r["fixes"] >= 2), key=lambda r: -r["fixes"])[: a.top]
    old_cut = head_ts - 365 * 86400
    frozen = sorted((r for r in rows if r["last"] and r["last"] < old_cut), key=lambda r: -r["complexity"])[: a.top]
    young = sorted((r for r in rows if r["first"] >= head_ts - 30 * 86400), key=lambda r: -r["loc"])[: a.top]

    win_label = f"last {a.days} days" if a.days else "all history"
    out = [f"# Git hotspots: {repo.name}", ""]
    out.append(f"- HEAD date {fmt_date(head_ts)}; {len(commits)} commits scanned; {win_commits} in window ({win_label}); "
               f"{len(author_commits)} authors, {len(active)} active in the 90 days before HEAD.")
    out.append(f"- {noise_commits} release, version-bump, formatting or dependency commits left out of coupling.")
    out.append(f"- {len(rows)} {'files' if a.all_files else 'code files'}{'' if a.tests else ' (tests excluded, add --tests)'} ranked{' under ' + a.under if a.under else ''}.")
    if win_commits < 30 and a.days:
        out.append("- Few commits in the window, so churn and coupling are weak signals. Try `--days 0`.")
    elif win_commits < 30:
        out.append("- Under 30 commits in total: git signals are weak here. Lean on reading and running the code.")
    out += ["", "## Hotspots (churn x complexity)", "",
            "Score = revisions in window x indentation complexity, both scaled to the max. High score means "
            "complicated code that keeps changing: the most expensive places to get wrong.", ""]
    out.append(md_table(["score", "file", "revs", "lines churned", "LOC", "complexity", "max depth", "fix commits", "owner"],
                        [[r["score"], r["path"], r["revs"], r["churn"], r["loc"], r["complexity"], r["max_depth"],
                          r["fixes"], f"{r['owner']} ({r['owner_share']}%)"] for r in hot]))
    out += ["", "## Change coupling (files that change together)", "",
            "Degree = shared commits / average revisions. Cross-folder pairs are the hidden dependencies. "
            + ("`import link = NO` means nothing in the code says these two are connected." if a.deps else
               "Pass `--deps deps.json` to flag pairs with no import between them."), ""]
    out.append(md_table(["kind", "file A", "file B", "shared", "degree", "import link"],
                        [[p["kind"], p["a"], p["b"], p["shared"], p["degree"], p["import_link"]] for p in pairs[: a.top * 2]])
               if pairs else "_No pairs above the thresholds. Lower `--min-shared` or widen `--days`._")
    out += ["", "## Ownership by folder (lines changed in window)", ""]
    out.append(md_table(["folder", "lines changed", "authors", "top authors"],
                        [[f["folder"], f["lines"], f["authors"], f["top"]] for f in folders[: a.top]]))
    out += ["", "## Knowledge at risk (main author not active in last 90 days)", ""]
    out.append(md_table(["file", "complexity", "main author (all time)", "last change"],
                        [[r["path"], r["complexity"], who(own_all[r["path"]].most_common(1)[0][0]), fmt_date(r["last"])]
                         for r in at_risk]) if at_risk else "_None: every file's main author is still active._")
    out += ["", "## Bug magnets (2+ fix/revert commits in window)", ""]
    out.append(md_table(["file", "fix commits", "revs", "complexity"],
                        [[r["path"], r["fixes"], r["revs"], r["complexity"]] for r in bug_magnets]) if bug_magnets else "_None._")
    out += ["", "## Frozen code (untouched for a year before HEAD, largest first)", "",
            "Either a stable foundation or dead code. Check deps.py orphans before deciding.", ""]
    out.append(md_table(["file", "complexity", "last change", "created"],
                        [[r["path"], r["complexity"], fmt_date(r["last"]), fmt_date(r["first"])] for r in frozen]) if frozen else "_None._")
    out += ["", "## New code (created in the 30 days before HEAD)", ""]
    out.append(md_table(["file", "LOC", "created"], [[r["path"], r["loc"], fmt_date(r["first"])] for r in young]) if young else "_None._")
    print("\n".join(out))

    if a.json:
        for r in rows:
            r["first"], r["last"] = fmt_date(r["first"]), fmt_date(r["last"])
        json.dump({"repo": repo.name, "head": fmt_date(head_ts), "window": win_label, "commits": len(commits),
                   "window_commits": win_commits, "hotspots": hot, "files": rows, "coupling": pairs,
                   "folders": folders, "active_authors": sorted(who(x) for x in active)},
                  open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
