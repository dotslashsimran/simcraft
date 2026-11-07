#!/usr/bin/env python3
"""Friction logger for a developer experience audit.

Keeps one audit folder with friction.json in it. Every step gets a timestamp
automatically. Standard library only (Python 3.9+).

  friction.py init     <dir> --tool uv --goal "..." --success "..." [--docs URL] [--env "..."]
  friction.py step     <dir> --action "..." [--url U] [--cmd C] [--output T | --output-file F]
                             [--outcome ok|fail|blocked|success|info] [--tag T ...] [--sev 1-3]
                             [--loc N] [--note T] [--fix T] [--shot PATH] [--human-min M]
  friction.py run      <dir> --action "..." [--url U] [--cwd D] [--show "as typed"] [--success-if-ok] -- <command>
  friction.py redact   <dir> FROM=TO ...   scrub sandbox paths from everything saved so far
  friction.py amend    <dir> <step#> [--tag ..] [--sev N] [--note T] [--fix T] [--outcome O] [--loc N]
  friction.py errcheck <dir> --mistake "wrong key" [--cwd D] -- <command>
  friction.py grade    <dir> <check#> --what 0-2 --why 0-2 --fix 0-2 [--note T] [--better "rewritten message"]
  friction.py metrics  <dir> [--json]
  friction.py render   <dir> [--force]        audit.md skeleton + timeline.svg (+ timeline.png)
  friction.py reel     <dir> [--secs 3]       journey.mp4 slideshow of the step screenshots (needs ffmpeg)
"""
import argparse
import datetime as dt
import getpass
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

TAGS = {
    "missing-step": "A step the reader must do is not in the docs",
    "wrong-snippet": "A code or command snippet is wrong or out of date",
    "unclear-error": "An error message does not say what happened, why, or how to fix it",
    "version-mismatch": "Docs, package and runtime versions disagree",
    "auth-wall": "Progress needs a signup, an API key, a card or an approval",
    "naming-confusion": "The same thing has two names, or two things share one name",
    "slow-feedback": "Something takes long enough that a reader wonders if it hung",
    "dead-link": "A link is broken or points to the wrong place",
    "copy-paste-hazard": "Copying the snippet as shown does not work (prompts, smart quotes, placeholders)",
    "other": "Anything else; say what in the note",
}
OUTCOMES = ("ok", "fail", "blocked", "success", "info")
SEV_WORDS = {1: "annoying", 2: "costs real time", 3: "likely to make people quit"}

# Human time model. The agent's own wall clock is not a fair stand-in for a person, so the audit also
# reports a modelled human time. Every number here is printed in audit.md so readers can disagree with it.
READ_WPM = 238          # average adult silent reading speed for non-fiction
READ_CAP_MIN = 6.0      # people skim long pages; cap the reading charge per page
TYPE_SEC_PER_CMD = 20   # find the snippet, copy, paste, look at it
TYPE_SEC_PER_LOC = 15   # writing or adapting one line of code
DEBUG_MIN_BY_SEV = {0: 1.0, 1: 1.5, 2: 4.0, 3: 10.0}  # reading an error and working out what to do
DEFAULT_PAGE_WORDS = 500


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone()


def iso(t):
    return t.isoformat(timespec="seconds")


def parse_iso(s):
    return dt.datetime.fromisoformat(s)


def path_of(d):
    return Path(d) / "friction.json"


def load(d):
    p = path_of(d)
    if not p.exists():
        sys.exit(f"no friction.json in {d}; run: friction.py init {d} --tool ... --goal ... --success ...")
    return json.loads(p.read_text())


def redact_file(d):
    # Kept beside friction.json, not in it, because the pairs themselves hold the private paths. Don't publish it.
    return Path(d) / ".redact.json"


def redactor(d):
    """Audits get published, so machine paths and the user name never reach the files.
    Pairs from --redact come first (longest first), then the home folder and the login name."""
    f = redact_file(d)
    pairs = json.loads(f.read_text()) if f.exists() else []
    pairs = sorted((tuple(x) for x in pairs), key=lambda x: -len(x[0]))
    home, user = str(Path.home()), getpass.getuser()

    def fix(text):
        if not isinstance(text, str):
            return text
        for a, b in pairs:
            text = text.replace(a, b)
            if a.startswith("/private/"):
                text = text.replace(a[len("/private"):], b)
        text = text.replace(home, "~")
        if len(user) > 2:
            text = re.sub(re.escape(user), "user", text)
        return text
    return fix


def scrub(obj, fix):
    if isinstance(obj, dict):
        return {k: scrub(v, fix) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, fix) for v in obj]
    return fix(obj)


def save(d, data):
    p = path_of(d)
    data = scrub(data, redactor(d))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(p)


