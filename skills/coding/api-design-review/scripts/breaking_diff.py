#!/usr/bin/env python3
"""Diff two OpenAPI 3.x documents and classify every change.

Classes:
  BREAKING  an existing, correctly written client can fail or misbehave.
  RISKY     correct clients keep working, but clients that are strict
            (exhaustive enum switches, strict deserializers, status code
            checks) or generated SDKs may break. Decide case by case.
  SAFE      additive or loosening change that tolerant clients absorb.

The rules follow the request/response asymmetry: a request may get looser
and a response may get richer, never the reverse. See reference.md for the
reasoning and precedents (Google AIP-180, Stripe API upgrades, Zalando
compatibility rules, Microsoft/Azure breaking change policy).

Usage:
  python3 breaking_diff.py old.json new.yaml [--format text|md|json] [--no-fail]

Exit code 1 when there is at least one BREAKING change (use in CI), unless
--no-fail is given.
"""

import argparse
import json
import os
import re
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _spec import load_spec, deref, ref_name, iter_operations, op_parameters, json_content  # noqa: E402

BREAKING, RISKY, SAFE = "BREAKING", "RISKY", "SAFE"
CONSTRAINT_UPPER = ("maxLength", "maximum", "maxItems", "maxProperties", "exclusiveMaximum")
CONSTRAINT_LOWER = ("minLength", "minimum", "minItems", "minProperties", "exclusiveMinimum")


def norm_path(path):
    return re.sub(r"\{[^}]+\}", "{}", path)


def stype(schema):
    t = schema.get("type")
    nullable = bool(schema.get("nullable"))
    if isinstance(t, list):
        nullable = nullable or "null" in t
        rest = [x for x in t if x != "null"]
        t = rest[0] if len(rest) == 1 else (",".join(sorted(rest)) or None)
    if t is None and "properties" in schema:
        t = "object"
    return t, nullable


