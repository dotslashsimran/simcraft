#!/usr/bin/env python3
"""List every installed skill (name, description, path) so the router can choose.

Searches, in order: the skills folder this repo ships, ~/.claude/skills, and
./.claude/skills in the current project. Duplicates (same name) are shown once.

  python3 catalog.py                 # table
  python3 catalog.py --json          # machine-readable
  python3 catalog.py --grep video    # only skills whose name or description match
"""
import json, os, re, sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOTS = [HERE.parents[3], Path.home() / ".claude" / "skills", Path.cwd() / ".claude" / "skills"]


def frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip()
            out[key] = val.strip("\"'")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def find():
    seen, rows = set(), []
    for root in ROOTS:
        if not root.is_dir():
            continue
        for md in sorted(root.rglob("SKILL.md")):
            if any(p in (".venv", "node_modules", "examples", "templates") for p in md.parts):
                continue
            try:
                fm = frontmatter(md.read_text(errors="ignore"))
            except OSError:
                continue
            name = fm.get("name") or md.parent.name
            if name in seen:
                continue
            seen.add(name)
            rel = md.parent.relative_to(root)
            category = rel.parts[0] if len(rel.parts) > 1 else ""
            rows.append({"name": name, "category": category, "description": fm.get("description", ""), "path": str(md)})
    return rows


def main():
    args = sys.argv[1:]
    rows = find()
    if "--grep" in args:
        q = args[args.index("--grep") + 1].lower()
        rows = [r for r in rows if q in r["name"].lower() or q in r["description"].lower()]
    if "--json" in args:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        cat = f"[{r['category']}] " if r["category"] else ""
        print(f"- {cat}{r['name']}: {r['description']}")
    print(f"\n{len(rows)} skills")


if __name__ == "__main__":
    main()
