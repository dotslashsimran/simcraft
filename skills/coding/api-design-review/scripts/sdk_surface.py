#!/usr/bin/env python3
"""Dump the public surface of a Python package or a TypeScript .d.ts file as a
tree, with ergonomics hints, so a reviewer can judge the SDK as a whole.

Usage:
  python3 sdk_surface.py <python.module.name> [--path DIR] [--depth 2]
  python3 sdk_surface.py path/to/index.d.ts
  options: --format text|json   --no-hints   --private

Python: the module is imported (so its import side effects run; only point it
at code you trust). "Public" means __all__ when defined, otherwise names
without a leading underscore that are defined in the package itself.

TypeScript: a lightweight parser reads exported functions, classes,
interfaces, types, enums, consts and namespaces from a .d.ts file. It does
not type-check and does not follow imports.

Hints flag the patterns that most often hurt SDK users: boolean flag
arguments, long positional parameter lists, mutable defaults, missing
docstrings and type annotations, 'any' in TypeScript, optional parameters
before required ones, timeouts that default to "wait forever", and
exception classes with no common base.
"""

import argparse
import importlib
import inspect
import json
import os
import pkgutil
import re
import sys
import types
from collections import Counter, OrderedDict

# ====================================================================== Python


class PySurface:
    def __init__(self, root_name, depth, include_private, hints):
        self.root_name = root_name
        self.depth = depth
        self.private = include_private
        self.want_hints = hints
        self.hints = []
        self.stats = Counter()
        self.exceptions = []

    def hint(self, where, kind, msg):
        if self.want_hints:
            self.hints.append(OrderedDict(where=where, kind=kind, hint=msg))

    def public(self, name):
        return self.private or not name.startswith("_")

    def run(self):
        mod = importlib.import_module(self.root_name)
        tree = self.module(mod, 0)
        self.exception_hints()
        return tree

    def module(self, mod, level):
        self.stats["modules"] += 1
        node = OrderedDict(kind="module", name=mod.__name__, doc=_first_line(mod.__doc__),
                           members=[])
        names = getattr(mod, "__all__", None)
        explicit = names is not None
        if names is None:
            names = sorted(n for n in vars(mod) if self.public(n))
        for name in names:
            if not hasattr(mod, name):
                continue
            obj = getattr(mod, name)
            if isinstance(obj, types.ModuleType):
                continue
            home = getattr(obj, "__module__", None)
            if not explicit and home and not (home == mod.__name__ or
                                              home.startswith(self.root_name + ".") or
                                              home == self.root_name or
                                              home.lstrip("_") == mod.__name__.lstrip("_")):
                continue  # re-exported from elsewhere (typing, os, ...)
            where = "%s.%s" % (mod.__name__, name)
            if inspect.isclass(obj):
                cnode = self.cls(obj, where)
                if obj.__name__ != name:
                    cnode["name"] = "%s = %s" % (name, obj.__name__)
                node["members"].append(cnode)
            elif callable(obj):
                node["members"].append(self.func(obj, where, name))
            else:
                self.stats["constants"] += 1
                node["members"].append(OrderedDict(kind="const", name=name,
                                                   type=type(obj).__name__,
                                                   value=_short_repr(obj)))
        if hasattr(mod, "__path__") and level < self.depth:
            for info in sorted(pkgutil.iter_modules(mod.__path__), key=lambda i: i.name):
                if not self.public(info.name) or info.name in ("tests", "test"):
                    continue
                full = "%s.%s" % (mod.__name__, info.name)
                try:
                    sub = importlib.import_module(full)
                except Exception as exc:  # noqa: BLE001
                    node["members"].append(OrderedDict(kind="module", name=full,
                                                       error="import failed: %s" % exc))
                    continue
                node["members"].append(self.module(sub, level + 1))
        return node

    def cls(self, c, where):
        self.stats["classes"] += 1
        bases = [b.__name__ for b in c.__bases__ if b is not object]
        is_exc = issubclass(c, BaseException)
        if is_exc:
            self.exceptions.append(c)
            self.stats["exceptions"] += 1
        node = OrderedDict(kind="exception" if is_exc else "class", name=c.__name__,
                           bases=bases, doc=_first_line(c.__doc__), members=[])
        if not c.__doc__ or (c.__doc__ and c.__doc__ == (c.__bases__[0].__doc__ if c.__bases__ else None)):
            self.stats["undocumented"] += 1
            if not is_exc:
                self.hint(where, "no-docstring", "Public class has no docstring.")
        init = c.__dict__.get("__init__")
        if init is not None and callable(init):
            node["init"] = self.signature(init, where + ".__init__", "__init__", method=True)
        for name, raw in c.__dict__.items():
            if name.startswith("_") and not self.private:
                continue
            mwhere = "%s.%s" % (where, name)
            if isinstance(raw, property):
                node["members"].append(OrderedDict(kind="property", name=name,
                                                   readonly=raw.fset is None,
                                                   type=_ann(_ret_ann(raw.fget))))
            elif isinstance(raw, (staticmethod, classmethod)):
                f = raw.__func__
                m = self.func(f, mwhere, name, method=isinstance(raw, classmethod))
                m["kind"] = "staticmethod" if isinstance(raw, staticmethod) else "classmethod"
                node["members"].append(m)
            elif inspect.isfunction(raw) or inspect.ismethoddescriptor(raw) or \
                    inspect.isbuiltin(raw):
                node["members"].append(self.func(raw, mwhere, name, method=True))
            elif not callable(raw):
                node["members"].append(OrderedDict(kind="attr", name=name,
                                                   type=type(raw).__name__,
                                                   value=_short_repr(raw)))
        return node

    def func(self, f, where, name, method=False):
        self.stats["methods" if method else "functions"] += 1
        node = OrderedDict(kind="method" if method else "function", name=name,
                           async_=inspect.iscoroutinefunction(f),
                           doc=_first_line(getattr(f, "__doc__", None)))
        node.update(self.signature(f, where, name, method))
        if not getattr(f, "__doc__", None):
            self.stats["undocumented"] += 1
            self.hint(where, "no-docstring", "Public callable has no docstring.")
        if re.search(r"[a-z][A-Z]", name) and not name.startswith("_"):
            self.hint(where, "naming", "camelCase name in Python. PEP 8 uses snake_case "
                      "for functions and methods.")
        return node

    def signature(self, f, where, name, method=False):
        try:
            sig = inspect.signature(f)
        except (TypeError, ValueError):
            return OrderedDict(sig="(...)")
        params = list(sig.parameters.values())
        if method and params and params[0].name in ("self", "cls"):
            params = params[1:]
        positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        annotated = sum(1 for p in params if p.annotation is not p.empty) + \
            (1 if sig.return_annotation is not sig.empty else 0)
        self.stats["params"] += len(params)
        self.stats["annotated"] += annotated
        self.stats["annotation_slots"] += len(params) + (0 if name == "__init__" else 1)
        if len(positional) > 4:
            self.hint(where, "long-positional",
                      "%d positional parameters (%s). Callers will pass them in the wrong order; "
                      "make the optional ones keyword-only with '*'." %
                      (len(positional), ", ".join(p.name for p in positional)))
        for p in positional:
            if isinstance(p.default, bool):
                self.hint(where, "bool-positional",
                          "'%s=%s' is a boolean flag that can be passed by position, so call "
                          "sites read as f(x, True). Make it keyword-only." % (p.name, p.default))
            if isinstance(p.default, (list, dict, set)):
                self.hint(where, "mutable-default",
                          "'%s' has a mutable default (%r), shared across calls." % (p.name, p.default))
        for p in params:
            if p.name in ("timeout", "timeout_s", "timeout_seconds", "read_timeout") \
                    and p.default is None:
                self.hint(where, "no-default-timeout",
                          "'%s' defaults to None, which usually means wait forever. A network "
                          "SDK should ship a finite default timeout." % p.name)
            elif p.name.startswith("timeout") and type(p.default) is object:
                self.hint(where, "no-default-timeout",
                          "'%s' defaults to a sentinel object, which usually means 'use the "
                          "global socket default', and that is no timeout unless the caller "
                          "set one. Calls can hang forever. Ship a finite default." % p.name)
        return OrderedDict(sig=_format_sig(sig, method))

    def exception_hints(self):
        if len(self.exceptions) < 2:
            return
        roots = set()
        for e in self.exceptions:
            mine = [b for b in e.__mro__[1:] if b in self.exceptions]
            if not mine:
                roots.add(e.__name__)
        if len(roots) > 1:
            self.hint(self.root_name, "exception-roots",
                      "Public exceptions have %d separate roots (%s). Users cannot catch "
                      "'every error from this library' with one except clause. Give them one "
                      "base class." % (len(roots), ", ".join(sorted(roots))))