def clip(text, n=4000):
    text = text or ""
    return text if len(text) <= n else text[: n // 2] + f"\n... [{len(text) - n} chars cut] ...\n" + text[-n // 2 :]


def check_tags(tags):
    for t in tags or []:
        if t not in TAGS:
            sys.exit(f"unknown tag '{t}'. Use one of: {', '.join(TAGS)}")


def run_shell(cmd, cwd, timeout):
    start = time.time()
    try:
        # stderr merged into stdout so the output reads in the order the developer saw it
        p = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        out, code = p.stdout or "", p.returncode
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        out += f"\n[timed out after {timeout}s]"
        code = 124
    return out, code, round(time.time() - start, 2)


def cmd_init(a):
    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    for sub in ("screenshots", "snapshots", "outputs"):
        (d / sub).mkdir(exist_ok=True)
    if path_of(d).exists() and not a.force:
        sys.exit(f"{path_of(d)} already exists (use --force to start over)")
    t = now()
    data = {
        "tool": a.tool,
        "goal": a.goal,
        "success": a.success,
        "docs": a.docs,
        "environment": a.env,
        "started_at": iso(t),
        "success_at": None,
        "steps": [],
        "error_checks": [],
    }
    if a.redact:
        redact_file(d).write_text(json.dumps([r.split("=", 1) for r in a.redact]))
    save(d, data)
    print(f"started audit of {a.tool} at {iso(t)}\n  goal:    {a.goal}\n  success: {a.success}")


def new_step(data, a, extra):
    t = now()
    start = parse_iso(data["started_at"])
    step = {
        "n": len(data["steps"]) + 1,
        "t": iso(t),
        "elapsed_s": round((t - start).total_seconds()),
        "url": a.url,
        "action": a.action,
        "cmd": None,
        "output": None,
        "exit_code": None,
        "duration_s": None,
        "outcome": "ok",
        "tags": a.tag or [],
        "sev": a.sev or 0,
        "loc": a.loc or 0,
        "note": a.note,
        "fix": getattr(a, "fix", None),
        "shot": getattr(a, "shot", None),
        "human_min": getattr(a, "human_min", None),
    }
    step.update(extra)
    if step["tags"] and not step["sev"]:
        step["sev"] = 1
    return step


def print_step(s):
    tag = f"  [{', '.join(s['tags'])} sev {s['sev']}]" if s["tags"] else ""
    print(f"#{s['n']} +{s['elapsed_s']}s {s['outcome'].upper()}: {s['action']}{tag}")


def cmd_step(a):
    check_tags(a.tag)
    data = load(a.dir)
    out = a.output
    if a.output_file:
        out = Path(a.output_file).read_text(errors="replace")
    s = new_step(data, a, {"cmd": a.cmd, "output": clip(out) if out else None, "outcome": a.outcome})
    data["steps"].append(s)
    if a.outcome == "success" and not data["success_at"]:
        data["success_at"] = s["t"]
    save(a.dir, data)
    print_step(s)


def cmd_run(a):
    check_tags(a.tag)
    if not a.command:
        sys.exit("give the command after --")
    real = " ".join(a.command)
    cmd = a.show or real
    data = load(a.dir)
    out, code, secs = run_shell(real, a.cwd, a.timeout)
    out = redactor(a.dir)(out)
    outcome = "ok" if code == 0 else "fail"
    if a.success_if_ok and code == 0:
        outcome = "success"
    s = new_step(data, a, {"cmd": cmd, "output": clip(out), "exit_code": code, "duration_s": secs, "outcome": outcome})
    if outcome == "fail" and not s["sev"]:
        s["sev"] = 1  # unknown until amended; every failure costs something
    data["steps"].append(s)
    if outcome == "success" and not data["success_at"]:
        data["success_at"] = s["t"]
    outfile = Path(a.dir) / "outputs" / f"step-{s['n']:02d}.txt"
    outfile.write_text(f"$ {cmd}\n{out}\n[exit {code}, {secs}s]\n")
    save(a.dir, data)
    print(out.rstrip()[-3000:])
    print(f"[exit {code}, {secs}s]")
    print_step(s)
    if outcome == "fail":
        print(f"  tag it: friction.py amend {a.dir} {s['n']} --tag <tag> --sev <1-3> --note '...' --fix '...'")


def cmd_amend(a):
    check_tags(a.tag)
    data = load(a.dir)
    try:
        s = data["steps"][a.n - 1]
    except IndexError:
        sys.exit(f"no step {a.n}")
    if a.tag:
        s["tags"] = sorted(set(s["tags"]) | set(a.tag))
    for k in ("sev", "note", "fix", "outcome", "loc", "shot", "human_min", "url"):
        v = getattr(a, k, None)
        if v is not None:
            s[k] = v
    if s["tags"] and not s["sev"]:
        s["sev"] = 1
    if a.outcome == "success" and not data["success_at"]:
        data["success_at"] = s["t"]
    save(a.dir, data)
    print_step(s)


def cmd_redact(a):
    rf = redact_file(a.dir)
    pairs = json.loads(rf.read_text()) if rf.exists() else []
    pairs += [r.split("=", 1) for r in a.pair]
    rf.write_text(json.dumps(pairs))
    save(a.dir, load(a.dir))
    fix = redactor(a.dir)
    for f in (Path(a.dir) / "outputs").glob("*.txt"):
        f.write_text(fix(f.read_text()))
    print(f"redacting {len(pairs)} pairs (kept in .redact.json, which is not for publishing); friction.json and outputs/ rewritten")


def cmd_errcheck(a):
    if not a.command:
        sys.exit("give the command that makes the mistake after --")
    real = " ".join(a.command)
    cmd = a.show or real
    data = load(a.dir)
    out, code, secs = run_shell(real, a.cwd, a.timeout)
    out = redactor(a.dir)(out)
    c = {"n": len(data["error_checks"]) + 1, "mistake": a.mistake, "cmd": cmd, "exit_code": code,
         "message": clip(out, 3000), "what": None, "why": None, "fix": None, "note": None, "better": None}
    data["error_checks"].append(c)
    save(a.dir, data)
    print(out.rstrip()[-3000:])
    print(f"[exit {code}, {secs}s]")
    print(f"check #{c['n']} saved. Grade it: friction.py grade {a.dir} {c['n']} --what 0-2 --why 0-2 --fix 0-2 --note '...' --better '...'")


def cmd_grade(a):
    data = load(a.dir)
    try:
        c = data["error_checks"][a.n - 1]
    except IndexError:
        sys.exit(f"no error check {a.n}")
    for k in ("what", "why", "fix", "note", "better"):
        v = getattr(a, k)
        if v is not None:
            c[k] = v
    save(a.dir, data)
    print(f"check #{c['n']} {c['mistake']}: {grade_letter(c)}")


def grade_letter(c):
    if None in (c.get("what"), c.get("why"), c.get("fix")):
        return "ungraded"
    total = c["what"] + c["why"] + c["fix"]
    letter = {6: "A", 5: "B", 4: "C", 3: "D"}.get(total, "F")
    return f"{letter} ({total}/6)"


# ---------- metrics ----------

def page_words(d, url):
    """Words on a page, from a snapshot taken by snapshot.mjs, if there is one."""
    if not url:
        return None
    key = url.split("#")[0].rstrip("/")
    for f in (Path(d) / "snapshots").glob("*.json"):
        try:
            j = json.loads(f.read_text())
        except Exception:
            continue
        for u in (j.get("url"), j.get("finalUrl")):
            if u and u.split("#")[0].rstrip("/") == key:
                return j.get("words")
    return None


def human_minutes(d, steps):
    """Modelled minutes per step for a first-time reader. See the constants at the top."""
    seen, per = set(), []
    for s in steps:
        if s.get("human_min") is not None:
            per.append(float(s["human_min"]))
            continue
        m = 0.0
        if s.get("url"):
            key = s["url"].split("#")[0].rstrip("/")
            if key not in seen:
                seen.add(key)
                w = page_words(d, s["url"]) or DEFAULT_PAGE_WORDS
                m += min(w / READ_WPM, READ_CAP_MIN)
        if s.get("cmd"):
            m += TYPE_SEC_PER_CMD / 60 + (s.get("duration_s") or 0) / 60
        m += (s.get("loc") or 0) * TYPE_SEC_PER_LOC / 60
        if s["outcome"] in ("fail", "blocked"):
            m += DEBUG_MIN_BY_SEV.get(s.get("sev") or 0, 4.0)
        elif s.get("tags"):
            m += DEBUG_MIN_BY_SEV.get(s.get("sev") or 0, 1.0) / 2
        per.append(round(m, 2))
    return per


def stalls(steps, hmin):
    """Runs of trouble: from a failed or blocked step until the next step that works.
    The heaviest run is where a real person is most likely to give up."""
    runs, cur = [], None
    for s, m in zip(steps, hmin):
        bad = s["outcome"] in ("fail", "blocked") or (s.get("sev") or 0) >= 3
        if bad:
            if cur is None:
                cur = {"from": s["n"], "to": s["n"], "minutes": 0.0, "sev": 0, "fails": 0, "blocked": False, "tags": set()}
            cur["to"] = s["n"]
            cur["minutes"] += m
            cur["sev"] += s.get("sev") or 1
            cur["fails"] += 1
            cur["blocked"] |= s["outcome"] == "blocked"
            cur["tags"] |= set(s.get("tags") or [])
        elif cur is not None:
            cur["minutes"] += m
            runs.append(cur)
            cur = None
    if cur is not None:
        cur["unresolved"] = True
        runs.append(cur)
    for r in runs:
        r["tags"] = sorted(r["tags"])
        # Blocked walls and unresolved runs weigh most: those are where the journey actually stops.
        r["risk"] = round(r["sev"] * 2 + r["minutes"] + (6 if r["blocked"] else 0) + (6 if r.get("unresolved") else 0), 1)
    return sorted(runs, key=lambda r: -r["risk"])


def compute(d, data):
    steps = data["steps"]
    hmin = human_minutes(d, steps)
    start = parse_iso(data["started_at"])
    urls = [s["url"].split("#")[0].rstrip("/") for s in steps if s.get("url")]
    switches = sum(1 for a, b in zip(urls, urls[1:]) if a != b)
    docs_host = urlparse(data.get("docs") or (urls[0] if urls else "")).hostname
    off_docs = sorted({urlparse(u).hostname for u in urls if urlparse(u).hostname and urlparse(u).hostname != docs_host})
    succ_idx = next((i for i, s in enumerate(steps) if s["outcome"] == "success"), None)
    upto = steps[: succ_idx + 1] if succ_idx is not None else steps
    tags = {}
    for s in steps:
        for t in s.get("tags") or []:
            tags.setdefault(t, {"count": 0, "sev": 0})
            tags[t]["count"] += 1
            tags[t]["sev"] += s.get("sev") or 1
    checks = data.get("error_checks", [])
    graded = [c for c in checks if None not in (c.get("what"), c.get("why"), c.get("fix"))]
    m = {
        "reached_success": succ_idx is not None,
        "wall_clock_min": round((parse_iso(data["success_at"]) - start).total_seconds() / 60, 1) if data.get("success_at") else None,
        "human_min_to_success": round(sum(hmin[: succ_idx + 1]), 1) if succ_idx is not None else None,
        "human_min_total": round(sum(hmin), 1),
        "steps_to_success": len(upto),
        "steps_total": len(steps),
        "commands_to_success": sum(1 for s in upto if s.get("cmd")),
        "pages_visited": len(set(urls)),
        "context_switches": switches,
        "left_docs_for": off_docs,
        "errors_to_success": sum(1 for s in upto if s["outcome"] == "fail"),
        "errors_total": sum(1 for s in steps if s["outcome"] == "fail"),
        "walls": sum(1 for s in steps if s["outcome"] == "blocked"),
        "loc_to_success": sum(s.get("loc") or 0 for s in upto),
        "friction_points": sum(1 for s in steps if s.get("tags")),
        "friction_score": sum(s.get("sev") or 0 for s in steps if s.get("tags")),
        "by_tag": dict(sorted(tags.items(), key=lambda kv: -kv[1]["sev"])),
        "by_sev": {k: sum(1 for s in steps if s.get("tags") and s.get("sev") == k) for k in (1, 2, 3)},
        "give_up": stalls(steps, hmin)[:3],
        "error_checks_graded": len(graded),
        "error_check_avg": round(sum(c["what"] + c["why"] + c["fix"] for c in graded) / len(graded), 1) if graded else None,
        "human_min_per_step": hmin,
    }
    return m


def cmd_metrics(a):
    data = load(a.dir)
    m = compute(a.dir, data)
    if a.json:
        print(json.dumps(m, indent=2))
        return
    print(f"{data['tool']}: {data['goal']}")
    print(f"  reached success:     {'yes' if m['reached_success'] else 'NO'}")
    print(f"  time to success:     {fmt_min(m['human_min_to_success'])} modelled for a person, {fmt_min(m['wall_clock_min'])} agent wall clock")
    print(f"  steps / commands:    {m['steps_to_success']} / {m['commands_to_success']} (to success)")
    print(f"  pages / switches:    {m['pages_visited']} pages, {m['context_switches']} context switches" + (f", left the docs for {', '.join(m['left_docs_for'])}" if m['left_docs_for'] else ""))
    print(f"  errors:              {m['errors_to_success']} before success, {m['errors_total']} total, {plural(m['walls'], 'wall')}")
    print(f"  lines of code:       {m['loc_to_success']}")
    print(f"  friction:            {m['friction_points']} points, score {m['friction_score']} (sev1 {m['by_sev'][1]}, sev2 {m['by_sev'][2]}, sev3 {m['by_sev'][3]})")
    for t, v in m["by_tag"].items():
        print(f"     {t:18} x{v['count']}  weight {v['sev']}")
    if m["give_up"]:
        g = m["give_up"][0]
        print(f"  {give_up_label(g) + ':':20} steps {g['from']}-{g['to']} ({', '.join(g['tags']) or 'errors'}), risk {g['risk']} (8 or more, a wall, or an unresolved error counts as a likely give-up)")
    if m["error_check_avg"] is not None:
        print(f"  error messages:      average {m['error_check_avg']}/6 over {m['error_checks_graded']} checks")


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def fmt_min(x):
    if x is None:
        return "n/a"
    return f"{x:.1f} min" if x < 60 else f"{x / 60:.1f} h"


# ---------- render ----------

COL = {"ok": "#3f7d58", "friction": "#d08a17", "success": "#1f6feb", "fail": "#c9372c", "blocked": "#1b1b1b", "info": "#9a9a9a"}

def dot_color(s):
    if s["outcome"] == "ok" and s.get("tags"):
        return COL["friction"]
    return COL.get(s["outcome"], "#777")


def give_up_label(g):
    """Only call it a give-up point when it is heavy enough to make someone quit."""
    return "most likely give-up" if (g["risk"] >= 8 or g["blocked"] or g.get("unresolved")) else "roughest stretch"



def esc(s):
    return html.escape(str(s or ""), quote=True)


def short_page(u):
    if not u:
        return ""
    p = urlparse(u)
    tail = [x for x in p.path.split("/") if x]
    return (tail[-1] if tail else p.hostname or "")[:22]


def timeline_svg(data, m):
    steps = data["steps"]
    hmin = m["human_min_per_step"]
    W, L, R = 1200, 70, 40
    total = max(sum(hmin), 0.1)
    xs, acc = [], 0.0
    for h in hmin:
        acc += h
        xs.append(acc)
    X = lambda v: L + (W - L - R) * v / total
    top_y, band_y, band_h, dot_y = 70, 96, 26, 170
    notes = [(s, x) for s, x in zip(steps, xs) if s.get("tags") and (s.get("sev") or 0) >= 2]
    H = dot_y + 70 + 18 * len(notes) + 40
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         f'<text x="{L}" y="32" font-size="20" font-weight="600" fill="#111">{esc(data["tool"])}: {esc(data["goal"])}</text>',
         f'<text x="{L}" y="54" font-size="13" fill="#555">Modelled time for a first-time developer. '
         f'{m["steps_to_success"]} steps, {m["pages_visited"]} pages, {m["context_switches"]} context switches, {plural(m["errors_total"], "error")}'
         f'{", success at " + fmt_min(m["human_min_to_success"]) if m["reached_success"] else ", success not reached"}.</text>']
    # docs page bands: one shaded band per stretch on the same page, so context switches are visible
    o.append(f'<text x="{L - 8}" y="{band_y + 17}" font-size="11" fill="#777" text-anchor="end">page</text>')
    prev_x, cur, shade = L, None, 0
    pages = []
    for s, x0, x1 in zip(steps, [0] + xs[:-1], xs):
        u = (s.get("url") or "").split("#")[0].rstrip("/") or cur
        if u != cur:
            pages.append([u, x0, x1])
            cur = u
        else:
            pages[-1][2] = x1
    for i, (u, a0, a1) in enumerate(pages):
        fill = "#eef2f7" if i % 2 == 0 else "#dfe6ef"
        o.append(f'<rect x="{X(a0):.1f}" y="{band_y}" width="{max(X(a1) - X(a0), 1):.1f}" height="{band_h}" fill="{fill}"/>')
        if X(a1) - X(a0) > 50:
            o.append(f'<text x="{X(a0) + 5:.1f}" y="{band_y + 17}" font-size="11" fill="#333">{esc(short_page(u))}</text>')
    # axis
    ay = dot_y + 34
    o.append(f'<line x1="{L}" y1="{ay}" x2="{W - R}" y2="{ay}" stroke="#bbb"/>')
    step_min = 1 if total <= 15 else 5 if total <= 60 else 15
    t = 0
    while t <= total + 1e-9:
        o.append(f'<line x1="{X(t):.1f}" y1="{ay}" x2="{X(t):.1f}" y2="{ay + 5}" stroke="#999"/>'
                 f'<text x="{X(t):.1f}" y="{ay + 18}" font-size="11" fill="#777" text-anchor="middle">{t:g}m</text>')
        t += step_min
    # give-up zone
    if m["give_up"]:
        g = m["give_up"][0]
        a0 = xs[g["from"] - 2] if g["from"] >= 2 else 0
        a1 = xs[g["to"] - 1]
        o.append(f'<rect x="{X(a0):.1f}" y="{top_y + 60}" width="{max(X(a1) - X(a0), 3):.1f}" height="{ay - top_y - 60}" fill="#c9372c" opacity="0.08"/>')
        o.append(f'<text x="{X(a0) + 3:.1f}" y="{top_y + 72}" font-size="11" fill="#c9372c">{give_up_label(g)}</text>')
    o.append(f'<line x1="{L}" y1="{dot_y}" x2="{W - R}" y2="{dot_y}" stroke="#e3e3e3"/>')
    last_label_x = -99
    for s, x in zip(steps, xs):
        r = 5 + 3 * (s.get("sev") or 0) if s.get("tags") or s["outcome"] in ("fail", "blocked") else 5
        c = dot_color(s)
        if s["outcome"] == "success":
            o.append(f'<path d="M{X(x):.1f},{dot_y - 30} L{X(x):.1f},{dot_y} M{X(x):.1f},{dot_y - 30} l14,5 l-14,5" stroke="{c}" stroke-width="2" fill="{c}"/>')
        o.append(f'<circle cx="{X(x):.1f}" cy="{dot_y}" r="{r}" fill="{c}" stroke="#fff" stroke-width="1.5"><title>#{s["n"]} {esc(s["action"])}</title></circle>')
        # Labels that would collide, and the success flag's label, go below the line.
        below = X(x) - last_label_x < 16 or s["outcome"] == "success"
        ly = dot_y + r + 14 if below else dot_y - r - 6
        if not below:
            last_label_x = X(x)
        o.append(f'<text x="{X(x):.1f}" y="{ly}" font-size="10" fill="#444" text-anchor="middle">{s["n"]}</text>')
    # notes for sev 2-3 friction
    ny = ay + 44
    for s, x in notes:
        o.append(f'<circle cx="{L + 6}" cy="{ny - 4}" r="4" fill="{dot_color(s)}"/>'
                 f'<text x="{L + 16}" y="{ny}" font-size="12" fill="#222">#{s["n"]} sev {s["sev"]} {esc(", ".join(s["tags"]))}: {esc((s.get("note") or s["action"])[:120])}</text>')
        ny += 18
    # legend
    lx = W - R - 470
    for i, (k, lab) in enumerate([("ok", "worked"), ("friction", "worked, with friction"), ("fail", "error"), ("blocked", "wall"), ("success", "success")]):
        o.append(f'<circle cx="{lx + i * 80 + (70 if i > 1 else 0)}" cy="{H - 16}" r="5" fill="{COL[k]}"/><text x="{lx + i * 80 + (70 if i > 1 else 0) + 10}" y="{H - 12}" font-size="11" fill="#555">{lab}</text>')
    o.append(f'<text x="{L}" y="{H - 12}" font-size="11" fill="#777">bigger dot = higher severity; shaded bands = docs page in use</text>')
    o.append("</svg>")
    return "\n".join(o)


