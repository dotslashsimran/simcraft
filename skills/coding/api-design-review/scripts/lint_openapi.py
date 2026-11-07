#!/usr/bin/env python3
"""Lint an OpenAPI 3.x document for API design problems.

This is a design linter, not a schema validator. It looks for the problems
that cost API consumers time: inconsistent naming, verbs in paths, singular
collections, errors with no machine-readable shape, list endpoints with no
pagination, mixed ID and time formats, money as floats, creating POSTs with
no idempotency key, missing operationIds, unsigned webhooks.

Usage:
  python3 lint_openapi.py openapi.json [--format text|md|json] [--fail-on high|medium|low]

Standard library only. YAML is read with PyYAML when installed, otherwise
with a small built-in parser (see _spec.py).
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict, OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _spec import (load_spec, deref, ref_name, iter_operations, op_parameters,  # noqa: E402
                   schema_properties, json_content)

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

CRUD_VERBS = {
    "get", "list", "create", "add", "update", "delete", "remove", "fetch", "set",
    "retrieve", "make", "new", "edit", "modify", "save", "put", "post", "patch", "find",
    "do", "read", "insert", "upsert", "destroy",
}
UNCOUNTABLE = {
    "data", "info", "metadata", "me", "search", "health", "healthz", "settings", "config",
    "configuration", "status", "auth", "oauth", "login", "logout", "billing", "news",
    "analytics", "feedback", "media", "inventory", "equipment", "staff", "series", "self",
    "batch", "bulk", "webhooks", "openapi", "docs", "admin", "usage", "software",
}
PAGE_SIZE_PARAMS = {"limit", "page_size", "pagesize", "per_page", "perpage", "max_results",
                    "maxresults", "$top", "top", "size", "count", "first", "last"}
CURSOR_PARAMS = {"cursor", "page_token", "pagetoken", "starting_after", "startingafter",
                 "ending_before", "endingbefore", "after", "before", "continuation_token",
                 "continuationtoken", "next", "$skiptoken", "skiptoken", "marker"}
OFFSET_PARAMS = {"offset", "page", "skip", "$skip", "page_number", "pagenumber", "start"}
NEXT_FIELDS = {"next_cursor", "nextcursor", "next_page_token", "nextpagetoken", "nextlink",
               "next_link", "has_more", "hasmore", "next", "cursor", "links", "_links",
               "next_page", "nextpage", "continuation_token", "continuationtoken"}
LIST_WRAPPERS = {"data", "items", "results", "value", "records", "entries", "list", "content"}
MONEY_NAMES = re.compile(r"(^|_|[a-z])(amount|price|total|cost|balance|fee|subtotal|tax|"
                         r"discount|refund(ed)?_?amount)$", re.I)
TIME_NAMES = re.compile(r"(_at$|At$|_time$|Time$|_date$|Date$|^date$|^time$|timestamp|"
                        r"^created$|^updated$|^modified$|^expires$|_on$|^when$)")
ID_NAMES = re.compile(r"(^id$|_id$|[a-z]Id$|^ID$)")
IDEMPOTENCY_HEADERS = {"idempotency-key", "repeatability-request-id", "x-idempotency-key",
                       "x-request-id", "request-id"}
IDEMPOTENCY_BODY = {"idempotency_key", "idempotencykey", "request_id", "requestid",
                    "client_token", "clienttoken"}


def word_case(name):
    if not name or not re.search(r"[A-Za-z]", name):
        return None
    if "-" in name and name == name.lower():
        return "kebab-case"
    if "_" in name.strip("_") and name == name.lower():
        return "snake_case"
    if "_" in name.strip("_") and name == name.upper():
        return "UPPER_SNAKE"
    if re.match(r"^[a-z][a-z0-9]*([A-Z][a-z0-9]*)+$", name):
        return "camelCase"
    if re.match(r"^[A-Z][a-z0-9]+([A-Z][a-z0-9]*)+$", name):
        return "PascalCase"
    if re.match(r"^[a-z][a-z0-9]*$", name):
        return "single"  # one lowercase word fits any convention
    return "mixed"


def split_words(name):
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[-_ .:]+", name) if w]


def is_plural(seg):
    words = split_words(seg)
    if not words:
        return True
    last = words[-1]
    return last in UNCOUNTABLE or (last.endswith("s") and not last.endswith("ss")) or \
        last.endswith("ies") or last in ("people", "children", "men", "women")


def is_param(seg):
    return seg.startswith("{") and seg.endswith("}")


def is_version(seg):
    return bool(re.match(r"^v\d+([._-]?\w+)?$", seg, re.I))


class Linter:
    def __init__(self, spec):
        self.spec = spec
        self.findings = []
        self.stats = Counter()

    def add(self, rule, severity, where, message, fix):
        self.findings.append(OrderedDict(rule=rule, severity=severity, where=where,
                                         message=message, fix=fix))

    # --------------------------------------------------------- schema walking

    def walk_schemas(self):
        """Yield (label, property_name, property_schema, parent_schema) for every
        object property in components and inline schemas."""
        seen_inline = []

        def visit(schema, label, depth=0):
            if depth > 10 or not isinstance(schema, dict) or "$ref" in schema:
                return
            for sub_key in ("allOf", "oneOf", "anyOf"):
                for i, sub in enumerate(schema.get(sub_key) or []):
                    visit(sub, label, depth + 1)
            for pname, pschema in (schema.get("properties") or {}).items():
                yield_list.append((label, pname, pschema, schema))
                visit(pschema, "%s.%s" % (label, pname), depth + 1)
            if isinstance(schema.get("items"), dict):
                visit(schema["items"], label + "[]", depth + 1)
            if isinstance(schema.get("additionalProperties"), dict):
                visit(schema["additionalProperties"], label + "{}", depth + 1)

        yield_list = []
        for name, schema in ((self.spec.get("components") or {}).get("schemas") or {}).items():
            visit(schema, name)
        for path, method, op, shared in iter_operations(self.spec):
            where = "%s %s" % (method.upper(), path)
            body = deref(self.spec, op.get("requestBody") or {})
            _, media = json_content(self.spec, body.get("content"))
            if media and isinstance(media.get("schema"), dict):
                visit(media["schema"], where + " request")
            for code, resp in (op.get("responses") or {}).items():
                resp = deref(self.spec, resp)
                _, media = json_content(self.spec, resp.get("content") if isinstance(resp, dict) else None)
                if media and isinstance(media.get("schema"), dict):
                    visit(media["schema"], "%s %s response" % (where, code))
        for name, item in (self.spec.get("webhooks") or {}).items():
            for method, op in (item or {}).items():
                if isinstance(op, dict):
                    body = deref(self.spec, op.get("requestBody") or {})
                    _, media = json_content(self.spec, body.get("content"))
                    if media and isinstance(media.get("schema"), dict):
                        visit(media["schema"], "webhook %s" % name)
        return yield_list

    # --------------------------------------------------------- rules

    def run(self):
        ops = list(iter_operations(self.spec))
        self.stats["paths"] = len(self.spec.get("paths") or {})
        self.stats["operations"] = len(ops)
        props = self.walk_schemas()
        self.check_path_naming()
        self.check_field_naming(props, ops)
        self.check_operations(ops)
        self.check_ids_and_times(props, ops)
        self.check_money(props)
        self.check_errors_global(ops)
        self.check_versioning()
        self.check_webhooks()
        self.findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["rule"], f["where"]))
        return self.findings

    def check_path_naming(self):
        styles = defaultdict(set)
        for path in self.spec.get("paths") or {}:
            segs = [s for s in path.strip("/").split("/") if s]
            for i, seg in enumerate(segs):
                if is_param(seg) or is_version(seg):
                    continue
                base = seg.split(":")[0]  # allow Google-style custom methods  /x/{id}:cancel
                if not base:
                    continue
                c = word_case(base)
                if c and c != "single":
                    styles[c].add(seg)
                words = split_words(base)
                nxt = segs[i + 1] if i + 1 < len(segs) else None
                if words and words[0] in CRUD_VERBS:
                    self.add("verb-in-path", "medium", path,
                             "Path segment '%s' is a verb. HTTP methods already carry the verb, "
                             "so this makes the API harder to guess." % seg,
                             "Model it as a resource: 'GET /orders' not '/getOrders', "
                             "'POST /orders' not '/orders/create'. Keep verbs only for real "
                             "actions that are not CRUD, like 'POST /orders/{id}/cancel' "
                             "(Google AIP-136 custom methods).")
                if nxt and is_param(nxt) and not is_plural(base):
                    self.add("plural-collections", "medium", path,
                             "Collection segment '%s' is singular but is followed by an ID." % seg,
                             "Use the plural noun for collections ('/orders/{order_id}'), as in "
                             "Google AIP-122, Microsoft and Zalando guidelines.")
        if len(styles) > 1:
            parts = ["%s: %s" % (k, ", ".join(sorted(v))) for k, v in sorted(styles.items())]
            self.add("path-case", "medium", "paths",
                     "Path segments mix naming styles (%s)." % "; ".join(parts),
                     "Pick one style for every path segment. kebab-case is the most common "
                     "for URLs (Zalando: MUST use kebab-case for path segments).")
        elif styles and list(styles)[0] not in ("kebab-case", "snake_case"):
            k = list(styles)[0]
            self.add("path-case", "low", "paths",
                     "Path segments use %s (%s). URLs are often treated as case-insensitive "
                     "by people and tools." % (k, ", ".join(sorted(styles[k]))),
                     "Prefer lowercase kebab-case path segments.")

    def check_field_naming(self, props, ops):
        by_style = defaultdict(list)
        for label, pname, _, _ in props:
            c = word_case(pname)
            if c and c != "single":
                by_style[c].append("%s.%s" % (label, pname))
        for path, method, op, shared in ops:
            for p in op_parameters(self.spec, op, shared):
                if p.get("in") in ("query", "path"):
                    c = word_case(p["name"])
                    if c and c != "single":
                        by_style[c].append("%s %s ?%s" % (method.upper(), path, p["name"]))
        if len(by_style) > 1:
            total = sum(len(v) for v in by_style.values())
            dominant = max(by_style, key=lambda k: len(by_style[k]))
            for style, names in sorted(by_style.items()):
                if style == dominant:
                    continue
                shown = ", ".join(names[:8]) + (" and %d more" % (len(names) - 8) if len(names) > 8 else "")
                self.add("field-case", "medium", "fields",
                         "%d of %d multi-word field and parameter names use %s while most use %s: %s"
                         % (len(names), total, style, dominant, shown),
                         "Use one case for every JSON field and query parameter. snake_case "
                         "(Stripe, Zalando) and camelCase (Microsoft, Google APIs in JSON) are "
                         "both fine. Mixing them is not, because SDK generators and users have "
                         "to remember which is which.")
        self.stats["field_case_styles"] = len(by_style)

        # enum value casing
        for label, pname, pschema, _ in props:
            s = deref(self.spec, pschema)
            values = s.get("enum") if isinstance(s, dict) else None
            if values and all(isinstance(v, str) for v in values):
                cases = {word_case(v) for v in values} - {None, "single"}
                lower = {v == v.lower() for v in values}
                if len(cases) > 1 or len(lower) > 1:
                    self.add("enum-case", "low", "%s.%s" % (label, pname),
                             "Enum values mix cases: %s." % ", ".join(map(str, values)),
                             "Use one case for all enum values (lower snake_case like Stripe, "
                             "or UPPER_SNAKE_CASE like Google and Zalando).")

    def check_operations(self, ops):
        op_ids = Counter()
        global_security = self.spec.get("security")
        for path, method, op, shared in ops:
            where = "%s %s" % (method.upper(), path)
            params = op_parameters(self.spec, op, shared)
            pnames = {p["name"].lower() for p in params}
            headers = {p["name"].lower() for p in params if p.get("in") == "header"}
            responses = op.get("responses") or {}
            codes = [str(c) for c in responses]
            segs = [s for s in path.strip("/").split("/") if s]
            last = segs[-1] if segs else ""

            # operationId
            if not op.get("operationId"):
                self.add("operation-id", "low", where,
                         "No operationId. Generated SDKs will invent method names from the path.",
                         "Add a stable operationId such as 'listOrders' or 'orders.list'.")
            else:
                op_ids[op["operationId"]] += 1

            # documented errors
            error_codes = [c for c in codes if c[0] in "45" or c.lower() == "default"]
            if not error_codes:
                self.add("undocumented-errors", "high", where,
                         "No 4xx, 5xx or default response is documented, so clients cannot "
                         "know what failure looks like.",
                         "Document at least 400/422, 401, 404 where relevant, 429 and a "
                         "'default' error response that uses one shared error schema.")
            for c in error_codes:
                resp = deref(self.spec, responses.get(c) or responses.get(int(c)) if c.isdigit() else responses.get(c))
                if isinstance(resp, dict) and not resp.get("content"):
                    self.add("error-schema", "medium", "%s -> %s" % (where, c),
                             "Error response %s has no body schema." % c,
                             "Return a problem details body (RFC 9457) with a stable machine "
                             "code, a human message and the request id.")
            secured = op.get("security", global_security)
            if secured and not any(c in ("401", "default") for c in codes):
                self.add("undocumented-errors", "low", where,
                         "Operation needs auth but does not document 401.",
                         "Document 401 (missing or bad credentials) and 403 (not allowed).")
            if any(is_param(s) for s in segs) and method in ("get", "put", "patch", "delete") \
                    and "404" not in codes and "default" not in codes:
                self.add("undocumented-errors", "low", where,
                         "Operation addresses one resource but does not document 404.",
                         "Document 404 with the shared error schema.")

            # list endpoints and pagination
            if method == "get" and last and not is_param(last):
                kind = self.list_shape(responses)
                if kind:
                    self.stats["list_endpoints"] += 1
                    has_size = pnames & PAGE_SIZE_PARAMS
                    has_cursor = pnames & CURSOR_PARAMS
                    has_offset = pnames & OFFSET_PARAMS
                    if not (has_size or has_cursor or has_offset):
                        self.add("pagination", "high", where,
                                 "List endpoint has no pagination parameters. It will return "
                                 "everything, which gets slow and then fails as data grows. "
                                 "Adding pagination later is a breaking change.",
                                 "Add 'limit' (with a documented default and maximum) and a "
                                 "cursor ('starting_after' like Stripe, or 'page_token' like "
                                 "Google AIP-158). Return the next cursor in the response.")
                    elif has_offset and not has_cursor:
                        self.add("pagination", "low", where,
                                 "Offset or page-number pagination (%s). It skips or repeats "
                                 "items when rows are added or deleted between pages and gets "
                                 "slow on deep pages." % ", ".join(sorted(has_offset)),
                                 "Prefer an opaque cursor for data that changes. Keep offset "
                                 "only for small, stable collections or UIs that need page "
                                 "numbers.")
                    if kind == "bare-array":
                        self.add("list-envelope", "medium", where,
                                 "List response is a top-level JSON array. You cannot add "
                                 "a next cursor, total or has_more later without breaking "
                                 "every client.",
                                 "Return an object: {\"data\": [...], \"next_cursor\": \"...\", "
                                 "\"has_more\": true}.")
                    elif (has_cursor or has_size) and not self.has_next_field(responses):
                        self.add("pagination", "medium", where,
                                 "Request accepts pagination parameters but the response has no "
                                 "next cursor or has_more field, so clients cannot tell when "
                                 "to stop.",
                                 "Add 'next_cursor' (null on the last page) or 'has_more'.")

            # creates should say so
            is_action = len(segs) >= 2 and is_param(segs[-2])
            if method == "post" and last and not is_param(last) and not is_action and not any(
                    c in codes for c in ("201", "202", "204")) and "200" in codes and not any(
                    w in split_words(last) for w in ("search", "query", "batch")):
                self.add("status-codes", "low", where,
                         "POST to a collection returns 200, not 201 Created.",
                         "Return 201 with a Location header for the new resource (RFC 9110), "
                         "or 202 with an operation to poll if the work is asynchronous.")

            # idempotency on creating POSTs
            if method == "post":
                creates = "201" in codes or (last and not is_param(last) and not
                                             any(w in split_words(last) for w in ("search", "query")))
                looks_action = len(segs) >= 2 and is_param(segs[-2]) if segs else False
                body_fields = self.body_fields(op)
                has_key = bool(headers & IDEMPOTENCY_HEADERS) or bool(
                    {f.lower() for f in body_fields} & IDEMPOTENCY_BODY)
                if (creates or looks_action) and not has_key:
                    money = any(MONEY_NAMES.search(f) for f in body_fields) or any(
                        w in path.lower() for w in ("payment", "charge", "refund", "payout",
                                                    "transfer", "invoice"))
                    self.add("idempotency", "high" if money else "medium", where,
                             "POST %s with no Idempotency-Key header. A client that times out "
                             "cannot safely retry: it may create the thing twice%s."
                             % ("creates or acts" if creates else "performs an action",
                                ", which here means moving money twice" if money else ""),
                             "Accept an 'Idempotency-Key' header (Stripe; IETF draft "
                             "httpapi-idempotency-key-header). Store the key with the first "
                             "response for at least 24 hours and replay it on retries; return "
                             "409 or 422 if the same key comes with a different body.")

            # partial updates
            if method == "patch":
                body = deref(self.spec, op.get("requestBody") or {})
                mt, media = json_content(self.spec, body.get("content"))
                schema = deref(self.spec, (media or {}).get("schema") or {})
                req = schema.get("required") if isinstance(schema, dict) else None
                if req:
                    self.add("partial-update", "medium", where,
                             "PATCH body requires fields (%s). A partial update should let "
                             "the client send only what changes." % ", ".join(req),
                             "Make every field optional in the PATCH schema (JSON Merge Patch, "
                             "RFC 7396), or use an update mask (Google AIP-134).")
            if method == "put" and any(is_param(s) for s in segs) and \
                    not isinstance((self.spec.get("paths") or {}).get(path, {}).get("patch"), dict):
                self.add("partial-update", "low", where,
                         "Only PUT is offered for updates. Clients must send the whole "
                         "resource, and any field they do not know about gets wiped.",
                         "Offer PATCH with merge-patch semantics for partial updates.")

            # rate limit signalling
            if "429" in codes:
                resp = deref(self.spec, responses.get("429"))
                hdrs = {h.lower() for h in ((resp or {}).get("headers") or {})}
                if "retry-after" not in hdrs:
                    self.add("rate-limit", "low", where + " -> 429",
                             "429 is documented without a Retry-After header.",
                             "Send Retry-After (RFC 9110) on 429 and 503, and consider the "
                             "IETF RateLimit and RateLimit-Policy headers.")
            if any(c in codes for c in ("429",)):
                self.stats["has_429"] += 1

        for oid, n in op_ids.items():
            if n > 1:
                self.add("operation-id", "high", "operationId " + oid,
                         "operationId '%s' is used %d times. Generated SDKs will clash." % (oid, n),
                         "Make every operationId unique.")
        if ops and not self.stats["has_429"]:
            self.add("rate-limit", "low", "all operations",
                     "No operation documents 429 Too Many Requests.",
                     "Document 429 with Retry-After so clients and SDKs can back off "
                     "correctly.")

    def list_shape(self, responses):
        for code in ("200", 200):
            resp = deref(self.spec, responses.get(code))
            if not isinstance(resp, dict):
                continue
            _, media = json_content(self.spec, resp.get("content"))
            schema = deref(self.spec, (media or {}).get("schema") or {})
            if not isinstance(schema, dict):
                return None
            if schema.get("type") == "array":
                return "bare-array"
            props = schema_properties(self.spec, schema)
            for name, p in props.items():
                if deref(self.spec, p).get("type") == "array" and name.lower() in LIST_WRAPPERS:
                    return "envelope"
        return None

    def has_next_field(self, responses):
        resp = deref(self.spec, responses.get("200") or responses.get(200))
        _, media = json_content(self.spec, (resp or {}).get("content"))
        props = schema_properties(self.spec, (media or {}).get("schema") or {})
        return any(n.lower() in NEXT_FIELDS for n in props)

    def body_fields(self, op):
        body = deref(self.spec, op.get("requestBody") or {})
        _, media = json_content(self.spec, body.get("content"))
        return list(schema_properties(self.spec, (media or {}).get("schema") or {}).keys())

    def check_ids_and_times(self, props, ops):
        id_types = defaultdict(list)
        time_kinds = defaultdict(list)
        time_suffixes = defaultdict(list)
        for label, pname, pschema, _ in props:
            s = deref(self.spec, pschema)
            if not isinstance(s, dict):
                continue
            t = s.get("type")
            if isinstance(t, list):
                t = next((x for x in t if x != "null"), None)
            where = "%s.%s" % (label, pname)
            if ID_NAMES.search(pname) and t in ("integer", "string", "number"):
                id_types[t if t != "number" else "integer"].append(where)
            desc = str(s.get("description") or "").lower()
            if TIME_NAMES.search(pname) or re.search(r"\b(unix|epoch|timestamp|rfc ?3339|iso ?8601)\b", desc):
                if t == "string" and s.get("format") in ("date-time", "date"):
                    kind = "string/" + s["format"]
                elif t in ("integer", "number"):
                    kind = "unix epoch number"
                elif t == "string":
                    kind = "string with no format"
                else:
                    kind = None
                if kind:
                    time_kinds[kind].append(where)
                m = re.search(r"(_at|At|_time|Time|_date|Date|_on|timestamp)$", pname)
                if m:
                    time_suffixes[m.group(1).lower().lstrip("_")].append(where)
        for path, method, op, shared in ops:
            for p in op_parameters(self.spec, op, shared):
                if p.get("in") == "path" and ID_NAMES.search(p["name"]) or p.get("name") == "id":
                    t = deref(self.spec, p.get("schema") or {}).get("type")
                    if t in ("integer", "string"):
                        id_types[t].append("%s %s {%s}" % (method.upper(), path, p["name"]))
        if len(id_types) > 1:
            self.add("id-format", "medium", "ids",
                     "IDs are integers in some places and strings in others. Integer: %s. "
                     "String: %s." % (", ".join(id_types["integer"][:6]),
                                      ", ".join(id_types["string"][:6])),
                     "Make every ID an opaque string. Clients should never do math on IDs, "
                     "JavaScript loses precision above 2^53, and a string lets you change the "
                     "ID scheme later. Prefixed IDs ('ord_8f2k...') like Stripe's also tell "
                     "people what kind of object an ID points to.")
        elif "integer" in id_types:
            self.add("id-format", "low", "ids",
                     "IDs are integers (%s)." % ", ".join(id_types["integer"][:6]),
                     "Prefer opaque string IDs. Sequential integers leak volume, invite "
                     "enumeration and break in JavaScript above 2^53.")
        if len(time_kinds) > 1:
            parts = ["%s (%s)" % (k, ", ".join(v[:4])) for k, v in sorted(time_kinds.items())]
            self.add("time-format", "medium", "timestamps",
                     "Timestamps use more than one representation: %s." % "; ".join(parts),
                     "Use RFC 3339 strings in UTC with 'format: date-time' everywhere "
                     "(Google AIP-142, Zalando, Microsoft). If you choose Unix seconds like "
                     "Stripe, use them everywhere.")
        elif "string with no format" in time_kinds:
            self.add("time-format", "low", "timestamps",
                     "Time fields are strings with no format: %s."
                     % ", ".join(time_kinds["string with no format"][:6]),
                     "Add 'format: date-time' (RFC 3339) or 'format: date'.")
        if len(time_suffixes) > 1:
            parts = ["%s (%s)" % (k, v[0]) for k, v in sorted(time_suffixes.items())]
            self.add("time-naming", "low", "timestamps",
                     "Time fields use different name suffixes: %s." % ", ".join(parts),
                     "Pick one suffix for instants, for example '_at' ('created_at') or "
                     "'_time' (Google 'create_time'), and use it everywhere.")

    def check_money(self, props):
        for label, pname, pschema, parent in props:
            if not MONEY_NAMES.search(pname):
                continue
            s = deref(self.spec, pschema)
            if not isinstance(s, dict):
                continue
            where = "%s.%s" % (label, pname)
            if s.get("type") == "number":
                self.add("money-format", "medium", where,
                         "Money field '%s' is a JSON number%s. Floats cannot hold 0.1 exactly, "
                         "and clients will round differently." %
                         (pname, " (format %s)" % s["format"] if s.get("format") else ""),
                         "Use an integer in the currency's minor unit ('amount': 1999 means "
                         "19.99, like Stripe), or a decimal that clients must parse as a "
                         "decimal type (a '19.99' string, Zalando's money object, or Google's "
                         "units plus nanos). Always put an ISO 4217 'currency' next to it.")
            siblings = {k.lower() for k in (parent.get("properties") or {})}
            if not any("currency" in k for k in siblings):
                self.add("money-format", "low", where,
                         "Money field '%s' has no currency next to it." % pname,
                         "Add an ISO 4217 'currency' field to the same object.")

    def check_errors_global(self, ops):
        error_schemas = defaultdict(list)
        for path, method, op, shared in ops:
            for code, resp in (op.get("responses") or {}).items():
                c = str(code)
                if not (c[0] in "45" or c == "default"):
                    continue
                name = ref_name(resp)
                resp = deref(self.spec, resp)
                mt, media = json_content(self.spec, (resp or {}).get("content"))
                if not media:
                    continue
                schema = media.get("schema") or {}
                sname = ref_name(schema) or ("inline" if schema else "none")
                error_schemas[sname].append("%s %s -> %s" % (method.upper(), path, c))
                if mt and "problem" in mt:
                    self.stats["problem_json"] += 1
        if len(error_schemas) > 1:
            parts = ["%s (%d responses)" % (k, len(v)) for k, v in sorted(error_schemas.items())]
            self.add("error-schema", "medium", "errors",
                     "Errors use %d different body shapes: %s." % (len(error_schemas), ", ".join(parts)),
                     "Use one error schema for every error response so clients can write one "
                     "error handler.")
        for sname in error_schemas:
            if sname in ("inline", "none"):
                continue
            props = {k.lower() for k in schema_properties(
                self.spec, {"$ref": "#/components/schemas/" + sname})}
            nested = props == {"error"}
            if nested:
                inner = schema_properties(self.spec, schema_properties(
                    self.spec, {"$ref": "#/components/schemas/" + sname}).get("error") or {})
                props = {k.lower() for k in inner}
            if not props & {"code", "type", "error_code", "errorcode"}:
                self.add("error-schema", "high", "schema " + sname,
                         "Error schema '%s' has no stable machine-readable code (fields: %s). "
                         "Clients end up matching on message text, which you can then never "
                         "change." % (sname, ", ".join(sorted(props)) or "none"),
                         "Add a stable 'code' (or RFC 9457 'type' URI), keep 'detail' or "
                         "'message' for humans, and add 'request_id'. See the rewrite in "
                         "reference.md.")
            if not props & {"request_id", "requestid", "trace_id", "traceid", "instance"}:
                self.add("error-schema", "low", "schema " + sname,
                         "Error schema '%s' has no request id." % sname,
                         "Include the request id in the body (and a Request-Id header) so a "
                         "user can quote it to support.")
        if error_schemas and not self.stats["problem_json"]:
            self.add("error-schema", "info", "errors",
                     "No error response uses 'application/problem+json'.",
                     "Consider RFC 9457 problem details: type, title, status, detail, instance, "
                     "plus your own 'code' and 'request_id' extension members.")

    def check_versioning(self):
        versions = set()
        for path in self.spec.get("paths") or {}:
            for seg in path.strip("/").split("/"):
                if is_version(seg):
                    versions.add(seg)
        if len(versions) > 1:
            self.add("versioning", "medium", "paths",
                     "One document mixes path versions: %s." % ", ".join(sorted(versions)),
                     "Publish one document per major version, or version the whole API in the "
                     "server URL. Mixed versions make clients guess which resources moved.")

    def check_webhooks(self):
        hooks = self.spec.get("webhooks") or {}
        if not hooks:
            return
        signed = False
        for name, item in hooks.items():
            for method, op in (item or {}).items():
                if not isinstance(op, dict):
                    continue
                for p in op.get("parameters") or []:
                    p = deref(self.spec, p)
                    if p.get("in") == "header" and "signature" in p.get("name", "").lower():
                        signed = True
                body = deref(self.spec, op.get("requestBody") or {})
                _, media = json_content(self.spec, body.get("content"))
                fields = {k.lower() for k in schema_properties(self.spec, (media or {}).get("schema") or {})}
                missing = [f for f in ("id", "type", "created_at") if not any(
                    f.split("_")[0] in x for x in fields)]
                if missing:
                    self.add("webhooks", "medium", "webhook " + name,
                             "Webhook payload is missing %s." % ", ".join(missing),
                             "Every event needs a unique 'id' (receivers dedupe on it, because "
                             "delivery is at least once), a 'type', and a creation time "
                             "(receivers order on it, because delivery order is not "
                             "guaranteed).")
        if not signed:
            self.add("webhooks", "high", "webhooks",
                     "No webhook documents a signature header. Receivers cannot tell your "
                     "events from forged ones.",
                     "Sign each delivery with HMAC-SHA256 over 'id.timestamp.body' and send "
                     "the id, timestamp and signature as headers (Standard Webhooks spec, or "
                     "Stripe-Signature style). Tell receivers to reject old timestamps.")


# ------------------------------------------------------------------ output

def render(findings, stats, fmt, source):
    counts = Counter(f["severity"] for f in findings)
    if fmt == "json":
        return json.dumps({"source": source, "stats": dict(stats), "counts": dict(counts),
                           "findings": findings}, indent=2)
    grouped = OrderedDict()
    for f in findings:
        grouped.setdefault((f["severity"], f["rule"], f["message"], f["fix"]), []).append(f["where"])
    out = []
    summary = "%d paths, %d operations, %d list endpoints. Findings: %s." % (
        stats["paths"], stats["operations"], stats["list_endpoints"],
        ", ".join("%d %s" % (counts[s], s) for s in ("high", "medium", "low", "info") if counts[s])
        or "none")
    if fmt == "md":
        out.append("# OpenAPI lint: %s\n\n%s\n" % (os.path.basename(source), summary))
        for (sev, rule, msg, fix), wheres in grouped.items():
            out.append("- **%s** `%s` at %s\n  %s\n  Fix: %s" % (
                sev.upper(), rule, ", ".join("`%s`" % w for w in wheres), msg, fix))
        return "\n".join(out) + "\n"
    out.append("OpenAPI lint: %s" % source)
    out.append(summary)
    for (sev, rule, msg, fix), wheres in grouped.items():
        out.append("")
        out.append("[%s] %s  @ %s" % (sev.upper(), rule, "; ".join(wheres)))
        out.append("    " + msg)
        out.append("    fix: " + fix)
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("spec", help="OpenAPI 3.x file (.json, .yaml, .yml)")
    ap.add_argument("--format", choices=("text", "md", "json"), default="text")
    ap.add_argument("--fail-on", choices=("high", "medium", "low"),
                    help="exit 1 if any finding at this severity or worse (for CI)")
    args = ap.parse_args()
    spec = load_spec(args.spec)
    if not isinstance(spec, dict) or "paths" not in spec:
        sys.exit("%s does not look like an OpenAPI document (no 'paths')." % args.spec)
    if str(spec.get("swagger", "")).startswith("2"):
        print("note: Swagger 2.0 input. Results are partial; convert to OpenAPI 3 for a full lint.",
              file=sys.stderr)
    linter = Linter(spec)
    findings = linter.run()
    sys.stdout.write(render(findings, linter.stats, args.format, args.spec))
    if args.fail_on:
        limit = SEVERITY_ORDER[args.fail_on]
        if any(SEVERITY_ORDER[f["severity"]] <= limit for f in findings):
            sys.exit(1)


if __name__ == "__main__":
    main()