def _first_line(doc):
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        if line.strip():
            return line.strip()[:100]
    return ""


def _short_repr(v):
    r = repr(v)
    return r if len(r) <= 60 else r[:57] + "..."


def _ann(a):
    if a is None or a is inspect.Signature.empty:
        return ""
    if isinstance(a, type):
        return a.__name__
    return str(a).replace("typing.", "")


def _ret_ann(f):
    try:
        return inspect.signature(f).return_annotation
    except (TypeError, ValueError):
        return None


def _format_sig(sig, method):
    params = list(sig.parameters.values())
    if method and params and params[0].name in ("self", "cls"):
        params = params[1:]
    text = str(sig.replace(parameters=params))
    text = re.sub(r" at 0x[0-9a-fA-F]+>", ">", text)  # memory addresses change every run
    return text.replace("typing.", "")


def render_py(node, indent=0):
    pad = "  " * indent
    out = []
    k = node["kind"]
    if k == "module":
        out.append("%smodule %s%s" % (pad, node["name"],
                                      ("  # " + node["doc"]) if node.get("doc") else
                                      ("  # " + node["error"] if node.get("error") else "")))
        for m in node.get("members", []):
            out.extend(render_py(m, indent + 1))
    elif k in ("class", "exception"):
        bases = "(%s)" % ", ".join(node["bases"]) if node["bases"] else ""
        init = node.get("init", {}).get("sig", "")
        out.append("%s%s %s%s%s%s" % (pad, k, node["name"], bases, init,
                                      ("  # " + node["doc"]) if node.get("doc") else ""))
        for m in node["members"]:
            out.extend(render_py(m, indent + 1))
    elif k in ("function", "method", "staticmethod", "classmethod"):
        prefix = {"function": "def", "method": "def", "staticmethod": "@staticmethod def",
                  "classmethod": "@classmethod def"}[k]
        if node.get("async_"):
            prefix = "async " + prefix
        out.append("%s%s %s%s" % (pad, prefix, node["name"], node.get("sig", "(...)")))
    elif k == "property":
        out.append("%sproperty %s%s%s" % (pad, node["name"],
                                          (": " + node["type"]) if node["type"] else "",
                                          " (read-only)" if node["readonly"] else ""))
    elif k in ("const", "attr"):
        out.append("%s%s: %s = %s" % (pad, node["name"], node["type"], node["value"]))
    return out