def render_md(data, m):
    steps = data["steps"]
    hmin = m["human_min_per_step"]
    L = []
    L.append(f"# {data['tool']}: developer experience audit\n")
    L.append(f"**Goal:** {data['goal']}  ")
    L.append(f"**Success means:** {data['success']}  ")
    L.append(f"**Docs:** {data.get('docs') or 'TODO'}  ")
    L.append(f"**Environment:** {data.get('environment') or 'TODO'}  ")
    L.append(f"**Date:** {data['started_at'][:10]}\n")
    L.append("## Summary\n")
    L.append("TODO: three or four sentences. Did a new developer reach the goal, how long did it take, where did it hurt most, and what one change would help most.\n")
    L.append("| Metric | Result |\n|---|---|")
    L.append(f"| Reached the goal | {'Yes' if m['reached_success'] else 'No'} |")
    L.append(f"| Time to first success (modelled, first-time developer) | {fmt_min(m['human_min_to_success'])} |")
    L.append(f"| Time to first success (agent wall clock) | {fmt_min(m['wall_clock_min'])} |")
    L.append(f"| Steps to success | {m['steps_to_success']} ({m['commands_to_success']} commands) |")
    L.append(f"| Docs pages visited / context switches | {m['pages_visited']} / {m['context_switches']} |")
    L.append(f"| Had to leave the docs for | {', '.join(m['left_docs_for']) or 'nothing'} |")
    L.append(f"| Errors before success | {m['errors_to_success']} |")
    L.append(f"| Signup or key walls | {m['walls']} |")
    L.append(f"| Lines of code written | {m['loc_to_success']} |")
    L.append(f"| Friction points (sev 1 / 2 / 3) | {m['friction_points']} ({m['by_sev'][1]} / {m['by_sev'][2]} / {m['by_sev'][3]}) |")
    L.append(f"| Error message quality | {str(m['error_check_avg']) + '/6 average' if m['error_check_avg'] is not None else 'not graded'} |")
    if m["give_up"]:
        g = m["give_up"][0]
        L.append(f"| {give_up_label(g).capitalize()} | steps {g['from']} to {g['to']} ({', '.join(g['tags']) or 'repeated errors'}) |")
    L.append("\n![Journey timeline](timeline.png)\n")
    L.append("## The journey\n")
    L.append("| # | Min | Page | What I did | Result | Friction |\n|---|---|---|---|---|---|")
    acc = 0.0
    for s, h in zip(steps, hmin):
        acc += h
        fr = f"{', '.join(s['tags'])} (sev {s['sev']})" if s.get("tags") else ""
        act = s["action"] + (f": `{s['cmd'][:70]}`" if s.get("cmd") else "")
        page = f"[{short_page(s['url'])}]({s['url']})" if s.get("url") else ""
        L.append(f"| {s['n']} | {acc:.1f} | {page} | {act.replace('|', chr(92) + '|')} | {s['outcome']} | {fr} |")
    L.append("\nTODO: under the table, tell the story in a few short paragraphs. Quote the exact error text where it matters and link the screenshots in `screenshots/`.\n")
    L.append("## Friction found\n")
    for s in sorted([s for s in steps if s.get("tags")], key=lambda s: -(s.get("sev") or 0)):
        L.append(f"- **#{s['n']} {', '.join(s['tags'])}, severity {s['sev']} ({SEV_WORDS.get(s['sev'], '')}).** {s.get('note') or 'TODO note'}"
                 + (f" Fix: {s['fix']}" if s.get("fix") else " Fix: TODO"))
    L.append("\n## Error messages\n")
    L.append("Each mistake was made on purpose. Grades: does the message say what happened, why, and how to fix it (0 to 2 each).\n")
    L.append("| Mistake | What | Why | Fix | Grade |\n|---|---|---|---|---|")
    for c in data.get("error_checks", []):
        L.append(f"| {c['mistake']} | {c['what'] if c['what'] is not None else '?'} | {c['why'] if c['why'] is not None else '?'} | {c['fix'] if c['fix'] is not None else '?'} | {grade_letter(c)} |")
    for c in data.get("error_checks", []):
        msg = (c.get("message") or "").strip().splitlines()
        # Long messages: keep the top (what the reader sees first) and the end (usually the hints).
        msg = "\n".join(msg if len(msg) <= 16 else msg[:4] + [f"... ({len(msg) - 14} lines cut) ..."] + msg[-10:])
        L.append(f"\n### {c['mistake']}\n\n```\n$ {c['cmd']}\n{msg}\n```\n")
        L.append(f"{c.get('note') or 'TODO: what a new developer would think this means.'}\n")
        if c.get("better"):
            L.append(f"A clearer message:\n\n```\n{c['better']}\n```\n")
    L.append("## Top 5 fixes\n")
    L.append("Ranked by impact (how many people it saves, and how much time) over effort (how much work for the team).\n")
    L.append("| # | Fix | Impact | Effort |\n|---|---|---|---|")
    for i in range(1, 6):
        L.append(f"| {i} | TODO | High/Med/Low | S/M/L |")
    L.append("\nTODO: for each fix, a short section with the current text, the problem in one sentence, and the rewritten doc snippet or error message, ready to paste.\n")
    L.append("## What they do well\n")
    L.append("TODO: specific things that worked, with the step number. Name what to keep so the fixes don't break it.\n")
    L.append("## Method\n")
    L.append(f"Fresh empty folder, no prior knowledge used beyond the official docs, docs followed literally in order. "
             f"Modelled time charges each new docs page at {READ_WPM} words a minute (at most {READ_CAP_MIN:g} minutes a page), "
             f"{TYPE_SEC_PER_CMD} s to copy and run each command plus its run time, {TYPE_SEC_PER_LOC} s per line of code, "
             f"and {', '.join(f'{v:g} min' for v in DEBUG_MIN_BY_SEV.values())} to work through a failure of severity 0, 1, 2 and 3. "
             f"Raw log: `friction.json`. Command output: `outputs/`. Page captures: `snapshots/` and `screenshots/`.\n")
    return "\n".join(L)


