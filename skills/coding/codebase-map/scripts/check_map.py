#!/usr/bin/env python3
"""Check every `path/to/file.ext:123` reference in a MAP.md against the repo: the file must exist and have
at least that many lines. Catches made-up or stale hops before anyone trusts the map.

Usage:
  python3 check_map.py <MAP.md> <repo>
Exit code 1 if any reference is broken. Standard library only.
"""
import re
import sys
from pathlib import Path

REF = re.compile(r"(?<![\w/.-])((?:[\w.@+-]+/)*[\w.@+-]+\.[A-Za-z0-9]+):(\d+)\b")


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    text = Path(sys.argv[1]).read_text()
    repo = Path(sys.argv[2]).resolve()
    ok = bad = 0
    seen = set()
    for m in REF.finditer(text):
        path, line = m.group(1), int(m.group(2))
        if (path, line) in seen or re.match(r"^\d", path):  # skip times like 10.5:30
            continue
        seen.add((path, line))
        f = repo / path
        if not f.is_file():
            print(f"MISSING  {path}:{line}")
            bad += 1
            continue
        n = sum(1 for _ in open(f, "rb"))
        if line > n:
            print(f"TOO FAR  {path}:{line} (file has {n} lines)")
            bad += 1
        else:
            ok += 1
    print(f"{ok} references ok, {bad} broken")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
