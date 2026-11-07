"""Map the attack surface of a Python or JS/TS codebase with regex heuristics. Standard library only.

It finds routes and handlers (and whether an auth or ownership check shows up near them), SQL built with string
formatting, shell/eval/deserialisation sinks, file paths built from input, outbound HTTP with non-literal URLs,
CORS/CSRF/cookie settings, webhook handlers without signature checks, LLM tool use, and more.
Every hit is a lead to read, not a finding. Lines that use a variable taken from the request are tagged [input].

Run: python3 surface.py <dir> [--json] [--only CATEGORY,...]
"""
import json, os, re, sys
from collections import defaultdict
from pathlib import Path

PY = {".py"}
JS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", ".next", ".nuxt",
             "coverage", "site-packages", ".tox", ".mypy_cache", ".pytest_cache", "vendor", "out"}
MAX_BYTES = 1_000_000

# (category, languages, regex, hint). "both" means py and js.
RULES = [
    # injection
    ("sql-format", "py", r"""f["']{1,3}[^"']*((?i:select\s[^"'`]*\sfrom|insert\s+into|update\s+\w+\s+set|delete\s+from)|\bWHERE\b|\bORDER\s+BY\b)[^"']*\{""",
     "SQL built with an f-string. Use placeholders (?, %s, :name) and pass values separately."),
    ("sql-format", "py", r"""["'][^"']*((?i:select\s[^"'`]*\sfrom|insert\s+into|update\s+\w+\s+set|delete\s+from)|\bWHERE\b|\bORDER\s+BY\b)[^"']*["']\s*(%\s*[\w(]|\.format\(|\+\s*\w)""",
     "SQL built with % / .format / +. Use placeholders."),
    ("sql-format", "js", r"""(?<![\w.]sql)(?<!Prisma\.sql)`[^`]*((?i:select\s[^"'`]*\sfrom|insert\s+into|update\s+\w+\s+set|delete\s+from)|\bWHERE\b|\bORDER\s+BY\b)[^`]*\$\{""",
     "SQL in a template literal with ${}. Use parameters or a tagged sql`` helper."),
    ("sql-format", "js", r"""["'][^"']*((?i:select\s[^"'`]*\sfrom|insert\s+into|update\s+\w+\s+set|delete\s+from)|\bWHERE\b|\bORDER\s+BY\b)[^"']*["']\s*\+""",
     "SQL built with string concatenation."),
    ("sql-format", "both", r"\$queryRawUnsafe|\$executeRawUnsafe|\.raw\(\s*`[^`]*\$\{|sequelize\.query\(\s*`[^`]*\$\{|\.extra\(|RawSQL\(",
     "Raw query API with interpolated input."),
    ("command-exec", "py", r"subprocess\.\w+\([^)]*shell\s*=\s*True|\bos\.system\(|\bos\.popen\(|commands\.getoutput",
     "Shell command. Pass an argument list with shell=False, never a string built from input."),
    ("command-exec", "js", r"child_process|(?<![\w.])exec(Sync)?\(|spawn\([^)]*shell:\s*true|\bexeca\(",
     "Shell command. Use execFile/spawn with an argument array, never a string built from input."),
    ("code-eval", "py", r"(?<![\w.])(eval|exec)\(|__import__\(|compile\([^)]*['\"]exec['\"]",
     "eval/exec of a string. If any part can come from a user or a model, this is remote code execution."),
    ("code-eval", "js", r"(?<![\w.])eval\(|new Function\(|vm\.runIn\w*\(|setTimeout\(\s*['\"`]",
     "Dynamic code evaluation."),
    ("deserialise", "py", r"pickle\.loads?\(|cPickle|dill\.loads?\(|marshal\.loads?\(|shelve\.open\(|jsonpickle\.decode\(|yaml\.load\((?![^)]*SafeLoader)|yaml\.unsafe_load|torch\.load\((?![^)]*weights_only\s*=\s*True)|joblib\.load\(",
     "Unsafe deserialisation runs code if the bytes come from outside. Use JSON, yaml.safe_load, weights_only=True."),
    ("deserialise", "js", r"node-serialize|\bunserialize\(|serialize-javascript|funcster|js-yaml.*\.load\(|\byaml\.load\(",
     "Deserialiser that can construct code or arbitrary objects."),
    ("template-xss", "py", r"render_template_string\(|Markup\(|mark_safe\(|\|\s*safe\b|autoescape\s*=\s*False|Environment\((?![^)]*autoescape)",
     "Server-side template or HTML built without escaping (SSTI or XSS)."),
    ("template-xss", "js", r"dangerouslySetInnerHTML|\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML|v-html|document\.write\(|\{\{\{",
     "Raw HTML insertion. Sanitise (DOMPurify) or render as text."),
    ("path-traversal", "py", r"\bopen\(|send_file\(|send_from_directory\(|FileResponse\(|os\.path\.join\(|Path\([^)]*\)\s*/|shutil\.\w+\(|os\.remove\(|os\.unlink\(|zipfile|tarfile\.open|\.extractall\(",
     "File path. If any part comes from input, resolve it and check it stays inside the base folder."),
    ("path-traversal", "js", r"fs\.\w+\(|fsPromises\.\w+\(|res\.sendFile\(|res\.download\(|path\.join\(|path\.resolve\(|createReadStream\(",
     "File path. If any part comes from input, resolve it and check it stays inside the base folder."),
    ("outbound-http", "py", r"requests\.(get|post|put|patch|delete|head|request)\(\s*(?!['\"])|httpx\.(get|post|put|request|stream)\(\s*(?!['\"])|urlopen\(\s*(?!['\"])|aiohttp\.\w+|\.get\(\s*url\b|urllib3",
     "Outbound request to a URL that is not a literal. If a user or a model controls it, this is SSRF."),
    ("outbound-http", "js", r"(?<![\w.])fetch\(\s*(?!['\"]|`[^`$]*`)|axios(\.\w+)?\(\s*(?!['\"])|(?<![\w.])got(\.\w+)?\(\s*(?!['\"])|(?<![\w.])https?\.(get|request)\(\s*(?!['\"])|(?<![\w.])ky\(\s*(?!['\"])",
     "Outbound request to a URL that is not a literal. If a user or a model controls it, this is SSRF."),
    ("open-redirect", "both", r"redirect\([^)]*(request\.|req\.|next|return_?to|returnUrl|callback|url)",
     "Redirect target from input. Allow only relative paths or a fixed list of hosts."),
    ("mass-assignment", "both", r"\*\*request\.(json|form|data|get_json\(\))|\*\*(data|payload|body)\)|Object\.assign\(\w+,\s*req\.body|\.\.\.req\.body|data:\s*req\.body|\.update\(\s*req\.body|\.create\(\s*req\.body",
     "Whole request body copied into a model. A client can set role, owner_id, is_admin, price."),
    # web config
    ("cors", "both", r"Access-Control-Allow-Origin['\"]?\s*[,:]\s*['\"]\*|allow_origins\s*=\s*\[\s*['\"]\*|origin:\s*(true|['\"]\*['\"])|(?<!\w)cors\(\s*\)|CORS\(\s*\w+\s*\)\s*$|supports_credentials\s*=\s*True|allow_credentials\s*=\s*True|credentials:\s*true|req\.headers\.origin",
     "CORS. Wildcard or reflected origin plus credentials lets any site read your API as the user."),
    ("csrf-cookie", "both", r"csrf_exempt|csrf\.exempt|WTF_CSRF_ENABLED\s*=\s*False|CSRF_ENABLED\s*=\s*False|httponly\s*=\s*False|httpOnly:\s*false|secure\s*=\s*False|secure:\s*false|samesite\s*=\s*['\"]?None|sameSite:\s*['\"]none",
     "CSRF or cookie flag weakened. Cookie auth needs SameSite=Lax/Strict or a CSRF token on every state change."),
    ("jwt", "both", r"verify\s*=\s*False(?=.*jwt)|jwt.*verify\s*=\s*False|verify_signature['\"]?\s*:\s*False|algorithms\s*=\s*\[[^\]]*['\"]none|jwt\.decode\((?!.*algorithms)|ignoreExpiration:\s*true|algorithms:\s*\[[^\]]*['\"]none",
     "JWT decoded without full verification (signature, algorithm list, expiry). In JS, jwt.decode does not verify."),
    ("weak-random", "both", r"(random\.(random|randint|choice|choices)\(|Math\.random\(\)|uuid1\().*(token|secret|password|otp|nonce|session|reset|invite)|(token|secret|password|otp|nonce|session|reset)\w*\s*=\s*.*(random\.(random|randint|choice)|Math\.random|uuid1)",
     "Guessable value used as a secret. Use secrets.token_urlsafe / crypto.randomBytes."),
    ("weak-crypto", "both", r"(md5|sha1)\(.*(pass|pwd)|(pass|pwd)\w*.*(md5|sha1)\(|hashlib\.(md5|sha1)\(|createHash\(['\"](md5|sha1)",
     "MD5/SHA1. Fine for cache keys, wrong for passwords (use bcrypt/argon2/scrypt/pbkdf2) or signatures (HMAC-SHA256)."),
    ("timing-compare", "both", r"(signature|sig|token|hmac|digest|api_?key|secret)\w*\s*(==|!=|===|!==)\s*\w|\w\s*(==|!=|===|!==)\s*\w*(signature|sig|token|hmac|digest|secret)\b",
     "Secret compared with ==. Use hmac.compare_digest / crypto.timingSafeEqual."),
    ("debug-tls", "both", r"debug\s*=\s*True|DEBUG\s*=\s*True|app\.run\([^)]*debug|verify\s*=\s*False|rejectUnauthorized:\s*false|NODE_TLS_REJECT_UNAUTHORIZED|CERT_NONE|check_hostname\s*=\s*False",
     "Debug mode or TLS verification off. Debug consoles are remote code execution if reachable."),
    ("error-leak", "both", r"\{\s*['\"]error['\"]\s*:\s*(str|repr)\(|(return|jsonify|Response|send|body\s*=).*traceback\.format_exc\(\)|(json|send|jsonify|Response)\(.*(err|error|e)\.(message|stack)|stack:\s*(err|error|e)\.stack",
     "Exception text sent to the client. It leaks SQL, paths and versions and turns blind attacks into easy ones."),
    ("upload", "both", r"request\.files|UploadFile|multer\(|formidable|busboy|\.save\([^)]*filename|req\.file\b|req\.files\b|upload_to",
     "File upload. Check size limit, type by content not extension, random stored name, storage outside web root, no serving as HTML."),
    ("secrets-in-logs", "both", r"(log(ger)?\.\w+|console\.\w+|print)\(.*(password|passwd|token|secret|authorization|api_?key|cookie)",
     "Possible secret written to logs."),
    ("client-secret", "js", r"(NEXT_PUBLIC_|VITE_|REACT_APP_|EXPO_PUBLIC_|PUBLIC_)\w*(SECRET|PRIVATE|SERVICE_ROLE|API_KEY|TOKEN)",
     "Env var with a public prefix is shipped to every browser. Secrets never get a public prefix."),
    ("llm-call", "both", r"openai|anthropic|google\.generativeai|google\.genai|GoogleGenerativeAI|messages\.create\(|chat\.completions|generateText\(|streamText\(|generateContent\(|langchain|litellm|ollama",
     "LLM call. Treat everything the model reads (user text, tool results, fetched pages, files) as untrusted."),
    ("llm-tools", "both", r"tools\s*[=:]\s*\[|@tool\b|\btool\(\{|function_declarations|['\"]type['\"]\s*:\s*['\"]function['\"]|input_schema|tool_choice|bind_tools\(|StructuredTool|createTool|defineTool",
     "LLM tool definitions. For each tool: what is the worst thing it does if the model is hijacked by injected text?"),
]

