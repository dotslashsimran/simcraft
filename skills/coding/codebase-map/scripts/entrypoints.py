#!/usr/bin/env python3
"""Find the ways into a repo: how it is built, run and deployed, its main functions, servers, CLI commands,
HTTP routes, background jobs, data model (schemas, migrations, ORM models), config it reads from the
environment, and its tests. Every code hit is reported as file:line so you can jump straight to it.

Usage:
  python3 entrypoints.py <repo> [--under src] [--top 40] [--json out.json]

Regex based, standard library only (Python 3.9+). Read-only. It never reads values from real .env files,
only variable names from code and from .env.example style templates.
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import is_test_path, list_files, md_table, read_text  # noqa: E402

CODE = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rb", ".java", ".kt", ".rs", ".php", ".cs"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
HTTP = r"(get|post|put|patch|delete|head|options|all|route|websocket|api_route)"

PATTERNS = {
    "main": [
        (r"""^if\s+__name__\s*==\s*['"]__main__['"]""", "python __main__"),
        (r"^func\s+main\s*\(\s*\)", "go main"),
        (r"^fn\s+main\s*\(", "rust main"),
        (r"public\s+static\s+void\s+main\s*\(", "java main"),
        (r"^#!.*\b(python|node|bash|sh|deno|bun)\b", "shebang script"),
    ],
    "server": [
        (r"\b(uvicorn|hypercorn|waitress)\.(run|serve)\(|\bapp\.run\([^)\n]*(host|port|debug)|\bweb\.run_app\(|\bserve_forever\(",
         "python server start"),
        (r"\b(FastAPI|Flask|Starlette|Sanic|Quart)\(", "python web app"),
        (r"\bexpress\(\)|new\s+(Hono|Koa|Elysia)\(|\bFastify\(|\bfastify\(", "node web app"),
        (r"\.listen\(\s*(process\.env\.\w+|\d+|port|PORT)", "listen on port"),
        (r"\bcreateServer\(", "node createServer"),
        (r"\bhttp\.ListenAndServe(TLS)?\(|\.Run\(\s*\":\d+\"|\bgrpc\.NewServer\(", "go server"),
        (r"\bWebSocketServer\(|\bnew\s+Server\(.*socket", "websocket server"),
    ],
    "cli": [
        (r"\btyper\.Typer\(", "typer app"),
        (r"\bargparse\.ArgumentParser\(", "argparse"),
        (r"""\.add_parser\(\s*['"]([\w:-]+)""", "argparse subcommand"),
        (r"""\.command\(\s*['"]([\w:<>\[\] -]+)['"]""", "commander/yargs command"),
        (r"""Use:\s*"([\w-]+)""", "cobra command"),
    ],
    "job": [
        (r"@(shared_task|\w+\.task|periodic_task|dramatiq\.actor|job|cron)\b", "task/job decorator"),
        (r"\bschedule\.every\(|\.add_job\(|\bcrontab\(|\bCronTrigger\(", "python scheduler"),
        (r"""\bcron\.schedule\(|new\s+CronJob\(|@Cron\(|\bnew\s+Worker\(\s*['"]""", "node cron/queue worker"),
        (r"\binngest\.createFunction\(|\bschedules?\.task\(|\bdefineJob\(", "job framework"),
        (r"\bBackgroundTasks\b", "fastapi background task"),
    ],
}
ROUTE_PATTERNS = [
    (re.compile(r"""@(\w+)\.""" + HTTP + r"""\(\s*['"]([^'"]*)['"]"""), "py"),
    (re.compile(r"""\b(app|router|server|api|\w*[Rr]outer|\w*[Aa]pp)\.""" + HTTP + r"""\(\s*['"`](/[^'"`]*)['"`]"""), "js"),
    (re.compile(r"""\b(?:re_)?path\(\s*r?['"]([^'"]*)['"]\s*,"""), "django"),
    (re.compile(r"""\b(\w+)\.(GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete|HandleFunc|Handle)\(\s*"(/[^"]*)\""""), "go"),
    (re.compile(r"""@(Get|Post|Put|Patch|Delete)(?:Mapping)?\(\s*['"]?([^'")]*)"""), "decorator"),
]
DATA_PATTERNS = [
    (r"^\s*model\s+(\w+)\s*\{", "prisma model", {".prisma"}),
    (r"(?i)^\s*create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?([\w.]+)", "SQL table", {".sql"}),
    (r"^class\s+(\w+)\((?:Base|[\w.]*DeclarativeBase|db\.Model|models\.Model|[\w.]*ORMBase)\)\s*:", "ORM model class", {".py"}),
    (r"^class\s+(\w+)\(SQLModel,\s*table\s*=\s*True", "SQLModel table", {".py"}),
    (r"""__tablename__\s*=\s*['"](\w+)""", "SQLAlchemy table", {".py"}),
    (r"""\b(?:pgTable|sqliteTable|mysqlTable)\(\s*['"](\w+)""", "drizzle table", {".ts", ".js"}),
    (r"""\b(?:mongoose\.)?model\(\s*['"](\w+)['"]\s*,""", "mongoose model", {".ts", ".js"}),
    (r"@Entity\(", "TypeORM/JPA entity", {".ts", ".java", ".kt"}),
    (r"^type\s+(\w+)\s+struct\s*\{[^}]*gorm:", "gorm model", {".go"}),
]
ENV_PATTERNS = [
    re.compile(r"""os\.(?:environ\.get|getenv|environ\.setdefault)\(\s*['"]([A-Z][A-Z0-9_]+)"""),
    re.compile(r"""os\.environ\[\s*['"]([A-Z][A-Z0-9_]+)"""),
    re.compile(r"""\bprocess\.env\.([A-Z][A-Z0-9_]+)|process\.env\[\s*['"]([A-Z][A-Z0-9_]+)"""),
    re.compile(r"""import\.meta\.env\.([A-Z][A-Z0-9_]+)"""),
    re.compile(r"""os\.(?:Getenv|LookupEnv)\(\s*"([A-Z][A-Z0-9_]+)"""),
    re.compile(r"""\b(?:env|config)\(\s*['"]([A-Z][A-Z0-9_]+)['"]"""),
]
MIGRATION_DIR = re.compile(r"(^|/)(migrations|alembic/versions|migrate|db/migrate|prisma/migrations|drizzle|supabase/migrations)/")


