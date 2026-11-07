"""Shared helpers: load an OpenAPI document (JSON or YAML) and resolve local $refs.

Standard library only. If PyYAML is installed it is used for YAML files.
Otherwise a small fallback parser handles the block-style YAML subset that
OpenAPI files normally use (maps, lists, quoted and plain scalars, flow lists
and maps on one line, | and > block strings, comments). Anchors, aliases,
tags and multi-document files are not supported by the fallback; convert
those files to JSON first.
"""

import json
import re
import sys

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


# ---------------------------------------------------------------- loading

def load_spec(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return json.loads(text)
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        pass
    try:
        return MiniYaml(text).parse()
    except Exception as exc:  # noqa: BLE001
        sys.exit(
            "Could not parse %s as YAML without PyYAML (%s).\n"
            "Install PyYAML (pip install pyyaml) or convert the file to JSON." % (path, exc)
        )


class MiniYaml:
    """Minimal block-YAML parser, enough for typical OpenAPI files."""

    def __init__(self, text):
        self.lines = []
        raw = text.splitlines()
        i = 0
        while i < len(raw):
            line = raw[i].rstrip()
            i += 1
            if not line.strip() or line.strip().startswith("#") or line.strip() in ("---", "..."):
                continue
            indent = len(line) - len(line.lstrip(" "))
            content = _strip_comment(line.strip())
            self.lines.append([indent, content, i])
            # keep raw lines of block scalars untouched
            if re.search(r":\s*[|>][-+]?$", content) or re.match(r"^-\s*[|>][-+]?$", content):
                block = []
                while i < len(raw) and (not raw[i].strip() or
                                        len(raw[i]) - len(raw[i].lstrip(" ")) > indent):
                    block.append(raw[i])
                    i += 1
                self.lines[-1].append(block)

    def parse(self):
        if not self.lines:
            return None
        value, idx = self._block(0, self.lines[0][0])
        if idx != len(self.lines):
            raise ValueError("unexpected indentation at line %d" % self.lines[idx][2])
        return value

    def _block(self, idx, indent):
        if self.lines[idx][1].startswith("- ") or self.lines[idx][1] == "-":
            return self._list(idx, indent)
        return self._map(idx, indent)

    def _list(self, idx, indent):
        out = []
        while idx < len(self.lines) and self.lines[idx][0] == indent and (
                self.lines[idx][1].startswith("- ") or self.lines[idx][1] == "-"):
            entry = self.lines[idx]
            rest = entry[1][1:].strip()
            if not rest:
                idx += 1
                if idx < len(self.lines) and self.lines[idx][0] > indent:
                    val, idx = self._block(idx, self.lines[idx][0])
                else:
                    val = None
                out.append(val)
            elif _split_key(rest) is not None and not rest.startswith(("'", '"', "[", "{")):
                # "- key: value" starts a map indented at the key position
                offset = len(entry[1]) - len(rest)
                entry[0] = indent + offset
                entry[1] = rest
                val, idx = self._map(idx, indent + offset)
                out.append(val)
            elif len(entry) > 3:
                out.append(_block_scalar(rest, entry[3]))
                idx += 1
            else:
                out.append(_scalar(rest))
                idx += 1
        return out, idx

    def _map(self, idx, indent):
        out = {}
        while idx < len(self.lines) and self.lines[idx][0] == indent:
            entry = self.lines[idx]
            if entry[1].startswith("- "):
                break
            kv = _split_key(entry[1])
            if kv is None:
                raise ValueError("expected 'key: value' at line %d" % entry[2])
            key, rest = kv
            idx += 1
            if len(entry) > 3:
                out[key] = _block_scalar(rest, entry[3])
            elif rest:
                out[key] = _scalar(rest)
            elif idx < len(self.lines) and self.lines[idx][0] > indent:
                out[key], idx = self._block(idx, self.lines[idx][0])
            elif idx < len(self.lines) and self.lines[idx][0] == indent and \
                    self.lines[idx][1].startswith("-"):
                out[key], idx = self._list(idx, indent)
            else:
                out[key] = None
        return out, idx


def _strip_comment(s):
    quote = None
    for i, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s


def _split_key(s):
    if s.startswith(("'", '"')):
        q = s[0]
        end = s.find(q, 1)
        if end == -1:
            return None
        key, after = s[1:end], s[end + 1:].lstrip()
        if not after.startswith(":"):
            return None
        return key, after[1:].strip()
    depth = 0
    for i, ch in enumerate(s):
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (i + 1 == len(s) or s[i + 1] in " \t"):
            return s[:i].strip(), s[i + 1:].strip()
    return None


def _block_scalar(indicator, raw_lines):
    lines = [l for l in raw_lines]
    nonblank = [l for l in lines if l.strip()]
    if not nonblank:
        return ""
    cut = min(len(l) - len(l.lstrip(" ")) for l in nonblank)
    body = [l[cut:] if l.strip() else "" for l in lines]
    while body and not body[-1]:
        body.pop()
    if indicator.startswith(">"):
        text = " ".join(x for x in body if x)
    else:
        text = "\n".join(body)
    return text if indicator.endswith("-") else text + "\n"


def _scalar(s):
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(x) for x in _split_flow(inner)] if inner else []
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        out = {}
        for part in _split_flow(inner) if inner else []:
            kv = _split_key(part)
            if kv is None:
                raise ValueError("bad flow map entry: %r" % part)
            out[kv[0]] = _scalar(kv[1]) if kv[1] else None
        return out
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return json.loads(s)
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True if low == "true" else s
    if low == "false":
        return False
    if low in ("null", "~", ""):
        return None
    if re.match(r"^[-+]?\d+$", s):
        return int(s)
    if re.match(r"^[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$", s):
        return float(s)
    return s