ROUTE_PY = re.compile(r"""^\s*@[\w.]*\.?(route|get|post|put|patch|delete|api_route|websocket)\(\s*(?:['"]\w+['"]\s*,\s*)?r?['"]([^'"]+)['"]""")
ROUTE_DJANGO = re.compile(r"""\b(re_)?path\(\s*r?['"]([^'"]*)['"]\s*,\s*([\w.]+)""")
ROUTE_JS = re.compile(r"""\b(app|router|server|api|fastify|r)\.(get|post|put|patch|delete|all|use)\(\s*['"`]([^'"`]+)['"`]""")
ROUTE_NEXT = re.compile(r"""^\s*export\s+(async\s+)?(function|const)\s+(GET|POST|PUT|PATCH|DELETE)\b""")
AUTH_MARK = re.compile(r"login_required|auth|Auth|current_user|currentUser|require_user|requireUser|get_user|getUser|request\.user|req\.user|getServerSession|getSession|session\[|jwt_required|token_required|permission|IsAuthenticated|Depends\(|verify_token|verifyToken|clerk|requireSession|withAuth|protected")
OWNER_MARK = re.compile(r"owner|user_id|userId|user\.id|user\[['\"]id|tenant|org_?id|orgId|organization|account_?id|accountId|workspace|team_?id|teamId|current_user|currentUser\.|req\.user\.|request\.user|session\.user|can_access|authorize|policy|ability|permission")
PARAM = re.compile(r"<[^>]+>|:\w+|\{\w+\}|\[\w+\]|\(\\d\+\)|\([^)]*\)")
WEBHOOK_VERIFY = re.compile(r"hmac|signature|Signature|constructEvent|verify|svix|timingSafeEqual|compare_digest", re.I)
LLM_IN_BODY = re.compile(r"generate(Text|Content|Object)\(|streamText\(|messages\.create\(|chat\.completions|responses\.create\(|\bllm\b|\bcomplete\(|ask_?model|callModel|runAgent|agent\.run", re.I)
RATELIMIT = re.compile(r"limiter|rate_?limit|ratelimit|RateLimit|express-rate-limit|slowapi|throttl|upstash/ratelimit", re.I)
SENSITIVE_ROUTE = re.compile(r"login|signin|sign-in|signup|register|reset|forgot|otp|verify|token|invite|password|magic", re.I)
INPUT_SRC = re.compile(r"""request\.|req\.(query|params|body|headers|cookies)|\.query\.get\(|args\.get\(|form\.get\(|get_json\(|\.json\(\)|searchParams|params\[|query\[|body\[|formData\(|input\(""")
ASSIGN = re.compile(r"""^\s*(?:const |let |var )?\s*\{?\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*\}?(?:\s*:\s*[\w<>\[\]|. ]{1,60}?)?\s*=(?!=)(.+)$""")


