#!/usr/bin/env python3
"""Build the internal import graph of a repo (Python, JS/TS, Go), collapse it to folders, and report the
layers, cycles, load-bearing files, likely dead files and main external dependencies. Emits Mermaid and DOT.

Usage:
  python3 deps.py <repo> [--under src] [--max-nodes 18 | --depth 2] [--tests] [--min-weight 1]
                  [--mermaid deps.mmd] [--dot deps.dot] [--json deps.json]

Python imports are read with the `ast` module (regex fallback for syntax this Python cannot parse).
JS/TS and Go imports are read with regexes. Resolution understands Python source roots and relative
imports, tsconfig/jsconfig `paths` and `baseUrl`, workspace package names, and go.mod module paths.
Standard library + git only (Python 3.9+). Read-only.
"""
import argparse
import ast
import json
import math
import os
import re
import sys
import sysconfig
from collections import Counter, defaultdict, deque
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import is_test_path, list_files, md_table, read_text  # noqa: E402

PY = {".py"}
JS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}
GO = {".go"}
JS_PROBE = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".json"]
NODE_BUILTINS = set(
    "assert buffer child_process cluster crypto dgram dns events fs http http2 https net os path perf_hooks "
    "process querystring readline stream string_decoder timers tls tty url util v8 vm worker_threads zlib "
    "async_hooks module inspector constants punycode".split())
ENTRY_NAMES = re.compile(
    r"^(main|index|app|server|cli|__main__|manage|setup|conftest|wsgi|asgi|run|start|worker|"
    r"middleware|instrumentation|layout|page|route|loading|error|not-found|template|default)\.\w+$"
    r"|\.config\.\w+$|^_app\.|^_document\.|^\+")
FRAMEWORK_DIRS = re.compile(
    r"(^|/)(app|pages|routes|scripts|bin|cmd|migrations|alembic|examples?|docs|templates|plugins|"
    r"commands|management|fixtures|stories|storybook|\.storybook|public|static)/")


def ext_of(p):
    return os.path.splitext(p)[1].lower()


# ---------- Python ----------

