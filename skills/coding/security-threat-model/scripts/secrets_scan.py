"""Find secrets in a codebase: known key formats, secret-looking assignments, and high-entropy strings, in the
current files and (with --history) in every commit. Also flags secret-named env vars with a public prefix
(NEXT_PUBLIC_, VITE_, ...) and built bundles that contain keys. Secrets are always printed masked.
Standard library only.

Run: python3 secrets_scan.py <dir> [--history] [--bundles] [--json]
Exit code 1 if anything is found, so it works as a test or a pre-commit check.
"""
import json, math, os, re, subprocess, sys
from pathlib import Path

PATTERNS = {
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----",
    "AWS access key id": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "GitHub token": r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b",
    "OpenAI-style key": r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}",
    "Anthropic key": r"\bsk-ant-[A-Za-z0-9_-]{32,}",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "Slack token": r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    "Slack webhook": r"hooks\.slack\.com/services/T[A-Za-z0-9_/]+",
    "Stripe-style key": r"\b(?:sk|rk|pk|whsec)_(?:live|test)_[A-Za-z0-9_]{8,}",
    "Twilio key": r"\bSK[0-9a-fA-F]{32}\b",
    "SendGrid key": r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
    "JWT": r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    "connection string with password": r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^\s:/@'\"]+:[^\s@'\"]{3,}@",
    "Supabase service role": r"SUPABASE_SERVICE_ROLE_KEY\s*[:=]\s*['\"]?eyJ",
}
ASSIGN = re.compile(r"""(?i)\b([\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|private[_-]?key|client[_-]?secret|auth|credential|signing[_-]?key)[\w.-]*)['"]?\s*[:=]\s*['"]([^'"\s]{8,})['"]""")
PUBLIC_PREFIX = re.compile(r"\b(NEXT_PUBLIC_|VITE_|REACT_APP_|EXPO_PUBLIC_|PUBLIC_|GATSBY_)\w*(SECRET|PRIVATE|SERVICE_ROLE|PASSWORD|TOKEN|API_KEY)\w*")
CANDIDATE = re.compile(r"""['"`=:]\s*['"]?([A-Za-z0-9+/=_\-]{24,})['"]?""")
PLACEHOLDER = re.compile(r"(?i)x{6,}|your[_-]|<[^>]+>|\.\.\.|changeme|placeholder|example|redacted|dummy|\*{4,}|^(.)\1+$")
ALLOW = re.compile(r"pragma: allowlist secret|secrets-scan: ignore")  # reviewed false positive, same marker as detect-secrets
FAKE_MARK = re.compile(r"(?i)fake|not[_-]?a[_-]?real|do[_-]?not[_-]?use")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", "site-packages", "coverage"}
BUNDLE_DIRS = {"dist", "build", ".next", "out", ".output", ".nuxt", "public"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".mp4", ".mov", ".mp3", ".wav",
            ".woff", ".woff2", ".ttf", ".otf", ".pyc", ".so", ".dylib", ".lock", ".svg", ".sqlite", ".db"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock", "go.sum"}
COMPILED = {k: re.compile(v) for k, v in PATTERNS.items()}


def entropy(s):
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def high_entropy(s):
    if PLACEHOLDER.search(s) or len(s) < 24:
        return False
    if re.fullmatch(r"[0-9a-f]+", s):  # hex: commit hashes and checksums are common, need length and entropy
        return len(s) >= 32 and entropy(s) > 3.5 and not re.fullmatch(r"[0-9a-f]{40}", s)
    has_mix = re.search(r"[a-z]", s) and re.search(r"[A-Z]", s) and re.search(r"\d", s)
    return bool(has_mix) and entropy(s) > 4.2


def mask(s):
    return s[:4] + "*" * min(len(s) - 6, 12) + s[-2:] if len(s) > 10 else s[:2] + "****"


def check_line(line):
    """Return a list of (kind, secret) for one line of text."""
    out, seen = [], set()
    for kind, rx in COMPILED.items():
        for m in rx.finditer(line):
            out.append((kind, m.group(0)))
            seen.add(m.group(0))
    for m in ASSIGN.finditer(line):
        val = m.group(2)
        if any(val in s or s in val for s in seen) or PLACEHOLDER.search(val):
            continue
        if re.search(r"(?i)^(true|false|null|none|process\.env|os\.environ|env\.|\$\{|\{\{)", val) or re.fullmatch(r"[\w.]+\(\)?", val) and "_" in val and val.islower():
            continue
        if entropy(val) < 2.5:
            continue
        out.append((f"secret assigned to {m.group(1)}", val))
        seen.add(val)
    for m in CANDIDATE.finditer(line):
        val = m.group(1)
        if any(val in s or s in val for s in seen):
            continue
        if high_entropy(val):
            out.append(("high-entropy string", val))
            seen.add(val)
    for m in PUBLIC_PREFIX.finditer(line):
        out.append(("secret-named env var with a public prefix (shipped to browsers)", m.group(0)))
    return out


def iter_files(root, bundles):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and (bundles or d not in BUNDLE_DIRS)]
        for f in files:
            p = Path(dirpath) / f
            if p.suffix.lower() in SKIP_EXT or f in SKIP_NAMES:
                continue
            try:
                if p.stat().st_size > 2_000_000:
                    continue
                yield p, p.read_text(errors="replace")
            except OSError:
                continue