def ext(p):
    return PurePosixPath(p).suffix.lower()


def toml_section(text, header):
    m = re.search(r"^\[" + re.escape(header) + r"\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if not m:
        return {}
    return dict(re.findall(r"""^\s*["']?([\w.-]+)["']?\s*=\s*["']([^"']+)["']""", m.group(1), re.M))


def manifests(repo, files):
    out = []
    for f in files:
        name = PurePosixPath(f).name
        if name == "package.json":
            try:
                data = json.loads(read_text(repo / f) or "{}")
            except ValueError:
                continue
            for k, v in (data.get("scripts") or {}).items():
                out.append((f, "npm script", f"{k}: {v}"))
            b = data.get("bin")
            for k, v in (b.items() if isinstance(b, dict) else [(data.get("name"), b)] if b else []):
                out.append((f, "bin", f"{k}: {v}"))
            for k in ("main", "module"):
                if data.get(k):
                    out.append((f, k, data[k]))
            dep = list((data.get("dependencies") or {}).keys())
            if dep:
                out.append((f, "dependencies", f"{len(dep)}: " + ", ".join(dep[:12]) + (" ..." if len(dep) > 12 else "")))
            if data.get("workspaces"):
                out.append((f, "workspaces", json.dumps(data["workspaces"])))
        elif name == "pyproject.toml":
            t = read_text(repo / f) or ""
            for sec in ("project.scripts", "tool.poetry.scripts", "project.gui-scripts"):
                for k, v in toml_section(t, sec).items():
                    out.append((f, "console script", f"{k} = {v}"))
            for k, v in toml_section(t, "tool.poe.tasks").items():
                out.append((f, "poe task", f"{k}: {v}"))
        elif name in ("setup.py", "setup.cfg"):
            for m in re.finditer(r"""['"]?([\w-]+)\s*=\s*([\w.]+:\w+)""", read_text(repo / f) or ""):
                out.append((f, "console script", f"{m.group(1)} = {m.group(2)}"))
        elif name in ("Makefile", "makefile", "justfile", "Justfile", "Taskfile.yml"):
            t = read_text(repo / f) or ""
            targets = re.findall(r"^([A-Za-z][\w.-]*)\s*:(?!=)", t, re.M)
            if targets:
                out.append((f, "targets", ", ".join(dict.fromkeys(targets))))
        elif name == "Procfile":
            for line in (read_text(repo / f) or "").splitlines():
                if ":" in line:
                    out.append((f, "process", line.strip()))
        elif name.startswith("Dockerfile") or name.endswith(".Dockerfile"):
            for m in re.finditer(r"^(FROM|CMD|ENTRYPOINT|EXPOSE)\s+(.+)$", read_text(repo / f) or "", re.M | re.I):
                out.append((f, m.group(1).upper(), m.group(2).strip()))
        elif re.match(r"(docker-)?compose(\.\w+)?\.ya?ml$", name):
            t = read_text(repo / f) or ""
            svc = re.search(r"^services:\s*$(.*?)(?=^\S|\Z)", t, re.M | re.S)
            if svc:
                names = re.findall(r"^  ([\w.-]+):\s*$", svc.group(1), re.M)
                out.append((f, "services", ", ".join(names)))
        elif name == "go.mod":
            m = re.search(r"^module\s+(\S+)", read_text(repo / f) or "", re.M)
            if m:
                out.append((f, "go module", m.group(1)))
        elif name == "Cargo.toml":
            t = read_text(repo / f) or ""
            for m in re.finditer(r"\[\[bin\]\]\s*name\s*=\s*\"([^\"]+)\"", t):
                out.append((f, "cargo bin", m.group(1)))
        elif name in ("vercel.json", "netlify.toml", "fly.toml", "render.yaml", "app.yaml", "serverless.yml"):
            t = read_text(repo / f) or ""
            crons = re.findall(r'"path"\s*:\s*"([^"]+)"\s*,\s*"schedule"\s*:\s*"([^"]+)"', t)
            out.append((f, "deploy config", "; ".join(f"cron {p} at {s}" for p, s in crons) or "present"))
    return out


def ci(repo, files):
    out = []
    for f in files:
        if re.match(r"\.github/workflows/.+\.ya?ml$", f) or f in (".gitlab-ci.yml", ".circleci/config.yml",
                                                                    "azure-pipelines.yml", "Jenkinsfile", ".travis.yml"):
            t = read_text(repo / f) or ""
            name = re.search(r"^name:\s*(.+)$", t, re.M)
            trig = re.search(r"^on:\s*(.*?)(?=^\S)", t, re.M | re.S)
            trig_s = ", ".join(re.findall(r"^\s{2}(\w+):", trig.group(1), re.M)) if trig else ""
            if trig and not trig_s:
                trig_s = trig.group(1).strip().replace("\n", " ")[:60]
            cron = re.findall(r"cron:\s*['\"]([^'\"]+)", t)
            runs = [r.strip() for r in re.findall(r"^\s*(?:-\s*)?run:\s*[|>]?\s*(.+)$", t, re.M) if r.strip() not in "|>"]
            runs += [s.strip() for s in re.findall(r"^\s+-\s+(?!uses|name|with)(\w[^:\n]*)$", t, re.M)][:0]
            out.append({"file": f, "name": name.group(1).strip() if name else "", "on": trig_s,
                        "cron": cron, "runs": runs[:6]})
    return out


def scan_code(repo, files, top):
    hits = defaultdict(list)
    routes, data, env, test_files = [], [], defaultdict(list), []
    compiled = {k: [(re.compile(p, re.M), label) for p, label in v] for k, v in PATTERNS.items()}
    data_c = [(re.compile(p, re.M), label, exts) for p, label, exts in DATA_PATTERNS]
    click_rx = re.compile(r"""^\s*@([\w.]+)\.(command|group)\((?:\s*(?:name\s*=\s*)?['"]([\w:-]+)['"])?""", re.M)
    def_rx = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)", re.M)
    pydantic = Counter()
    for f in files:
        e = ext(f)
        if is_test_path(f):
            test_files.append(f)
            continue
        if e not in CODE and e not in {".prisma", ".sql"} and not (e == "" and "/bin/" in "/" + f):
            continue
        text = read_text(repo / f)
        if text is None:
            continue
        line_of = lambda pos: text.count("\n", 0, pos) + 1
        if e in CODE or e == "":
            for kind, pats in compiled.items():
                for rx, label in pats:
                    if (label == "commander/yargs command" and e not in JS_EXT) or (label == "cobra command" and e != ".go"):
                        continue
                    for m in rx.finditer(text):
                        if kind == "main" and label == "shebang script" and m.start() != 0:
                            continue
                        detail = m.group(1) if (m.groups() and kind == "cli" and m.group(1)) else ""
                        hits[kind].append((f"{f}:{line_of(m.start())}", label, detail))
            if e == ".py":
                for m in click_rx.finditer(text):
                    label = "click/typer group" if m.group(2) == "group" else "click/typer command"
                    d = def_rx.search(text, m.end())
                    name = m.group(3) or (d.group(1).replace("_", "-") if d else "")
                    hits["cli"].append((f"{f}:{line_of(m.start())}", label, name))
            n = 0
            if e == ".py":
                n = len(re.findall(r"^class\s+\w+\((?:[\w.]*BaseModel|BaseSettings)\b|^@dataclass", text, re.M))
            elif e in JS_EXT:
                n = len(re.findall(r"^export\s+(?:interface|type)\s+\w+", text, re.M))
            elif e == ".go":
                n = len(re.findall(r"^type\s+\w+\s+struct\b", text, re.M))
            if n:
                pydantic[f] += n
            for rx, flavor in ROUTE_PATTERNS:
                if flavor == "django" and not f.endswith("urls.py"):
                    continue
                for m in rx.finditer(text):
                    g = m.groups()
                    if flavor == "django":
                        method, path = "ANY", g[0]
                    elif flavor == "decorator":
                        method, path = g[0].upper(), g[1]
                    else:
                        method, path = g[1].upper(), g[2]
                    if method in ("ROUTE", "API_ROUTE", "HANDLEFUNC", "HANDLE", "ALL", "USE"):
                        method = "ANY"
                    routes.append((method, path or "/", f"{f}:{line_of(m.start())}"))
            for rx in ENV_PATTERNS:
                for m in rx.finditer(text):
                    name = next(g for g in m.groups() if g)
                    env[name].append(f"{f}:{line_of(m.start())}")
        for rx, label, exts in data_c:
            if e in exts:
                for m in rx.finditer(text):
                    name = m.group(1) if m.groups() else PurePosixPath(f).stem
                    data.append((label, name, f"{f}:{line_of(m.start())}"))
    # File-system routes (Next.js, SvelteKit, Remix, Nuxt) are found by path, not by code.
    for f in files:
        m = re.search(r"(?:^|/)app/(.*?)/?route\.(ts|js|tsx|jsx)$", f)
        if m:
            text = read_text(repo / f) or ""
            methods = re.findall(r"export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", text)
            routes.append((",".join(methods) or "ANY", "/" + re.sub(r"\([^/]*\)/?", "", m.group(1)), f + ":1"))
        m = re.search(r"(?:^|/)pages/(api/.*)\.(ts|js|tsx|jsx)$", f)
        if m:
            routes.append(("ANY", "/" + re.sub(r"/index$", "", m.group(1)), f + ":1"))
        m = re.search(r"(?:^|/)app/(.*?)/?page\.(tsx|jsx|ts|js|mdx)$", f)
        if m:
            routes.append(("PAGE", "/" + re.sub(r"\([^/]*\)/?", "", m.group(1)), f + ":1"))
        m = re.search(r"(?:^|/)src/routes/(.*?)/?\+(page|server)\.(svelte|ts|js)$", f)
        if m:
            routes.append(("PAGE" if m.group(2) == "page" else "ANY", "/" + m.group(1), f + ":1"))
    loc = lambda w: (w[0].rsplit(":", 1)[0], int(w[0].rsplit(":", 1)[1]))
    for k in hits:
        hits[k].sort(key=loc)
    return hits, routes, data, env, test_files, pydantic