class Differ:
    def __init__(self, old, new):
        self.old, self.new = old, new
        self.changes = []
        self.usage = OrderedDict()  # "schema Name (direction)" -> [where it is used]

    def add(self, cls, where, what, why=""):
        self.changes.append(OrderedDict(cls=cls, where=where, change=what, why=why))

    # ------------------------------------------------------------ schemas

    def schema(self, o, n, direction, where, seen=None, depth=0):
        """direction: 'request' (client sends) or 'response' (client reads)."""
        seen = seen or set()
        if depth > 25 or o is None or n is None:
            return
        key = (ref_name(o), ref_name(n))
        if key != (None, None):
            if key in seen:
                return
            seen = seen | {key}
        if key[0] and key[0] == key[1]:
            label = "schema %s (%s)" % (key[0], direction)
            first = label not in self.usage
            self.usage.setdefault(label, [])
            if where not in self.usage[label]:
                self.usage[label].append(where)
            if not first:
                return
            where = label
        o, n = deref(self.old, o), deref(self.new, n)
        if not isinstance(o, dict) or not isinstance(n, dict):
            return
        req = direction == "request"
        ot, onull = stype(o)
        nt, nnull = stype(n)

        if ot and nt and ot != nt:
            widening = (ot, nt) == ("integer", "number")
            if req and widening:
                self.add(SAFE, where, "type widened integer -> number")
            elif not req and (ot, nt) == ("number", "integer"):
                self.add(SAFE, where, "type narrowed number -> integer")
            else:
                self.add(BREAKING, where, "type changed %s -> %s" % (ot, nt),
                         "Clients serialize or parse this field with the old type.")
            return
        if o.get("format") != n.get("format") and (o.get("format") or n.get("format")):
            self.add(BREAKING if (o.get("format") and n.get("format")) or req else RISKY, where,
                     "format changed %s -> %s" % (o.get("format"), n.get("format")),
                     "Typed SDKs map formats to types (date-time to a datetime, int64 to a long).")
        if onull != nnull:
            if not req and nnull:
                self.add(BREAKING, where, "response field became nullable",
                         "Clients that dereference it without a null check will crash.")
            elif req and not nnull:
                self.add(BREAKING, where, "request field no longer accepts null")
            else:
                self.add(SAFE, where, "nullability loosened" if req else "response field no longer null")

        # enums
        oe, ne = o.get("enum"), n.get("enum")
        if oe is not None and ne is not None:
            added = [v for v in ne if v not in oe]
            removed = [v for v in oe if v not in ne]
            if added:
                if req:
                    self.add(SAFE, where, "enum values added: %s" % added)
                else:
                    self.add(RISKY, where, "enum values added to a response: %s" % added,
                             "Additive by most policies (Stripe, Google AIP-180), but clients "
                             "with exhaustive switches or strict enum types will fail. Say so in "
                             "the changelog and make sure SDKs have an 'unknown' fallback.")
            if removed:
                if req:
                    self.add(BREAKING, where, "enum values removed from a request: %s" % removed,
                             "Clients still send them and will now get errors.")
                else:
                    self.add(SAFE, where, "enum values no longer returned: %s" % removed)
        elif oe is None and ne is not None:
            self.add(BREAKING if req else SAFE, where, "enum restriction added")
        elif oe is not None and ne is None:
            self.add(SAFE if req else RISKY, where, "enum restriction removed")

        # numeric and length constraints
        for k in CONSTRAINT_UPPER + CONSTRAINT_LOWER + ("pattern",):
            ov, nv = o.get(k), n.get(k)
            if ov == nv or isinstance(ov, bool) or isinstance(nv, bool):
                continue
            if k == "pattern":
                tighter = nv is not None
            elif k in CONSTRAINT_UPPER:
                tighter = nv is not None and (ov is None or nv < ov)
            else:
                tighter = nv is not None and (ov is None or nv > ov)
            if req:
                self.add(BREAKING if tighter else SAFE, where,
                         "%s changed %s -> %s" % (k, ov, nv),
                         "Values that were valid before are now rejected." if tighter else "")
            else:
                self.add(SAFE if tighter else RISKY, where, "%s changed %s -> %s" % (k, ov, nv),
                         "" if tighter else "Clients may have sized buffers, columns or UI "
                         "for the old limit.")

        # objects
        op_, np_ = o.get("properties") or {}, n.get("properties") or {}
        oreq, nreq = set(o.get("required") or []), set(n.get("required") or [])
        removed = [k for k in op_ if k not in np_]
        added = [k for k in np_ if k not in op_]
        for k in removed:
            hint = self.rename_hint(op_[k], np_, added, k)
            if req:
                self.add(BREAKING, "%s.%s" % (where, k), "request field removed" + hint,
                         "Clients still send it. It will be rejected or, worse, silently "
                         "ignored.")
            else:
                self.add(BREAKING, "%s.%s" % (where, k), "response field removed" + hint,
                         "Clients that read it get nothing or crash.")
        for k in added:
            if req and k in nreq:
                self.add(BREAKING, "%s.%s" % (where, k), "new required request field",
                         "Every existing call is missing it.")
            else:
                self.add(SAFE, "%s.%s" % (where, k),
                         "new optional request field" if req else "new response field")
        for k in op_:
            if k not in np_:
                continue
            fw = "%s.%s" % (where, k)
            if req and k in nreq and k not in oreq:
                self.add(BREAKING, fw, "request field became required")
            elif req and k in oreq and k not in nreq:
                self.add(SAFE, fw, "request field became optional")
            elif not req and k in oreq and k not in nreq:
                self.add(BREAKING, fw, "response field no longer guaranteed (not required)",
                         "Clients assume it is always present.")
            self.schema(op_[k], np_[k], direction, fw, seen, depth + 1)

        oa, na = o.get("additionalProperties"), n.get("additionalProperties")
        if req and oa is not False and na is False:
            self.add(BREAKING, where, "request object now rejects unknown fields")
        if isinstance(o.get("items"), dict) and isinstance(n.get("items"), dict):
            self.schema(o["items"], n["items"], direction, where + "[]", seen, depth + 1)

        for comb in ("oneOf", "anyOf"):
            ov, nv = o.get(comb) or [], n.get(comb) or []
            if len(nv) > len(ov):
                self.add(SAFE if req else RISKY, where,
                         "%s gained %d variant(s)" % (comb, len(nv) - len(ov)),
                         "" if req else "Clients that switch on the variant may not handle it.")
            elif len(nv) < len(ov):
                self.add(BREAKING if req else SAFE, where,
                         "%s lost %d variant(s)" % (comb, len(ov) - len(nv)))
        for i, (a, b) in enumerate(zip(o.get("allOf") or [], n.get("allOf") or [])):
            self.schema(a, b, direction, where, seen, depth + 1)

    def rename_hint(self, old_prop, new_props, added, old_name=None):
        ot = stype(deref(self.old, old_prop))[0]
        same = [a for a in added if stype(deref(self.new, new_props[a]))[0] == ot]
        if len(same) != 1:
            same = [a for a in added if old_name and (a.startswith(old_name) or
                                                      old_name.startswith(a) or
                                                      a.replace("_", "").lower() ==
                                                      old_name.replace("_", "").lower())]
        if len(same) == 1:
            return " (looks renamed to '%s'; keep the old name as a deprecated alias)" % same[0]
        return ""

    # ------------------------------------------------------------ operations

    def ops(self, spec):
        out = OrderedDict()
        for path, method, op, shared in iter_operations(spec):
            out[(method, norm_path(path))] = (path, op, shared)
        return out

    def run(self):
        self.global_changes()
        old_ops, new_ops = self.ops(self.old), self.ops(self.new)
        for key, (path, op, shared) in old_ops.items():
            where = "%s %s" % (key[0].upper(), path)
            if key not in new_ops:
                self.add(BREAKING, where, "operation removed",
                         "Deprecate first, announce a sunset date, then remove in a new "
                         "major version.")
                continue
            npath, nop, nshared = new_ops[key]
            if npath != path:
                self.add(SAFE, where, "path parameter renamed: %s -> %s" % (path, npath),
                         "Wire format unchanged; generated SDK argument names may change.")
            self.operation(where, op, shared, nop, nshared)
        for key, (path, op, shared) in new_ops.items():
            if key not in old_ops:
                self.add(SAFE, "%s %s" % (key[0].upper(), path), "operation added")
        self.webhooks()
        return self.changes

    def global_changes(self):
        os_ = [s.get("url") for s in self.old.get("servers") or [] if isinstance(s, dict)]
        ns = [s.get("url") for s in self.new.get("servers") or [] if isinstance(s, dict)]
        for url in os_:
            if url not in ns:
                self.add(BREAKING, "servers", "server URL removed: %s" % url,
                         "Clients have the base URL hard-coded.")
        oschemes = (self.old.get("components") or {}).get("securitySchemes") or {}
        nschemes = (self.new.get("components") or {}).get("securitySchemes") or {}
        for name, s in oschemes.items():
            if name not in nschemes:
                self.add(BREAKING, "securitySchemes." + name, "security scheme removed")
            elif deref(self.old, s).get("type") != deref(self.new, nschemes[name]).get("type") or \
                    deref(self.old, s).get("in") != deref(self.new, nschemes[name]).get("in") or \
                    deref(self.old, s).get("name") != deref(self.new, nschemes[name]).get("name"):
                self.add(BREAKING, "securitySchemes." + name, "security scheme changed how "
                         "credentials are sent")
        ov, nv = str((self.old.get("info") or {}).get("version", "")), \
            str((self.new.get("info") or {}).get("version", ""))
        self.versions = (ov, nv)

    def operation(self, where, op, shared, nop, nshared):
        if op.get("operationId") and op.get("operationId") != nop.get("operationId"):
            self.add(BREAKING, where, "operationId changed %s -> %s" %
                     (op.get("operationId"), nop.get("operationId")),
                     "Wire format is unchanged, but generated SDK method names change.")
        if nop.get("deprecated") and not op.get("deprecated"):
            self.add(SAFE, where, "operation deprecated",
                     "Good: also send Deprecation and Sunset headers (RFC 9745, RFC 8594).")

        # security
        osec = op.get("security", self.old.get("security")) or []
        nsec = nop.get("security", self.new.get("security")) or []
        if self.sec_set(osec) != self.sec_set(nsec):
            if not osec or osec == [{}]:
                self.add(BREAKING, where, "authentication now required",
                         "Anonymous callers will get 401.")
            elif not nsec or nsec == [{}]:
                self.add(SAFE, where, "authentication no longer required")
            else:
                self.add(BREAKING, where, "security requirements changed: %s -> %s" %
                         (sorted(self.sec_set(osec)), sorted(self.sec_set(nsec))),
                         "Existing tokens may lack the new scheme or scope.")

        # parameters
        oparams = {(p.get("in"), p["name"]): p for p in op_parameters(self.old, op, shared)}
        nparams = {(p.get("in"), p["name"]): p for p in op_parameters(self.new, nop, nshared)}
        for k, p in oparams.items():
            pw = "%s %s param '%s'" % (where, k[0], k[1])
            if k not in nparams:
                if k[0] == "path":
                    continue  # covered by path rename
                moved = [nk for nk in nparams if nk[1] == k[1]]
                if moved:
                    self.add(BREAKING, pw, "parameter moved from %s to %s" % (k[0], moved[0][0]))
                else:
                    self.add(BREAKING if p.get("required") else RISKY, pw, "parameter removed",
                             "Clients still send it and it will be ignored or rejected. If it "
                             "changed behaviour (a filter, a flag), ignoring it silently is "
                             "worse than an error.")
                continue
            np_ = nparams[k]
            if np_.get("required") and not p.get("required"):
                self.add(BREAKING, pw, "parameter became required")
            elif p.get("required") and not np_.get("required"):
                self.add(SAFE, pw, "parameter became optional")
            self.schema(p.get("schema"), np_.get("schema"), "request", pw)
        for k, p in nparams.items():
            if k not in oparams and not any(ok[1] == k[1] for ok in oparams):
                pw = "%s %s param '%s'" % (where, k[0], k[1])
                if p.get("required") and k[0] != "path":
                    self.add(BREAKING, pw, "new required parameter", "Existing calls lack it.")
                elif k[0] != "path":
                    self.add(SAFE, pw, "new optional parameter")

        # request body
        ob = deref(self.old, op.get("requestBody")) or {}
        nb = deref(self.new, nop.get("requestBody")) or {}
        if nb and not ob:
            self.add(BREAKING if nb.get("required") else SAFE, where + " body",
                     "request body added" + (" and required" if nb.get("required") else ""))
        elif ob and not nb:
            self.add(BREAKING, where + " body", "request body removed")
        elif ob and nb:
            if nb.get("required") and not ob.get("required"):
                self.add(BREAKING, where + " body", "request body became required")
            self.content(ob.get("content") or {}, nb.get("content") or {}, "request",
                         where + " body")

        # responses
        oresp, nresp = op.get("responses") or {}, nop.get("responses") or {}
        oc = {str(c) for c in oresp}
        nc = {str(c) for c in nresp}
        osucc = {c for c in oc if c.startswith("2")}
        nsucc = {c for c in nc if c.startswith("2")}
        for c in sorted(osucc - nsucc):
            self.add(BREAKING, where, "success status %s no longer returned (now %s)" %
                     (c, ", ".join(sorted(nsucc)) or "none"),
                     "Clients that check the exact status code will treat success as failure.")
        for c in sorted(nsucc - osucc):
            if osucc and osucc <= nsucc:
                self.add(RISKY, where, "new success status %s" % c)
        for c in sorted((nc - oc) - nsucc):
            self.add(RISKY, where, "new error status %s documented" % c,
                     "Safe if this only documents existing behaviour. If the server now "
                     "fails in a new way, clients need to handle it.")
        for c in sorted((oc - nc) - osucc):
            self.add(SAFE, where, "error status %s no longer documented" % c)
        for c in sorted(oc & nc):
            ro = deref(self.old, oresp.get(c, oresp.get(int(c)) if c.isdigit() else None)) or {}
            rn = deref(self.new, nresp.get(c, nresp.get(int(c)) if c.isdigit() else None)) or {}
            rw = "%s -> %s" % (where, c)
            self.content(ro.get("content") or {}, rn.get("content") or {}, "response", rw)
            oh = {h.lower() for h in ro.get("headers") or {}}
            nh = {h.lower() for h in rn.get("headers") or {}}
            for h in sorted(oh - nh):
                self.add(BREAKING, rw, "response header '%s' removed" % h)
            for h in sorted(nh - oh):
                self.add(SAFE, rw, "response header '%s' added" % h)

    def sec_set(self, sec):
        out = set()
        for req in sec or []:
            for name, scopes in (req or {}).items():
                out.add((name, tuple(sorted(scopes or []))))
        return frozenset(out)

    def content(self, oc, nc, direction, where):
        for mt in oc:
            if mt not in nc:
                self.add(BREAKING, where, "media type '%s' removed" % mt)
        for mt in nc:
            if mt not in oc:
                self.add(SAFE, where, "media type '%s' added" % mt)
        for mt in oc:
            if mt in nc:
                self.schema((oc[mt] or {}).get("schema"), (nc[mt] or {}).get("schema"),
                            direction, where)

    def webhooks(self):
        oh, nh = self.old.get("webhooks") or {}, self.new.get("webhooks") or {}
        for name, item in oh.items():
            where = "webhook " + name
            if name not in nh:
                self.add(BREAKING, where, "webhook event removed")
                continue
            for method, op in (item or {}).items():
                nop = (nh[name] or {}).get(method)
                if not isinstance(op, dict) or not isinstance(nop, dict):
                    continue
                ob = deref(self.old, op.get("requestBody")) or {}
                nb = deref(self.new, nop.get("requestBody")) or {}
                # the receiver reads the payload, so it follows response rules
                self.content(ob.get("content") or {}, nb.get("content") or {}, "response", where)
        for name in nh:
            if name not in oh:
                self.add(RISKY, "webhook " + name, "new webhook event type",
                         "Receivers subscribed to all events must ignore unknown types; "
                         "only safe if your docs promised that.")