def scan_files(root, bundles=False):
    found = []
    for p, text in iter_files(root, bundles):
        rel = str(p.relative_to(root))
        if p.name == ".env" or p.name.startswith(".env.") and not p.name.endswith((".example", ".sample", ".template")):
            found.append({"where": rel, "line": 0, "kind": "env file present (should be gitignored, never committed)", "masked": "", "fake": False})
        for i, line in enumerate(text.splitlines(), 1):
            if ALLOW.search(line):
                continue
            if len(line) > 5000:  # minified bundle line: only known formats
                hits = [(k, m.group(0)) for k, rx in COMPILED.items() for m in rx.finditer(line)]
            else:
                hits = check_line(line)
            for kind, val in hits:
                shown = val if kind.startswith("secret-named env var") else mask(val)  # a variable name is not a secret
                found.append({"where": rel, "line": i, "kind": kind, "masked": shown, "fake": bool(FAKE_MARK.search(line))})
    return found


def scan_history(root):
    git = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, errors="replace")
    if git("rev-parse", "--git-dir").returncode != 0:
        return [], "not a git repo, history skipped"
    out = git("log", "--all", "-p", "--no-color", "--unified=0", "--format=@@COMMIT %h").stdout
    found, commit, path, seen = [], "?", "?", set()
    for line in out.splitlines():
        if line.startswith("@@COMMIT "):
            commit = line.split()[1]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and not ALLOW.search(line):
            for kind, val in check_line(line[1:]):
                key = (path, val)
                if key in seen:
                    continue
                seen.add(key)
                found.append({"where": f"{commit}:{path}", "line": 0, "kind": kind, "masked": mask(val), "fake": bool(FAKE_MARK.search(line))})
    return found, None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = Path(args[0] if args else ".").resolve()
    found = scan_files(root, "--bundles" in sys.argv)
    note = None
    if "--history" in sys.argv:
        hist, note = scan_history(root)
        found += [h for h in hist]
    if "--json" in sys.argv:
        print(json.dumps(found, indent=2))
    else:
        if not found:
            print(f"No secrets found in {root.name}/" + (" (files and history)" if "--history" in sys.argv else " (files only; add --history)"))
        for f in found:
            loc = f"{f['where']}:{f['line']}" if f["line"] else f["where"]
            print(f"  {loc}  {f['kind']}  {f['masked']}{'  (marked fake)' if f['fake'] else ''}")
        if found:
            print(f"\n{len(found)} finding(s). A real secret that was ever committed or shipped is leaked: rotate it at the provider first,"
                  " then move it to an env var or secret manager. Deleting the line does not remove it from history.")
        if note:
            print(f"({note})")
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