def cmd_render(a):
    data = load(a.dir)
    m = compute(a.dir, data)
    d = Path(a.dir)
    svg = timeline_svg(data, m)
    (d / "timeline.svg").write_text(svg)
    png = d / "timeline.png"
    here = Path(__file__).resolve().parent
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, str(here / "svg2png.mjs"), str(d / "timeline.svg"), str(png)], capture_output=True, text=True)
        if r.returncode != 0:
            print("could not render timeline.png (playwright missing?); timeline.svg is there:", r.stderr.strip()[-300:])
    md = render_md(data, m)
    target = d / "audit.md"
    if target.exists() and not a.force:
        target = d / "audit.skeleton.md"
    target.write_text(md)
    print(f"wrote {target}, {d / 'timeline.svg'}" + (f", {png}" if png.exists() else ""))


def cmd_reel(a):
    data = load(a.dir)
    d = Path(a.dir).resolve()
    shots = []
    for s in data["steps"]:
        if s.get("shot"):
            p = (d / s["shot"]) if not os.path.isabs(s["shot"]) else Path(s["shot"])
            if p.exists():
                shots.append(p)
    if (d / "timeline.png").exists():
        shots.append(d / "timeline.png")
    if not shots:
        sys.exit("no screenshots attached to steps (use --shot on step/amend)")
    import tempfile
    lst = Path(tempfile.mkstemp(suffix=".txt")[1])  # holds absolute paths, so it stays out of the audit folder
    lines = []
    for p in shots:
        lines += [f"file '{p}'", f"duration {a.secs}"]
    lines.append(f"file '{shots[-1]}'")
    lst.write_text("\n".join(lines) + "\n")
    ff = os.environ.get("FFMPEG", "ffmpeg")
    out = d / "journey.mp4"
    cmd = [ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
           "-vf", "scale=1440:900:force_original_aspect_ratio=decrease,pad=1440:900:(ow-iw)/2:(oh-ih)/2:white,format=yuv420p",
           "-r", "30", str(out)]
    r = subprocess.run(cmd)
    lst.unlink()
    if r.returncode == 0:
        print(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, need_action=True):
        sp.add_argument("dir")
        if need_action:
            sp.add_argument("--action", required=True, help="what the developer did, in a few words")
        sp.add_argument("--url", help="docs page this step came from")
        sp.add_argument("--tag", action="append", help="friction tag (repeatable): " + ", ".join(TAGS))
        sp.add_argument("--sev", type=int, choices=[1, 2, 3])
        sp.add_argument("--loc", type=int, help="lines of code written or edited in this step")
        sp.add_argument("--note")
        sp.add_argument("--fix", help="the concrete fix for this friction")
        sp.add_argument("--shot", help="screenshot path, relative to the audit folder")
        sp.add_argument("--human-min", type=float, dest="human_min", help="override the modelled minutes for this step")

    sp = sub.add_parser("init")
    sp.add_argument("dir")
    sp.add_argument("--tool", required=True)
    sp.add_argument("--goal", required=True)
    sp.add_argument("--success", required=True, help="the exact observable result that counts as success")
    sp.add_argument("--docs", help="the docs entry point")
    sp.add_argument("--env", help="OS, shell, runtime versions, fresh dir or container")
    sp.add_argument("--redact", action="append", help="FROM=TO text replaced in everything saved, e.g. the sandbox path=~ (repeatable)")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("step")
    common(sp)
    sp.add_argument("--cmd")
    sp.add_argument("--output")
    sp.add_argument("--output-file")
    sp.add_argument("--outcome", choices=OUTCOMES, default="ok")
    sp.set_defaults(fn=cmd_step)

    sp = sub.add_parser("run")
    common(sp)
    sp.add_argument("--cwd")
    sp.add_argument("--timeout", type=int, default=600)
    sp.add_argument("--success-if-ok", action="store_true", help="mark the step as the goal reached if it exits 0")
    sp.add_argument("--show", help="the command as the developer types it, when the real one is wrapped (sandbox env, docker exec)")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("amend")
    common(sp, need_action=False)
    sp.add_argument("n", type=int)
    sp.add_argument("--outcome", choices=OUTCOMES)
    sp.set_defaults(fn=cmd_amend)

    sp = sub.add_parser("redact")
    sp.add_argument("dir")
    sp.add_argument("pair", nargs="+", help="FROM=TO")
    sp.set_defaults(fn=cmd_redact)

    sp = sub.add_parser("errcheck")
    sp.add_argument("dir")
    sp.add_argument("--mistake", required=True)
    sp.add_argument("--show", help="the command as the developer types it, when the real one is wrapped")
    sp.add_argument("--cwd")
    sp.add_argument("--timeout", type=int, default=300)
    sp.set_defaults(fn=cmd_errcheck)

    sp = sub.add_parser("grade")
    sp.add_argument("dir")
    sp.add_argument("n", type=int)
    for k in ("what", "why", "fix"):
        sp.add_argument(f"--{k}", type=int, choices=[0, 1, 2])
    sp.add_argument("--note")
    sp.add_argument("--better", help="a rewritten error message that would score 6/6")
    sp.set_defaults(fn=cmd_grade)

    sp = sub.add_parser("metrics")
    sp.add_argument("dir")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_metrics)

    sp = sub.add_parser("render")
    sp.add_argument("dir")
    sp.add_argument("--force", action="store_true", help="overwrite audit.md (otherwise writes audit.skeleton.md if audit.md exists)")
    sp.set_defaults(fn=cmd_render)

    sp = sub.add_parser("reel")
    sp.add_argument("dir")
    sp.add_argument("--secs", type=float, default=3.0)
    sp.set_defaults(fn=cmd_reel)

    # Everything after a bare "--" is the shell command, untouched by argparse.
    argv = sys.argv[1:]
    command = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1 :]
    a = p.parse_args(argv)
    a.command = command
    a.fn(a)


if __name__ == "__main__":
    main()