def env_templates(repo, files):
    names = {}
    for f in files:
        n = PurePosixPath(f).name
        if re.match(r"\.env\.(example|sample|template|dist|defaults)$|^env\.example$|\.env\.local\.example$", n):
            for m in re.finditer(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]+)\s*=", read_text(repo / f) or "", re.M):
                names.setdefault(m.group(1), f)
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--under", default="")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--json")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    files = list_files(repo, a.under)

    man = manifests(repo, files)
    pipelines = ci(repo, files)
    hits, routes, data, env, tests, pydantic = scan_code(repo, files, a.top)
    templ = env_templates(repo, files)
    migrations = sorted(f for f in files if MIGRATION_DIR.search("/" + f) and ext(f) in {".py", ".sql", ".ts", ".js", ".rb", ".go"})
    readmes = sorted((f for f in files if re.match(
        r"(.*/)?([Rr]eadme|README|CONTRIBUTING|ARCHITECTURE|AGENTS|CLAUDE|DEVELOPMENT|HACKING|DESIGN)(\.\w+)?$", f)),
        key=lambda f: (f.count("/"), f))
    docs_adr = [f for f in files if re.search(r"(^|/)(adr|decisions|rfcs?)/", f, re.I)]

    T = a.top
    out = [f"# Entry points: {repo.name}{' / ' + a.under if a.under else ''}", "",
           f"{len(files)} files scanned. Read these sections in order: they go from how the project is run "
           "down to where each kind of work starts in the code.", ""]
    out += ["## Read first", ""]
    out += [f"- {f}" for f in readmes[:T]] + [f"- {f} (decision record)" for f in docs_adr[:10]] or ["_No README or contributor docs found._"]
    out += ["", "## Build, run and deploy (manifests)", ""]
    out.append(md_table(["file", "kind", "value"], [[f, k, v] for f, k, v in man[: T * 2]]) if man else "_None found._")
    out += ["", "## CI pipelines", ""]
    out.append(md_table(["file", "name", "triggers", "commands it runs"],
                        [[p["file"], p["name"], p["on"] + (f" (cron {', '.join(p['cron'])})" if p["cron"] else ""),
                          " ; ".join(p["runs"])] for p in pipelines]) if pipelines else "_None found._")
    for kind, title in (("main", "Main functions and scripts"), ("server", "Servers"),
                        ("cli", "CLI commands"), ("job", "Background jobs and schedules")):
        out += ["", f"## {title}", ""]
        rows = hits.get(kind, [])
        out.append(md_table(["where", "what", "name"], [[w, l, d] for w, l, d in rows[:T]]) if rows else "_None found._")
        if len(rows) > T:
            out.append(f"\n...and {len(rows) - T} more.")
    out += ["", f"## HTTP routes and pages ({len(routes)})", ""]
    routes.sort(key=lambda r: (r[1], r[0]))
    out.append(md_table(["method", "path", "where"], [list(r) for r in routes[: T * 2]]) if routes else "_None found._")
    if len(routes) > T * 2:
        out.append(f"\n...and {len(routes) - T * 2} more.")
    out += ["", "## Data model", ""]
    if migrations:
        out.append(f"- {len(migrations)} migration files. Oldest: `{migrations[0]}`. Newest: `{migrations[-1]}`. "
                   "Read the newest few to see what changed recently, the oldest to see the original core tables.")
    out.append(md_table(["kind", "name", "where"], [list(d) for d in data[: T * 2]]) if data else
               "_No ORM models, schema files or CREATE TABLE statements found. The data may live in an external "
               "service, in plain classes (dataclasses, pydantic, TS types), or in JSON files._")
    if pydantic:
        out.append(f"\n{sum(pydantic.values())} data shapes (pydantic models, dataclasses, exported TS types, Go structs) "
                   f"in {len(pydantic)} files. They show what the data looks like in memory and on the wire. "
                   "Files with the most:\n")
        out.append(md_table(["file", "shapes"], [[f, n] for f, n in pydantic.most_common(10)]))
    out += ["", f"## Config read from the environment ({len(set(env) | set(templ))} names)", ""]
    names = sorted(set(env) | set(templ), key=lambda n: (-len(env.get(n, [])), n))
    out.append(md_table(["name", "read in", "first place", "in a .env template"],
                        [[n, f"{len(env.get(n, []))} place{'' if len(env.get(n, [])) == 1 else 's'}", (env.get(n) or ["-"])[0],
                          ("yes" if n in templ else "NO") if templ else "no template"]
                         for n in names[:T]]) if names else "_None found._")
    if any(n not in templ for n in env) and templ:
        out.append("\n`NO` in the last column means the code reads it but the template does not mention it: a setup gotcha.")
    out += ["", f"## Tests ({len(tests)} files)", ""]
    by_dir = Counter("/".join(t.split("/")[:2]) if "/" in t else "(root)" for t in tests)
    out.append(md_table(["folder", "test files"], [[d, c] for d, c in by_dir.most_common(15)]) if tests else
               "_No test files found. Ask how changes are checked before shipping._")
    print("\n".join(out))
    if a.json:
        json.dump({"manifests": man, "ci": pipelines, "hits": hits, "routes": routes, "data": data,
                   "env": env, "env_templates": templ, "migrations": migrations, "tests": tests},
                  open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