def _split_flow(s):
    parts, depth, quote, cur = [], 0, None, ""
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


# ---------------------------------------------------------------- $ref handling

def resolve_pointer(doc, ref):
    if not ref.startswith("#/"):
        return None  # external refs are not followed
    node = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def deref(doc, node, seen=None):
    """Follow a single $ref chain (not recursive into children)."""
    seen = seen or set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen:
            return {}
        seen.add(ref)
        target = resolve_pointer(doc, ref)
        if target is None:
            return node
        node = target
    return node


def ref_name(node):
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return node["$ref"].rsplit("/", 1)[-1]
    return None


def iter_operations(spec):
    """Yield (path, method, operation, path_item) with path-level params merged."""
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        item = deref(spec, item)
        shared = item.get("parameters") or []
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            yield path, method, op, shared


def op_parameters(spec, op, shared):
    """Return resolved parameters, operation-level overriding path-level."""
    merged = {}
    for p in list(shared) + list(op.get("parameters") or []):
        p = deref(spec, p)
        if isinstance(p, dict) and "name" in p:
            merged[(p.get("in"), p["name"].lower() if p.get("in") == "header" else p["name"])] = p
    return list(merged.values())


def schema_properties(spec, schema, depth=0, seen=None):
    """Flatten object properties (follows $ref, allOf). Returns dict name -> schema."""
    seen = seen if seen is not None else set()
    out = {}
    if depth > 12 or not isinstance(schema, dict):
        return out
    name = ref_name(schema)
    if name:
        if name in seen:
            return out
        seen = seen | {name}
    schema = deref(spec, schema)
    for sub in schema.get("allOf") or []:
        out.update(schema_properties(spec, sub, depth + 1, seen))
    for k, v in (schema.get("properties") or {}).items():
        out[k] = v
    return out


def json_content(spec, content):
    """Pick the JSON-ish media type from a content map."""
    if not isinstance(content, dict):
        return None, None
    for mt, body in content.items():
        if "json" in mt:
            return mt, body
    for mt, body in content.items():
        return mt, body
    return None, None