def py_imports(text):
    """Yield (module, names, level, kind). kind is runtime, lazy (inside a function) or type (TYPE_CHECKING)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        yield from py_imports_regex(text)
        return

    def walk(stmts, kind):
        # Imports are statements, so only statement lists are walked, not expressions.
        for st in stmts:
            if isinstance(st, ast.Import):
                for al in st.names:
                    yield al.name, [], 0, kind
            elif isinstance(st, ast.ImportFrom):
                yield st.module or "", [al.name for al in st.names], st.level, kind
            elif isinstance(st, ast.If) and "TYPE_CHECKING" in ast.dump(st.test):
                yield from walk(st.body, "type")
                yield from walk(st.orelse, kind)
            else:
                k = "lazy" if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and kind == "runtime" else kind
                for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                    sub = getattr(st, field, None)
                    if isinstance(sub, list):
                        walk_list = [x for x in sub if isinstance(x, ast.stmt)]
                        # except handlers and match cases hold their own statement bodies
                        for x in sub:
                            if not isinstance(x, ast.stmt) and hasattr(x, "body"):
                                walk_list.extend(x.body)
                        yield from walk(walk_list, k)

    yield from walk(tree.body, "runtime")


def py_imports_regex(text):
    for m in re.finditer(r"^([ \t]*)from\s+(\.*)([\w.]*)\s+import\s+\(?([\w\s,*]+)", text, re.M):
        names = [n.strip().split(" as ")[0] for n in m.group(4).split(",") if n.strip()]
        yield m.group(3), names, len(m.group(2)), "lazy" if m.group(1) else "runtime"
    for m in re.finditer(r"^([ \t]*)import\s+([\w.]+(?:\s*,\s*[\w.]+)*)", text, re.M):
        for mod in m.group(2).split(","):
            yield mod.strip(), [], 0, "lazy" if m.group(1) else "runtime"


def stdlib_names():
    names = set(sys.builtin_module_names) | {"__future__", "typing_extensions"}
    lib = sysconfig.get_paths().get("stdlib", "")
    for d in (lib, os.path.join(lib, "lib-dynload")):
        try:
            for n in os.listdir(d):
                if n.endswith(".py"):
                    names.add(n[:-3])
                elif n.endswith((".so", ".pyd")):
                    names.add(n.split(".")[0])
                elif os.path.isdir(os.path.join(d, n)) and n.isidentifier():
                    names.add(n)
        except OSError:
            pass
    names |= {"zoneinfo", "graphlib", "tomllib", "_thread"}  # newer than some interpreters this may run on
    names.discard("site-packages")
    return names


class PyResolver:
    def __init__(self, files, alldirs_with_py):
        self.files = set(files)
        pkg_dirs = {str(PurePosixPath(f).parent) for f in files if f.endswith("/__init__.py") or f == "__init__.py"}
        roots = {""}
        for d in pkg_dirs:
            top = d
            while str(PurePosixPath(top).parent) in pkg_dirs and str(PurePosixPath(top).parent) != top:
                top = str(PurePosixPath(top).parent)
            parent = str(PurePosixPath(top).parent)
            roots.add("" if parent == "." else parent)
        for d in alldirs_with_py:
            if PurePosixPath(d).name == "src":
                roots.add(d)
        self.roots = roots
        self.stems = Counter(PurePosixPath(f).stem for f in files)
        self.index = defaultdict(list)  # dotted name -> files or dir pseudo-nodes ("pkg/sub/")
        for f in files:
            for r in roots:
                if r and not f.startswith(r + "/"):
                    continue
                rel = f[len(r) + 1:] if r else f
                parts = rel[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if parts and all(p.isidentifier() for p in parts):
                    self.index[".".join(parts)].append(f)
        for d in alldirs_with_py:  # namespace packages (folders of .py files without __init__.py)
            if d in pkg_dirs:
                continue
            for r in roots:
                if r and not d.startswith(r + "/"):
                    continue
                parts = (d[len(r) + 1:] if r else d).split("/")
                if parts and all(p.isidentifier() for p in parts):
                    self.index[".".join(parts)].append(d + "/")

    def pick(self, cands, importer):
        def score(c):
            common = os.path.commonprefix([c, importer]).count("/")
            return (-common, c.count("/"), c)
        return sorted(set(cands), key=score)[0]

    def resolve(self, importer, module, names, level):
        """Return (internal targets, external top-level name or None)."""
        pkg = PurePosixPath(importer).parent
        if level:
            base = pkg
            for _ in range(level - 1):
                base = base.parent
            basestr = "" if str(base) == "." else str(base)
            stem = "/".join(x for x in [basestr, module.replace(".", "/")] if x)
            out = []
            for n in names or [None]:
                for c in ([f"{stem}/{n}.py", f"{stem}/{n}/__init__.py"] if n and n != "*" else []) + \
                         [f"{stem}.py", f"{stem}/__init__.py"]:
                    if c in self.files:
                        out.append(c)
                        break
            return out, None
        # Absolute import: the importer's own folder first (scripts import siblings), then source roots.
        mod_names = [f"{module}.{n}" for n in names if n != "*"] + [module] if names else \
            [".".join(module.split(".")[:i]) for i in range(module.count(".") + 1, 0, -1)]
        out = []
        for m in mod_names:
            local = "/".join(x for x in [str(pkg) if str(pkg) != "." else "", m.replace(".", "/")] if x)
            for c in (local + ".py", local + "/__init__.py"):
                if c in self.files and c != importer:
                    out.append(c)
                    break
            else:
                if m in self.index:
                    out.append(self.pick(self.index[m], importer))
            if out and not names:
                break
        if not out:
            return [], module.split(".")[0]
        return list(dict.fromkeys(out)), None


# ---------- JS / TS ----------

JS_IMPORT = re.compile(
    r"""(?:^|[;\s])(import|export)\s+(type\s+)?(?:[\w*{}\s,$]+?\s+from\s+)?['"]([^'"\n]+)['"]"""
    r"""|\brequire\(\s*['"]([^'"\n]+)['"]\s*\)|\bimport\(\s*['"]([^'"\n]+)['"]\s*\)""", re.M)


def strip_js_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def js_imports(text):
    for m in JS_IMPORT.finditer(strip_js_comments(text)):
        if m.group(3):
            yield m.group(3), "type" if m.group(2) else "runtime"
        elif m.group(4):
            yield m.group(4), "runtime"
        elif m.group(5):
            yield m.group(5), "lazy"


def load_jsonish(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r'(?m)^\s*//.*$|(?<=[,{\[\s])//[^\n"]*$', "", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except ValueError:
        return {}


class JsResolver:
    def __init__(self, repo, files, all_files):
        self.files = set(all_files)
        self.configs = {}  # dir -> (baseUrl dir, [(prefix, suffix, [targets])])
        for f in all_files:
            name = PurePosixPath(f).name
            if re.match(r"(tsconfig|jsconfig)(\.\w+)?\.json$", name):
                d = str(PurePosixPath(f).parent)
                d = "" if d == "." else d
                if d in self.configs and name not in ("tsconfig.json", "jsconfig.json"):
                    continue
                opts = self.compiler_options(repo, f, 0)
                if opts is not None:
                    self.configs[d] = opts
        self.workspaces = {}
        for f in all_files:
            if PurePosixPath(f).name == "package.json":
                data = load_jsonish(read_text(Path(repo) / f) or "")
                if isinstance(data, dict) and data.get("name"):
                    d = str(PurePosixPath(f).parent)
                    self.workspaces[data["name"]] = ("" if d == "." else d, data)

    def compiler_options(self, repo, f, depth):
        data = load_jsonish(read_text(Path(repo) / f) or "")
        if not isinstance(data, dict):
            return None
        d = str(PurePosixPath(f).parent)
        d = "" if d == "." else d
        base, paths = None, []
        ext = data.get("extends")
        if isinstance(ext, str) and ext.startswith(".") and depth < 3:
            parent = os.path.normpath(os.path.join(d, ext)).replace(os.sep, "/")
            if not parent.endswith(".json"):
                parent += ".json"
            if parent in self.files:
                got = self.compiler_options(repo, parent, depth + 1)
                if got:
                    base, paths = got
        co = data.get("compilerOptions") or {}
        if co.get("baseUrl"):
            base = os.path.normpath(os.path.join(d, co["baseUrl"])).replace(os.sep, "/")
            base = "" if base == "." else base
        if co.get("paths"):
            root = base if base is not None else d
            paths = []
            for pat, targets in co["paths"].items():
                pre, _, suf = pat.partition("*")
                tg = [os.path.normpath(os.path.join(root, t)).replace(os.sep, "/") for t in targets]
                paths.append((pre, suf, tg, "*" in pat))
        return base, paths

    def probe(self, stem):
        stem = stem.lstrip("./") if stem.startswith("./") else stem
        stem = os.path.normpath(stem).replace(os.sep, "/")
        if stem in (".", ""):
            stem = ""
        if stem in self.files and (ext_of(stem) in JS or ext_of(stem) == ".json"):
            return stem
        if re.search(r"\.(m|c)?jsx?$", stem):  # TS ESM style: './x.js' points at x.ts
            bare = re.sub(r"\.(m|c)?jsx?$", "", stem)
            for e in (".ts", ".tsx", ".mts", ".cts"):
                if bare + e in self.files:
                    return bare + e
        for e in JS_PROBE:
            if stem + e in self.files:
                return stem + e
        for e in JS_PROBE:
            idx = (stem + "/index" if stem else "index") + e
            if idx in self.files:
                return idx
        return None

    def config_for(self, importer):
        d = str(PurePosixPath(importer).parent)
        while True:
            d = "" if d == "." else d
            if d in self.configs:
                return self.configs[d]
            if not d:
                return None
            d = str(PurePosixPath(d).parent)

    def resolve(self, importer, spec):
        if spec.startswith("."):
            hit = self.probe(os.path.join(str(PurePosixPath(importer).parent), spec))
            return hit, None
        if spec.startswith("node:") or spec.split("/")[0] in NODE_BUILTINS:
            return None, "node:" + spec.replace("node:", "").split("/")[0]
        cfg = self.config_for(importer)
        if cfg:
            base, paths = cfg
            for pre, suf, targets, wild in paths:
                if (wild and spec.startswith(pre) and spec.endswith(suf)) or (not wild and spec == pre):
                    mid = spec[len(pre): len(spec) - len(suf) if suf else None] if wild else ""
                    for t in targets:
                        hit = self.probe(t.replace("*", mid))
                        if hit:
                            return hit, None
            if base is not None:
                hit = self.probe(os.path.join(base, spec) if base else spec)
                if hit:
                    return hit, None
        for name, (d, data) in self.workspaces.items():
            if spec == name or spec.startswith(name + "/"):
                sub = spec[len(name) + 1:]
                cands = [os.path.join(d, sub), os.path.join(d, "src", sub)] if sub else \
                    [os.path.join(d, str(data.get(k))) for k in ("source", "module", "main") if data.get(k)] + \
                    [os.path.join(d, "src/index"), os.path.join(d, "index"), d]
                for c in cands:
                    hit = self.probe(c)
                    if hit:
                        return hit, None
                return None, name
        if spec.startswith(("@/", "~/")):  # common bundler alias for src/ without a tsconfig
            for c in ("src/" + spec[2:], spec[2:]):
                hit = self.probe(c)
                if hit:
                    return hit, None
        parts = spec.split("/")
        return None, "/".join(parts[:2]) if spec.startswith("@") else parts[0]


# ---------- Go ----------

GO_BLOCK = re.compile(r"^import\s*\((.*?)\)", re.S | re.M)
GO_SINGLE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)


def go_imports(text):
    for m in GO_BLOCK.finditer(text):
        for s in re.findall(r'"([^"]+)"', m.group(1)):
            yield s
    yield from GO_SINGLE.findall(text)


# ---------- graph helpers ----------

def tarjan(nodes, edges):
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]
    sys.setrecursionlimit(max(10000, len(nodes) * 4))

    def strong(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in edges.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(comp)

    for v in nodes:
        if v not in index:
            strong(v)
    return out


def shortest_cycle(start, comp, edges):
    comp = set(comp)
    prev = {start: None}
    q = deque([start])
    while q:
        v = q.popleft()
        for w in edges.get(v, ()):
            if w == start:
                path = [v]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])
                return list(reversed(path)) + [start]
            if w in comp and w not in prev:
                prev[w] = v
                q.append(w)
    return [start]


def folder_of(unit):
    if unit.endswith("/"):
        return unit.rstrip("/")
    d = str(PurePosixPath(unit).parent)
    return "" if d == "." else d


partial = set()  # folders split only partly; their node also holds the subfolders left inside


def choose_nodes(units, base, max_nodes, depth):
    """Pick the folders that become graph nodes. Fixed depth, or adaptive: keep splitting the biggest
    folder into its subfolders while the node count stays under max_nodes."""
    folders = Counter(folder_of(u) for u in units)
    if depth:
        bdepth = len(base.split("/")) if base else 0
        return {"/".join(f.split("/")[: bdepth + depth]) if f else "" for f in folders}

    # Folder tree with recursive file counts, built once.
    kids_of, size = defaultdict(set), Counter()
    for f, c in folders.items():
        parts = f.split("/") if f else []
        for i in range(len(parts) + 1):
            anc = "/".join(parts[:i])
            size[anc] += c
            if i < len(parts):
                kids_of[anc].add("/".join(parts[: i + 1]))

    nodes, frozen = {base}, set()
    while True:
        # Folders under 10 files are not worth splitting; they read fine as one box.
        cands = sorted((n for n in nodes if n not in frozen and kids_of.get(n) and size[n] >= 10),
                       key=lambda n: -size[n])
        if not cands:
            return nodes
        n = cands[0]
        new = (nodes - {n}) | kids_of[n] | ({n} if folders.get(n) else set())
        if len(new) > max_nodes:
            # Too many subfolders: split off only the biggest ones. The rest stay inside n,
            # which then reads as "n (rest)".
            room = max_nodes - len(nodes)
            if room >= 2:
                biggest = sorted(kids_of[n], key=lambda k: -size[k])[:room]
                nodes = nodes | set(biggest)
                partial.add(n)
            frozen.add(n)
            continue
        frozen.add(n)  # expanded; it stays only as the node for its own direct files
        nodes = new


def node_for(unit, nodes):
    f = folder_of(unit)
    best = None
    for n in nodes:
        if n == f or n == "" or f.startswith(n + "/"):
            if best is None or len(n) > len(best):
                best = n
    return best


def render_svg(nodes, level_of, shown, cyc_edges, label, count):
    """Layered SVG: one row per layer, top layer first, rows ordered to reduce crossings. Stdlib only."""
    from html import escape
    rows = defaultdict(list)
    for n in nodes:
        if count(n) or any(n in k for k in shown):
            rows[level_of(n)].append(n)
    nbrs = defaultdict(set)
    for x, y in shown:
        nbrs[x].add(y)
        nbrs[y].add(x)
    for r in rows.values():
        r.sort()
    pos = {}
    for _ in range(4):  # barycenter sweeps
        for lv in rows:
            for i, n in enumerate(rows[lv]):
                pos[n] = i
        for lv in rows:
            def bary(n):
                ps = [pos[m] for m in nbrs[n] if m in pos and level_of(m) != lv]
                return sum(ps) / len(ps) if ps else pos[n]
            rows[lv].sort(key=bary)
    # Wrap wide layers (a big cycle puts many folders on one level) into rows of at most 5.
    wrapped, row_of = [], {}
    for lv in sorted(rows, reverse=True):
        r = rows[lv]
        for i in range(0, len(r), 5):
            wrapped.append(r[i:i + 5])
    rows = dict(enumerate(wrapped))
    levels = sorted(rows)
    for i, r in rows.items():
        for n in r:
            row_of[n] = i
    char_w, box_h, gap_x, gap_y, pad = 7.2, 44, 28, 90, 30
    width_of = {n: max(90, len(label(n)) * char_w + 24) for r in rows.values() for n in r}
    row_w = {lv: sum(width_of[n] for n in rows[lv]) + gap_x * (len(rows[lv]) - 1) for lv in levels}
    total_w = max(row_w.values(), default=200) + 2 * pad
    xy = {}
    for i, lv in enumerate(levels):
        x = (total_w - row_w[lv]) / 2
        for n in rows[lv]:
            xy[n] = (x, pad + i * (box_h + gap_y))
            x += width_of[n] + gap_x
    total_h = pad * 2 + len(levels) * box_h + (len(levels) - 1) * gap_y
    maxw = max((sum(k.values()) for k in shown.values()), default=1)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" height="{total_h:.0f}" '
         f'viewBox="0 0 {total_w:.0f} {total_h:.0f}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
         '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto">'
         '<path d="M0,0 L10,5 L0,10 z" fill="#888"/></marker>'
         '<marker id="r" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" markerUnits="userSpaceOnUse" orient="auto">'
         '<path d="M0,0 L10,5 L0,10 z" fill="#d33"/></marker></defs>',
         f'<rect width="100%" height="100%" fill="#fff"/>']
    for (x_, y_), k in sorted(shown.items()):
        if x_ not in xy or y_ not in xy:
            continue
        (ax, ay), (bx, by) = xy[x_], xy[y_]
        ax += width_of[x_] / 2
        bx += width_of[y_] / 2
        if row_of[y_] > row_of[x_]:
            ay = ay + box_h
        elif row_of[y_] < row_of[x_]:
            by = by + box_h
        else:
            ay, by = ay + box_h, by + box_h
        mid = (ay + by) / 2 if by != ay else ay + 30
        red = (x_, y_) in cyc_edges
        w = 1 + 3 * math.log1p(sum(k.values())) / math.log1p(maxw)
        dash = '' if k["runtime"] else ' stroke-dasharray="5,4"'
        color = "#d33" if red else "#888"
        o.append(f'<path d="M{ax:.0f},{ay:.0f} C{ax:.0f},{mid:.0f} {bx:.0f},{mid:.0f} {bx:.0f},{by:.0f}" fill="none" '
                 f'stroke="{color}" stroke-width="{w:.1f}"{dash} marker-end="url(#{"r" if red else "a"})" opacity="0.8"/>')
        # Label near the arrowhead (t = 0.8 on the curve) so labels of edges sharing a source do not stack.
        t = 0.8
        lx = (1 - t) ** 3 * ax + 3 * (1 - t) ** 2 * t * ax + 3 * (1 - t) * t * t * bx + t ** 3 * bx
        ly = (1 - t) ** 3 * ay + 3 * (1 - t) ** 2 * t * mid + 3 * (1 - t) * t * t * mid + t ** 3 * by
        o.append(f'<text x="{lx + 4:.0f}" y="{ly:.0f}" fill="{color}" font-size="10">{sum(k.values())}</text>')
    for n, (x, y) in xy.items():
        o.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{width_of[n]:.0f}" height="{box_h}" rx="8" fill="#f4f1ea" stroke="#333"/>')
        o.append(f'<text x="{x + width_of[n] / 2:.0f}" y="{y + 19:.0f}" text-anchor="middle" font-weight="bold">{escape(label(n))}</text>')
        if count(n):
            o.append(f'<text x="{x + width_of[n] / 2:.0f}" y="{y + 34:.0f}" text-anchor="middle" fill="#666" font-size="10">{count(n)} files</text>')
    o.append("</svg>")
    return "\n".join(o)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--under", default="", help="only map files under this folder")
    ap.add_argument("--max-nodes", type=int, default=18)
    ap.add_argument("--depth", type=int, default=0, help="fixed folder depth instead of adaptive splitting")
    ap.add_argument("--tests", action="store_true", help="include test files (left out by default)")
    ap.add_argument("--min-weight", type=int, default=1, help="hide folder edges with fewer imports than this")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--mermaid")
    ap.add_argument("--dot")
    ap.add_argument("--svg", help="write a rendered SVG of the folder graph (no extra tools needed)")
    ap.add_argument("--json")
    a = ap.parse_args()

    repo = Path(a.repo).resolve()
    under = a.under.strip("/")
    all_files = list_files(repo)
    scope = [f for f in all_files if (not under or f.startswith(under + "/"))]
    src = [f for f in scope if ext_of(f) in PY | JS | GO and (a.tests or not is_test_path(f))]
    if not src:
        sys.exit("no Python, JS/TS or Go files found")
    lang = Counter("python" if ext_of(f) in PY else "js/ts" if ext_of(f) in JS else "go" for f in src)

    py_files = [f for f in all_files if ext_of(f) in PY]
    py_dirs = {folder_of(f) for f in py_files} - {""}
    pyr = PyResolver(py_files, py_dirs) if any(ext_of(f) in PY for f in src) else None
    jsr = JsResolver(repo, src, all_files) if any(ext_of(f) in JS for f in src) else None
    gomods = {}
    for f in all_files:
        if PurePosixPath(f).name == "go.mod":
            m = re.search(r"^module\s+(\S+)", read_text(repo / f) or "", re.M)
            if m:
                gomods[folder_of(f)] = m.group(1)
    stdlib = stdlib_names()

    edges = defaultdict(lambda: Counter())  # (from, to) -> kind counts
    external = Counter()
    texts = {}
    for f in src:
        text = read_text(repo / f)
        if text is None:
            continue
        texts[f] = text
        e = ext_of(f)
        if e in PY:
            for mod, names, level, kind in py_imports(text):
                hits, ext_name = pyr.resolve(f, mod, names, level)
                for h in hits:
                    if h != f:
                        edges[(f, h)][kind] += 1
                if ext_name and ext_name not in stdlib:
                    # A bare name that matches a file in the repo is usually loaded by editing sys.path.
                    local = pyr.stems.get(ext_name, 0) >= 1
                    external[("python (local file, sys.path)" if local else "python", ext_name)] += 1
        elif e in JS:
            for spec, kind in js_imports(text):
                hit, ext_name = jsr.resolve(f, spec)
                if hit and hit != f:
                    edges[(f, hit)][kind] += 1
                elif ext_name and not ext_name.startswith("node:"):
                    external[("js", ext_name)] += 1
        elif e in GO:
            d = folder_of(f)
            mod_dir = max((m for m in gomods if m == "" or d == m or d.startswith(m + "/")), key=len, default=None)
            for spec in go_imports(text):
                modpath = gomods.get(mod_dir) if mod_dir is not None else None
                if modpath and (spec == modpath or spec.startswith(modpath + "/")):
                    target = "/".join(x for x in [mod_dir, spec[len(modpath) + 1:]] if x) + "/"
                    if target.rstrip("/") != d:
                        edges[(f, target)]["runtime"] += 1
                elif "." in spec.split("/")[0]:
                    external[("go", "/".join(spec.split("/")[:3]))] += 1

    in_scope = lambda u: (not under or u.startswith(under + "/")) and (a.tests or not is_test_path(u))
    edges = {k: v for k, v in edges.items() if in_scope(k[1])}
    units = set(src) | {t for _, t in edges}
    nodes = choose_nodes(units, under, a.max_nodes, a.depth)
    unit_node = {u: node_for(u, nodes) for u in units}

    # Folder graph.
    fedges = defaultdict(Counter)
    for (x, y), kinds in edges.items():
        nx_, ny = unit_node[x], unit_node[y]
        if nx_ is None or ny is None or nx_ == ny:
            continue
        for k, v in kinds.items():
            fedges[(nx_, ny)][k] += v
    files_per_node = Counter(unit_node[u] for u in src)
    runtime_adj = defaultdict(set)
    for (x, y), k in fedges.items():
        if k["runtime"]:
            runtime_adj[x].add(y)
    sccs = tarjan(sorted(nodes), runtime_adj)
    comp_of = {n: i for i, c in enumerate(sccs) for n in c}
    folder_cycles = [c for c in sccs if len(c) > 1]

    # Layers: level 0 depends on nothing internal; each level depends only on lower ones.
    cadj = defaultdict(set)
    for x, ys in runtime_adj.items():
        for y in ys:
            if comp_of[x] != comp_of[y]:
                cadj[comp_of[x]].add(comp_of[y])
    level = {}

    def lvl(c):
        if c not in level:
            level[c] = 0
            level[c] = 1 + max((lvl(d) for d in cadj[c]), default=-1)
        return level[c]
    for i in range(len(sccs)):
        lvl(i)
    layers = defaultdict(list)
    for n in nodes:
        layers[level[comp_of[n]]].append(n)

    fan_in, fan_out = Counter(), Counter()
    for (x, y) in fedges:
        fan_out[x] += 1
        fan_in[y] += 1

    # File level: load-bearing files, cycles, orphans.
    importers = defaultdict(set)
    file_adj = defaultdict(set)
    for (x, y), k in edges.items():
        importers[y].add(x)
        if k["runtime"]:
            file_adj[x].add(y)
    file_sccs = [c for c in tarjan(sorted(units), file_adj) if len(c) > 1]
    file_sccs.sort(key=len)
    load_bearing = sorted(((u, len(s)) for u, s in importers.items() if not u.endswith("/")), key=lambda t: -t[1])
    # A Go package (folder) is imported as a whole, so files in an imported folder count as used.
    used_dirs = {u.rstrip("/") for u in units if u.endswith("/")}
    orphans = []
    for f in src:
        name = PurePosixPath(f).name
        if importers.get(f) or ENTRY_NAMES.search(name) or FRAMEWORK_DIRS.search("/" + f) or is_test_path(f):
            continue
        if ext_of(f) in GO and (folder_of(f) in used_dirs or "package main" in texts.get(f, "")):
            continue
        if ext_of(f) in PY and (name == "__init__.py" or re.search(r"__name__\s*==\s*['\"]__main__", texts.get(f, ""))):
            continue
        orphans.append(f)

    # Labels: strip the common prefix so node names stay short.
    shown_nodes = [n for n in nodes if n]
    label_base = os.path.commonpath(shown_nodes) if len(shown_nodes) > 1 and all(shown_nodes) else under
    label_base = under if (label_base in ("", ".") or label_base in nodes) else label_base
    def lbl(n):
        if label_base and n == label_base:
            name = PurePosixPath(n).name
        else:
            name = (n[len(label_base) + 1:] if label_base and n.startswith(label_base + "/") else n) or "(root)"
        return name + " (rest)" if n in partial else name

    def nid(n):
        return "n_" + re.sub(r"\W", "_", n or "root")

    shown = {k: v for k, v in fedges.items() if sum(v.values()) >= a.min_weight}
    cyc_edges = {(x, y) for (x, y) in shown if comp_of[x] == comp_of[y] and len(sccs[comp_of[x]]) > 1}
    mm = ["flowchart TD"]
    for n in sorted(nodes, key=lambda n: (-level[comp_of[n]], n)):
        if files_per_node[n] or any(n in k for k in shown):
            count = f"<br/>{files_per_node[n]} files" if files_per_node[n] else ""
            mm.append(f'  {nid(n)}["{lbl(n)}{count}"]')
    link_i, red = 0, []
    for (x, y), k in sorted(shown.items()):
        total = sum(k.values())
        arrow = "-->" if k["runtime"] else "-.->"
        mm.append(f"  {nid(x)} {arrow}|{total}| {nid(y)}")
        if (x, y) in cyc_edges:
            red.append(str(link_i))
        link_i += 1
    if red:
        mm.append(f"  linkStyle {','.join(red)} stroke:#d33,stroke-width:2px")
    mermaid = "\n".join(mm)
    dot = ["digraph deps {", "  rankdir=TB; node [shape=box, style=rounded, fontname=Helvetica];"]
    for n in nodes:
        dot.append(f'  {nid(n)} [label="{lbl(n)}\\n{files_per_node[n]} files"];')
    for (x, y), k in sorted(shown.items()):
        style = "solid" if k["runtime"] else "dashed"
        color = "red" if (x, y) in cyc_edges else "black"
        dot.append(f'  {nid(x)} -> {nid(y)} [label="{sum(k.values())}", style={style}, color={color}];')
    dot.append("}")
    dot = "\n".join(dot)

    out = [f"# Import graph: {repo.name}{' / ' + under if under else ''}", ""]
    out.append(f"- {len(src)} source files ({', '.join(f'{k} {v}' for k, v in lang.most_common())})"
               f"{'' if a.tests else ', tests left out (add --tests)'}; "
               f"{sum(sum(v.values()) for v in edges.values())} internal imports between {len(edges)} file pairs.")
    if label_base and label_base != under:
        out.append(f"- Folder names below are relative to `{label_base}/`.")
    out.append(f"- Collapsed to {len(nodes)} folders. Solid arrow = runtime import, dashed = type-only or "
               f"inside a function. Red = part of a cycle. Arrow label = number of imports.")
    big = max(nodes, key=lambda n: files_per_node[n])
    if big and len(src) >= 50 and files_per_node[big] > 0.4 * len(src) and len(nodes) > 1:
        out.append(f"- `{lbl(big)}` holds {files_per_node[big]} of {len(src)} files. Map its insides with "
                   f"`--under {big}`.")
    out += ["", "## Architecture (folders)", "", "```mermaid", mermaid, "```", ""]
    out += ["## Layers (top depends on bottom)", "",
            "Level 0 imports nothing else in the repo: the foundation. Higher levels build on lower ones. "
            "Folders in the same cycle share a level.", ""]
    out.append(md_table(["level", "folders"],
                        [[lv, ", ".join(sorted(lbl(n) for n in layers[lv]))] for lv in sorted(layers, reverse=True)]))
    out += ["", "## Folders", ""]
    out.append(md_table(["folder", "files", "used by (folders)", "uses (folders)", "instability"],
                        [[lbl(n), files_per_node[n], fan_in[n], fan_out[n],
                          round(fan_out[n] / (fan_in[n] + fan_out[n]), 2) if fan_in[n] + fan_out[n] else "-"]
                         for n in sorted(nodes, key=lambda n: -files_per_node[n])]))
    out.append("\nInstability near 0 = many things depend on it and it depends on little (change it carefully). "
               "Near 1 = it depends on others and nothing depends on it (safe to change).")
    out += ["", "## Folder cycles (runtime imports only)", ""]
    if folder_cycles:
        for c in folder_cycles:
            inside = sorted(((x, y, fedges[(x, y)]["runtime"]) for x in c for y in c if fedges.get((x, y), {}).get("runtime")),
                            key=lambda t: t[2])
            light = ", ".join(f"{lbl(x)} -> {lbl(y)} ({w})" for x, y, w in inside[:5])
            out.append(f"- {len(c)} folders: {', '.join(sorted(lbl(n) for n in c))}. "
                       f"Lightest edges inside it (cheapest to cut): {light}.")
        out.append("\nA folder cycle with no matching file cycle means the folders are tangled as concepts, "
                   "but nothing breaks at import time. Treat them as one module when you reason about changes.")
    else:
        out.append("_None. The folder graph is a clean DAG._")
    out += ["", f"## File cycles (runtime imports only): {len(file_sccs)} groups", ""]
    for c in file_sccs[: a.top]:
        cyc = shortest_cycle(sorted(c)[0], c, file_adj)
        out.append(f"- ({len(c)} files) " + " -> ".join(cyc))
    if not file_sccs:
        out.append("_None._")
    out += ["", "## Load-bearing files (most distinct importers)", ""]
    out.append(md_table(["file", "imported by"], [[u, n] for u, n in load_bearing[: a.top]]))
    out += ["", f"## Possibly dead files: {len(orphans)} with no importers", "",
            "Not imported by anything, and not an entrypoint, test or framework-loaded file by name or folder. "
            "Check for dynamic imports, plugin registries, string-based loading and config before deleting.", ""]
    out += [f"- {o}" for o in orphans[: a.top * 2]] or ["_None._"]
    if len(orphans) > a.top * 2:
        out.append(f"- ...and {len(orphans) - a.top * 2} more (see --json)")
    out += ["", "## Most used external packages", ""]
    out.append(md_table(["lang", "package", "import statements"],
                        [[l, p, n] for (l, p), n in external.most_common(a.top)]))
    print("\n".join(out))

    if a.mermaid:
        Path(a.mermaid).write_text(mermaid + "\n")
    if a.dot:
        Path(a.dot).write_text(dot + "\n")
    if a.svg:
        Path(a.svg).write_text(render_svg(nodes, lambda n: level[comp_of[n]], shown, cyc_edges, lbl,
                                          lambda n: files_per_node[n]) + "\n")
    if a.json:
        json.dump({
            "repo": repo.name, "under": under, "nodes": sorted(nodes),
            "folder_edges": [{"from": x, "to": y, **k} for (x, y), k in fedges.items()],
            "file_edges": [{"from": x, "to": y, **k} for (x, y), k in edges.items()],
            "layers": {str(k): v for k, v in layers.items()}, "folder_cycles": folder_cycles,
            "file_cycles": file_sccs, "load_bearing": load_bearing[:100], "orphans": orphans,
            "external": [{"lang": l, "package": p, "count": n} for (l, p), n in external.most_common()],
        }, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
