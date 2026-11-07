"""Shared helpers for the codebase-map scripts: list a repo's source files the way git sees them,
skip vendored, generated and minified files, and read text safely. Standard library only (Python 3.9+).
"""
import os
import re
import subprocess
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "out", ".next", ".nuxt",
    ".svelte-kit", ".turbo", ".cache", "coverage", "target", "vendor", "third_party", "site-packages",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "bower_components", ".gradle", ".idea", ".vscode",
}
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "poetry.lock", "uv.lock", "Pipfile.lock",
    "Cargo.lock", "go.sum", "composer.lock", "Gemfile.lock", "npm-shrinkwrap.json",
}
GENERATED_RE = re.compile(
    r"(\.min\.(js|css)$|\.map$|\.snap$|_pb2(_grpc)?\.py$|\.pb\.go$|\.generated\.|\.d\.ts$|"
    r"(^|/)(generated|__generated__|gen)/|\.lock$|\.svg$|\.ipynb$|tsconfig\.tsbuildinfo$)"
)
CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rb", ".java", ".kt", ".scala", ".rs", ".c",
    ".h", ".cc", ".cpp", ".hpp", ".cs", ".php", ".swift", ".m", ".vue", ".svelte", ".sh", ".sql", ".ex", ".exs",
    ".lua", ".dart", ".r", ".jl", ".zig",
}


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else ""


def is_git_repo(repo):
    return git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"


def skipped(path):
    parts = path.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return True
    if parts[-1] in LOCKFILES:
        return True
    return bool(GENERATED_RE.search(path))


def list_files(repo, under=""):
    """Tracked plus untracked-but-not-ignored files, relative to repo, with skip rules applied."""
    repo = Path(repo)
    if is_git_repo(repo):
        out = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        files = [f for f in out.split("\0") if f]
    else:
        files = []
        for root, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for n in names:
                files.append(os.path.relpath(os.path.join(root, n), repo).replace(os.sep, "/"))
    if under:
        under = under.strip("/") + "/"
        files = [f for f in files if f.startswith(under)]
    return sorted(f for f in set(files) if not skipped(f) and (repo / f).is_file())


def read_text(path, limit=2_000_000):
    """Return file text, or None for binary, huge or unreadable files."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit or b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def is_test_path(path):
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    return bool(
        re.search(r"(^|/)(tests?|__tests__|spec|specs|e2e|testing|fixtures|cassettes)/", p)
        or re.match(r"test_.*\.py$|.*_test\.(py|go)$|.*\.(test|spec)\.[jt]sx?$|conftest\.py$", name)
    )


def md_table(headers, rows):
    def cell(v):
        return str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)