def lang_of(p):
    return "py" if p.suffix in PY else "js" if p.suffix in JS else None


def walk(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            p = Path(dirpath) / f
            if lang_of(p) and p.stat().st_size < MAX_BYTES and not f.endswith((".min.js", ".d.ts")):
                yield p


def tainted_names(lines):
    """Variable names assigned from something request-shaped, plus handler parameters. Crude on purpose."""
    names = set()
    for ln in lines:
        m = ASSIGN.match(ln)
        if m and INPUT_SRC.search(m.group(2)):
            for n in re.split(r"[,\s]+", m.group(1)):
                if n and n.isidentifier() and n not in {"const", "let", "var", "self"}:
                    names.add(n)
    return names


def block_after(lines, i, lang):
    """Rough handler body: for Python the indented block of the next def, for JS the next 30 lines."""
    if lang == "js":
        return lines[i:i + 30]
    j = i
    while j < len(lines) and not re.match(r"\s*(async\s+)?def\s", lines[j]):
        j += 1
    if j >= len(lines):
        return lines[i:i + 30]
    indent = len(lines[j]) - len(lines[j].lstrip())
    body = [lines[j]]
    for ln in lines[j + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        body.append(ln)
    return lines[i:j] + body


def handler_params(body):
    for ln in body:
        m = re.match(r"\s*(?:async\s+)?def\s+\w+\(([^)]*)\)", ln)
        if m:
            return {a.split(":")[0].split("=")[0].strip() for a in m.group(1).split(",")} - {"self", "request", "req", "ctx", ""}
    return set()


def scan(root, only=None):
    root = Path(root).resolve()
    hits = defaultdict(list)
    routes, notes = [], []
    project_text_flags = defaultdict(set)
    compiled = [(c, l, re.compile(rx, re.I if c in {"weak-random", "secrets-in-logs", "timing-compare"} else 0), h)
                for c, l, rx, h in RULES]
    for p in walk(root):
        rel = str(p.relative_to(root))
        lang = lang_of(p)
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        taint = tainted_names(lines)
        file_level_auth = bool(re.search(r"\.use\([^)]*(auth|Auth|session|requireUser|clerk)|before_request|middleware", text))
        if "BaseHTTPRequestHandler" in text or "createServer(" in text:
            notes.append(f"{rel}: hand-rolled HTTP routing. Check every branch for auth, there is no framework doing it.")
        if RATELIMIT.search(text):
            project_text_flags["ratelimit"].add(rel)
        for i, ln in enumerate(lines):
            s = ln.strip()
            if len(ln) > 2000 or s.startswith(("#", "//", "*", "/*")):
                continue
            # routes
            for rx, kind in ((ROUTE_PY, "py"), (ROUTE_DJANGO, "django"), (ROUTE_JS, "js"), (ROUTE_NEXT, "next")):
                if (kind in {"py", "django"} and lang != "py") or (kind in {"js", "next"} and lang != "js"):
                    continue
                m = rx.search(ln)
                if not m:
                    continue
                if kind == "next":
                    path = "/" + "/".join(x for x in p.parent.relative_to(root).parts if x not in {"app", "src", "pages"}) + f" [{m.group(3)}]"
                elif kind == "js" and m.group(2) == "use":
                    continue
                else:
                    path = m.group(2) if kind in {"py", "django"} else m.group(3)
                body = block_after(lines, i, "js" if kind in {"js", "next"} else "py")
                body_text = "\n".join(body)
                taint |= handler_params(body)
                has_auth = bool(AUTH_MARK.search(body_text)) or file_level_auth
                has_param = bool(PARAM.search(re.sub(r" \[[A-Z]+\]$", "", path)))
                flags = []
                if not has_auth:
                    flags.append("NO-AUTH-SEEN")
                if has_param and not OWNER_MARK.search(body_text):
                    flags.append("IDOR?")
                if re.search(r"webhook|/hook", path, re.I) and not WEBHOOK_VERIFY.search(body_text):
                    flags.append("WEBHOOK-UNVERIFIED?")
                if SENSITIVE_ROUTE.search(path):
                    flags.append("ABUSE-TARGET")
                if LLM_IN_BODY.search(body_text) and not has_auth:
                    flags.append("LLM-COST")
                routes.append({"file": rel, "line": i + 1, "route": path, "flags": flags})
            # rules
            for cat, l, rx, hint in compiled:
                if only and cat not in only:
                    continue
                if l != "both" and l != lang:
                    continue
                if rx.search(ln):
                    if cat == "path-traversal" and not (taint and re.search(r"\b(" + "|".join(map(re.escape, taint)) + r")\b", ln)) and not INPUT_SRC.search(ln):
                        continue  # file ops are everywhere; only keep ones touching input
                    used = sorted(n for n in taint if re.search(r"\b" + re.escape(n) + r"\b", ln)) if taint else []
                    tag = "[input] " if used or INPUT_SRC.search(ln) else ""
                    hits[cat].append({"file": rel, "line": i + 1, "code": s[:160], "input": bool(tag), "hint": hint})
                    if cat in {"llm-call", "llm-tools"}:
                        project_text_flags[cat].add(rel)
                    if cat in {"command-exec", "outbound-http", "code-eval"}:
                        project_text_flags["dangerous-" + cat].add(rel)
    if routes and not project_text_flags["ratelimit"] and any("ABUSE-TARGET" in r["flags"] for r in routes):
        notes.append("No rate limiting library or code found, and there are login/signup/reset style routes.")
    if project_text_flags["llm-tools"]:
        danger = project_text_flags["dangerous-outbound-http"] | project_text_flags["dangerous-command-exec"] | project_text_flags["dangerous-code-eval"]
        if danger:
            notes.append("LLM tools exist and the code also makes outbound requests or runs commands ("
                         + ", ".join(sorted(danger)[:5]) + "). Check whether a tool can reach these with model-chosen arguments: "
                         "that is the prompt injection to exfiltration or code execution path.")
    return root, routes, hits, notes


ORDER = ["sql-format", "command-exec", "code-eval", "deserialise", "path-traversal", "outbound-http", "template-xss",
         "jwt", "mass-assignment", "open-redirect", "cors", "csrf-cookie", "timing-compare", "weak-random", "weak-crypto",
         "client-secret", "secrets-in-logs", "error-leak", "upload", "debug-tls", "llm-tools", "llm-call"]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].split(","))
        args = [a for a in args if a != sys.argv[sys.argv.index("--only") + 1]]
    root, routes, hits, notes = scan(args[0] if args else ".", only)
    if "--json" in sys.argv:
        print(json.dumps({"routes": routes, "hits": hits, "notes": notes}, indent=2))
        return
    print(f"Attack surface of {root.name}/  (heuristic leads: read each one before calling it a finding)\n")
    print(f"ROUTES ({len(routes)})")
    for r in routes:
        print(f"  {r['file']}:{r['line']}  {r['route']}  {' '.join(r['flags'])}")
    if routes:
        print("  flags: NO-AUTH-SEEN = no auth call near the handler; IDOR? = takes an id but no owner/tenant check seen;")
        print("         WEBHOOK-UNVERIFIED? = no signature check seen; ABUSE-TARGET = needs rate limiting;")
        print("         LLM-COST = anyone can make you pay for model calls (needs auth, rate limit and a spend cap)")
    for cat in ORDER:
        if not hits.get(cat):
            continue
        items = sorted(hits[cat], key=lambda h: (not h["input"], h["file"], h["line"]))
        print(f"\n{cat.upper()} ({len(items)})  {items[0]['hint']}")
        for h in items[:40]:
            print(f"  {'[input] ' if h['input'] else ''}{h['file']}:{h['line']}  {h['code']}")
        if len(items) > 40:
            print(f"  ... {len(items) - 40} more (use --json)")
    if notes:
        print("\nNOTES")
        for n in notes:
            print("  - " + n)


if __name__ == "__main__":
    main()