def semver_advice(changes, versions):
    has_b = any(c["cls"] == BREAKING for c in changes)
    has_new = any(c["cls"] in (SAFE, RISKY) for c in changes)
    need = "major" if has_b else ("minor" if has_new else "patch")
    ov, nv = versions
    note = "Changes need a %s version bump." % need
    try:
        om, nm = int(ov.split(".")[0]), int(nv.split(".")[0])
        if has_b and nm <= om:
            note += " info.version went %s -> %s, which does not signal a breaking change." % (ov, nv)
    except ValueError:
        pass
    return note


def render(changes, advice, fmt, a, b, usage):
    touched = OrderedDict()
    for label, wheres in usage.items():
        if any(c["where"].startswith(label) for c in changes):
            touched[label] = wheres
    if fmt == "json":
        return json.dumps({"old": a, "new": b, "advice": advice, "changes": changes,
                           "shared_schema_usage": touched}, indent=2)
    counts = {k: sum(1 for c in changes if c["cls"] == k) for k in (BREAKING, RISKY, SAFE)}
    lines = []
    head = "%d breaking, %d risky, %d safe. %s" % (counts[BREAKING], counts[RISKY], counts[SAFE], advice)
    if fmt == "md":
        lines.append("# API diff: %s -> %s\n\n%s\n" % (os.path.basename(a), os.path.basename(b), head))
        for cls in (BREAKING, RISKY, SAFE):
            items = [c for c in changes if c["cls"] == cls]
            if not items:
                continue
            lines.append("## %s (%d)\n" % (cls.title(), len(items)))
            for c in items:
                lines.append("- `%s`: %s%s" % (c["where"], c["change"],
                                               (". " + c["why"]) if c["why"] else ""))
            lines.append("")
        if touched:
            lines.append("## Where changed shared schemas are used\n")
            for label, wheres in touched.items():
                lines.append("- `%s`: %s" % (label, ", ".join("`%s`" % w for w in wheres)))
        return "\n".join(lines) + "\n"
    lines.append("API diff: %s -> %s" % (a, b))
    lines.append(head)
    for cls in (BREAKING, RISKY, SAFE):
        items = [c for c in changes if c["cls"] == cls]
        if not items:
            continue
        lines.append("")
        lines.append("%s (%d)" % (cls, len(items)))
        for c in items:
            lines.append("  %s: %s" % (c["where"], c["change"]))
            if c["why"]:
                lines.append("      " + c["why"])
    if touched:
        lines.append("")
        lines.append("Where changed shared schemas are used")
        for label, wheres in touched.items():
            lines.append("  %s: %s" % (label, "; ".join(wheres)))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--format", choices=("text", "md", "json"), default="text")
    ap.add_argument("--no-fail", action="store_true", help="always exit 0")
    args = ap.parse_args()
    old, new = load_spec(args.old), load_spec(args.new)
    d = Differ(old, new)
    changes = d.run()
    # de-duplicate (shared schemas are reached from several operations)
    seen, unique = set(), []
    for c in changes:
        k = (c["cls"], c["where"], c["change"])
        if k not in seen:
            seen.add(k)
            unique.append(c)
    advice = semver_advice(unique, d.versions)
    sys.stdout.write(render(unique, advice, args.format, args.old, args.new, d.usage))
    if not args.no_fail and any(c["cls"] == BREAKING for c in unique):
        sys.exit(1)


if __name__ == "__main__":
    main()