# ================================================================== TypeScript

DECL_RE = re.compile(
    r"^(export\s+)?(declare\s+)?(default\s+)?(abstract\s+)?(async\s+)?"
    r"(function|class|interface|type|const|let|var|enum|namespace|module)\s+"
    r"([\w$.'\"]+)")


class TsSurface:
    def __init__(self, hints):
        self.want_hints = hints
        self.hints = []
        self.stats = Counter()

    def hint(self, where, kind, msg):
        if self.want_hints:
            self.hints.append(OrderedDict(where=where, kind=kind, hint=msg))

    def run(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        src = re.sub(r"/\*\*(.*?)\*/", lambda m: " @@JSDOC@@ ", src, flags=re.S)
        src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
        src = re.sub(r"(?m)//.*$", "", src)
        tree = OrderedDict(kind="module", name=os.path.basename(path),
                           members=self.block(src, os.path.basename(path), top=True))
        errors = [m for m in tree["members"] if m["kind"] == "class" and m["name"].endswith("Error")]
        roots = [m["name"] for m in errors if not any(
            e["name"] in (m.get("heritage") or "") for e in errors if e is not m)]
        if len(roots) > 1:
            self.hint(tree["name"], "exception-roots",
                      "Error classes have %d separate roots (%s). Users cannot catch every SDK "
                      "error with one instanceof check. Give them one base class." %
                      (len(roots), ", ".join(roots)))
        return tree

    def statements(self, src):
        out, cur, depth, i = [], "", 0, 0
        n = len(src)
        while i < n:
            ch = src[i]
            if ch in "'\"`":
                end = i + 1
                while end < n and src[end] != ch:
                    end += 2 if src[end] == "\\" else 1
                cur += src[i:end + 1]
                i = end + 1
                continue
            cur += ch
            if ch in "{([":
                depth += 1
            elif ch in "})]":
                depth -= 1
                if depth == 0 and ch == "}":
                    head = cur.strip().replace("@@JSDOC@@", "").strip()
                    if not re.match(r"^(export\s+)?(declare\s+)?(type|const|let|var|default\s*\{|=)", head):
                        out.append(cur.strip())
                        cur = ""
            elif ch == ";" and depth == 0:
                out.append(cur.strip())
                cur = ""
            elif ch == "\n" and depth == 0 and re.search(r"[\w)\]>]\s*$", cur.strip() or "{") \
                    and re.match(r"\s*(export|declare|@@JSDOC@@|interface|class|function|type|"
                                 r"const|enum|namespace)\b", src[i + 1:i + 40]):
                out.append(cur.strip())
                cur = ""
            i += 1
        if cur.strip():
            out.append(cur.strip())
        return [s for s in out if s and s != ";"]

    def block(self, src, scope, top=False):
        members = []
        for stmt in self.statements(src):
            documented = stmt.lstrip().startswith("@@JSDOC@@")
            while stmt.lstrip().startswith("@@JSDOC@@"):
                stmt = stmt.lstrip()[len("@@JSDOC@@"):]
            stmt = stmt.strip().rstrip(";").strip()
            if not stmt:
                continue
            if re.match(r"^export\s*(\*|\{)", stmt):
                members.append(OrderedDict(kind="re-export", text=re.sub(r"\s+", " ", stmt)))
                continue
            m = DECL_RE.match(stmt)
            if not m:
                continue
            exported = bool(m.group(1)) or not top or stmt.startswith("declare")
            if not exported:
                continue
            kind, name = m.group(6), m.group(7).strip("'\"")
            where = "%s.%s" % (scope, name) if not top else name
            node = OrderedDict(kind=kind, name=name)
            if kind not in ("namespace", "module"):
                self.stats["declarations"] += 1
                if documented:
                    self.stats["documented"] += 1
                else:
                    self.hint(where, "no-docs", "Exported %s has no JSDoc comment." % kind)
            if kind == "function":
                sig = stmt[m.end():].strip()
                node["sig"] = _squash(sig)
                self.stats["functions"] += 1
                self.check_params(where, sig)
            elif kind in ("class", "interface"):
                head, body = _split_body(stmt[m.end():])
                node["heritage"] = _squash(head)
                node["members"] = self.members(body, where, kind)
                self.stats[kind + "es" if kind == "class" else kind + "s"] += 1
            elif kind in ("namespace", "module"):
                _, body = _split_body(stmt[m.end():])
                node["members"] = self.block(body, where)
            elif kind == "enum":
                _, body = _split_body(stmt[m.end():])
                node["values"] = [v.split("=")[0].strip() for v in body.split(",") if v.strip()]
            else:
                node["sig"] = _squash(stmt[m.end():])[:160]
                if kind == "type" and re.search(r"\bany\b", stmt):
                    self.hint(where, "any", "Type uses 'any', which turns off checking for users.")
            members.append(node)
        return members

    def members(self, body, where, kind):
        out = []
        carry = False
        for raw in _split_top(body, ";\n"):
            documented = "@@JSDOC@@" in raw or carry
            line = raw.replace("@@JSDOC@@", "").strip().rstrip(",;").strip()
            carry = documented and not line
            if not line or line.startswith(("private ", "#")) or re.match(r"^(static\s+)?private\b", line):
                continue
            mods = re.match(r"^((public|protected|readonly|static|abstract|declare|override|async)\s+)*", line)
            modtext = mods.group(0) if mods else ""
            rest = line[len(modtext):]
            mwhere = "%s.%s" % (where, re.split(r"[(:<?\s]", rest, 1)[0])
            mm = re.match(r"^(get\s+|set\s+)?(new\s*|[\w$]+|\[[^\]]+\])?(\??)\s*(<[^(]*>)?\s*\(", rest)
            if mm and (mm.group(2) or rest.startswith("(")):
                self.stats["methods"] += 1
                if not documented and kind == "class":
                    self.hint(mwhere, "no-docs", "Public method has no JSDoc comment.")
                out.append(OrderedDict(kind="method", text=_squash(modtext + rest)))
                self.check_params(mwhere, rest[rest.index("("):])
            else:
                self.stats["properties"] += 1
                out.append(OrderedDict(kind="property", text=_squash(modtext + rest)))
                if re.search(r":\s*any\b", rest):
                    self.hint(mwhere, "any", "Property typed 'any'.")
        return out

    def check_params(self, where, sig):
        if not sig.startswith("(") and "(" in sig:
            sig = sig[sig.index("("):]
        if not sig.startswith("("):
            return
        depth, end = 0, 0
        for i, ch in enumerate(sig):
            depth += ch in "({[<"
            depth -= ch in ")}]>" and not (ch == ">" and i and sig[i - 1] == "=")
            if depth == 0:
                end = i
                break
        params = [p.strip() for p in _split_top(sig[1:end], ",") if p.strip()]
        ret = sig[end + 1:].strip().lstrip(":").strip()
        if len(params) > 4:
            self.hint(where, "long-positional", "%d positional parameters. Group the optional "
                      "ones into an options object." % len(params))
        seen_optional = False
        for p in params:
            name = re.split(r"[?:=]", p, 1)[0].strip()
            optional = "?" in p.split(":")[0] or "=" in p
            if optional:
                seen_optional = True
            elif seen_optional and not p.startswith("..."):
                self.hint(where, "optional-before-required",
                          "Required parameter '%s' comes after an optional one." % name)
            if re.search(r":\s*boolean\s*$", p):
                self.hint(where, "bool-positional",
                          "'%s: boolean' is a positional flag, so call sites read as f(x, true). "
                          "Use an options object." % name)
            if re.search(r":\s*any\b", p):
                self.hint(where, "any", "Parameter '%s' is typed 'any'." % name)
            if re.match(r"timeout", name, re.I) is None and "callback" in name.lower():
                self.hint(where, "callback", "Callback parameter '%s'. Modern TS SDKs return "
                          "Promises." % name)
        if re.match(r"any\b", ret):
            self.hint(where, "any", "Returns 'any'.")


def _squash(s):
    return re.sub(r"\s+", " ", s).strip()


def _split_body(s):
    depth = 0
    for i, ch in enumerate(s):
        if ch in "(<[":
            depth += 1
        elif ch in ")>]" and not (ch == ">" and i and s[i - 1] == "="):
            depth -= 1
        elif ch == "{" and depth == 0:
            close = _match_brace(s, i)
            return s[:i], s[i + 1:close]
    return s, ""


def _match_brace(s, start):
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(s)


def _split_top(s, seps):
    out, cur, depth = [], "", 0
    for i, ch in enumerate(s):
        if ch in "({[<":
            depth += 1
        elif ch in ")}]>" and not (ch == ">" and i and s[i - 1] == "="):
            depth -= 1
        if ch in seps and depth == 0:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out.append(cur)
    return out


def render_ts(node, indent=0):
    pad = "  " * indent
    out = []
    k = node["kind"]
    if k == "module":
        out.append("%smodule %s" % (pad, node["name"]))
        for m in node["members"]:
            out.extend(render_ts(m, indent + 1))
    elif k in ("class", "interface"):
        out.append("%s%s %s%s" % (pad, k, node["name"],
                                  (" " + node["heritage"]) if node.get("heritage") else ""))
        for m in node["members"]:
            out.append("%s  %s %s" % (pad, "fn  " if m["kind"] == "method" else "prop", m["text"]))
    elif k in ("namespace", "module"):
        out.append("%snamespace %s" % (pad, node["name"]))
        for m in node["members"]:
            out.extend(render_ts(m, indent + 1))
    elif k == "enum":
        out.append("%senum %s { %s }" % (pad, node["name"], ", ".join(node["values"])))
    elif k == "re-export":
        out.append("%s%s" % (pad, node["text"]))
    else:
        out.append("%s%s %s %s" % (pad, k, node["name"], node.get("sig", "")))
    return out


# ======================================================================== main


def summary_lines(stats, kind):
    if kind == "py":
        slots = stats["annotation_slots"] or 1
        callables = stats["functions"] + stats["methods"] + stats["classes"]
        return ["%d modules, %d classes (%d exceptions), %d functions, %d methods, %d constants."
                % (stats["modules"], stats["classes"], stats["exceptions"], stats["functions"],
                   stats["methods"], stats["constants"]),
                "Docstrings on %d%% of public classes and callables. Type annotations on %d%% "
                "of parameter and return slots." %
                (100 - (100 * stats["undocumented"] // (callables or 1)),
                 100 * stats["annotated"] // slots)]
    total = stats["declarations"] or 1
    return ["%d exported declarations (%d functions, %d classes, %d interfaces), %d members "
            "(%d methods, %d properties)." %
            (stats["declarations"], stats["functions"], stats["classes"], stats["interfaces"],
             stats["methods"] + stats["properties"], stats["methods"], stats["properties"]),
            "JSDoc on %d%% of exported declarations." % (100 * stats["documented"] // total)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", help="Python module name (e.g. 'json') or a .d.ts file")
    ap.add_argument("--path", help="directory to add to sys.path before importing")
    ap.add_argument("--depth", type=int, default=2, help="submodule depth for packages")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--no-hints", action="store_true")
    ap.add_argument("--private", action="store_true", help="include _private names")
    args = ap.parse_args()

    if args.target.endswith(".ts"):
        surf = TsSurface(not args.no_hints)
        tree = surf.run(args.target)
        lines = render_ts(tree)
        kind = "ts"
    else:
        if args.path:
            sys.path.insert(0, os.path.abspath(args.path))
        surf = PySurface(args.target, args.depth, args.private, not args.no_hints)
        tree = surf.run()
        lines = render_py(tree)
        kind = "py"

    if args.format == "json":
        print(json.dumps({"tree": tree, "stats": dict(surf.stats), "hints": surf.hints},
                         indent=2, default=str))
        return
    print("\n".join(lines))
    print("")
    print("Summary")
    for line in summary_lines(surf.stats, kind):
        print("  " + line)
    if surf.hints:
        by_kind = Counter(h["kind"] for h in surf.hints)
        print("")
        print("Hints (%s)" % ", ".join("%d %s" % (n, k) for k, n in by_kind.most_common()))
        quiet = ("no-docstring", "no-docs")
        for h in surf.hints:
            if h["kind"] not in quiet:
                print("  [%s] %s: %s" % (h["kind"], h["where"], h["hint"]))
        undocumented = [h["where"] for h in surf.hints if h["kind"] in quiet]
        if undocumented:
            print("  [docs] %d public names have no doc comment, for example: %s%s" % (
                len(undocumented), ", ".join(undocumented[:8]),
                " (use --format json for the full list)" if len(undocumented) > 8 else ""))


if __name__ == "__main__":
    main()
