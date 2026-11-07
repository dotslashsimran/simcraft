#!/usr/bin/env python3
"""Build a throwaway git repo with known regressions hidden in its history.

  python3 make_demo_repo.py /tmp/demo-repo

About 40 commits of a tiny Python stats app, including:
  - "Tidy stats helpers": a wrong median for even-length lists, mixed in with
    harmless hunks (docstring, a new helper, a README line). It sits on a
    feature branch that is merged with a merge commit.
  - "Speed up dedupe bookkeeping": makes dedupe() quadratic (a slowdown).
  - "Refresh badge colours": changes the rendered badge.png (a visual change).
    An earlier commit changes the colour by one step, which a screenshot diff
    with a tolerance should ignore.
  - three commits that do not build (syntax errors), one right after the bug.
  - dependency lockfile changes at three points, to exercise install caching.
  - every ordinary commit bumps the version, which the CLI prints, so a naive
    golden-output diff would blame every commit.

Dates and authors are fixed, so the commit hashes are the same on every run.
Prints a JSON map of scenario -> commit subject.
"""
import json
import os
import subprocess
import sys

MATHLIB = '''"""Small statistics helpers."""


def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def dedupe(xs):
    seen = set()
    out = []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def spread(xs):
    return max(xs) - min(xs)
'''

CLI = '''import sys
import time

import mathlib
from version import VERSION

xs = [float(v) for v in open(sys.argv[1]).read().split()]
print("stats report, generated at %s" % time.strftime("%H:%M:%S"))
print("version %s" % VERSION)
print("count  %d" % len(xs))
print("mean   %.2f" % mathlib.mean(xs))
print("median %.2f" % mathlib.median(xs))
print("spread %.2f" % mathlib.spread(xs))
'''

BUILD = '''import os
import py_compile
import sys

for name in sorted(os.listdir(".")):
    if name.endswith(".py"):
        py_compile.compile(name, doraise=True)
want = open("requirements.lock").read()
try:
    have = open(".deps/installed.txt").read()
except OSError:
    have = None
if have != want:
    sys.exit("dependencies are out of date, run install.py")
print("build ok")
'''

INSTALL = '''import os

os.makedirs(".deps", exist_ok=True)
data = open("requirements.lock").read()
open(".deps/installed.txt", "w").write(data)
log = os.environ.get("DEMO_INSTALL_LOG")
if log:
    open(log, "a").write("install\\n")
print("installed")
'''

BENCH = '''import mathlib

xs = list(range(6000)) * 2
assert len(mathlib.dedupe(xs)) == 6000
'''

SERVER = '''import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import mathlib


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/median":
            self.send_response(404)
            self.end_headers()
            return
        xs = [float(v) for v in parse_qs(u.query)["xs"][0].split(",")]
        body = json.dumps({"median": mathlib.median(xs)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8000)
HTTPServer(("127.0.0.1", ap.parse_args().port), Handler).serve_forever()
'''

RENDER = '''import struct
import sys
import zlib

from theme import BADGE, TEXT

W, H = 48, 16
rows = []
for y in range(H):
    row = bytearray()
    for x in range(W):
        on_text = 4 <= y < 12 and 6 <= x < 42 and (x // 3 + y) % 4 == 0
        row += bytes(TEXT if on_text else BADGE)
    rows.append(b"\\x00" + bytes(row))


def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)


png = (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
open(sys.argv[1] if len(sys.argv) > 1 else "badge.png", "wb").write(png)
'''


class Repo:
    def __init__(self, path):
        self.path = path
        self.t = 1700000000
        self.version = 0

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="Demo Dev", GIT_AUTHOR_EMAIL="dev@example.com",
                   GIT_COMMITTER_NAME="Demo Dev", GIT_COMMITTER_EMAIL="dev@example.com",
                   GIT_AUTHOR_DATE="%d +0000" % self.t, GIT_COMMITTER_DATE="%d +0000" % self.t,
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git"] + list(args), cwd=self.path, env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def write(self, name, text):
        full = os.path.join(self.path, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)

    def read(self, name):
        with open(os.path.join(self.path, name)) as fh:
            return fh.read()

    def replace(self, name, old, new):
        text = self.read(name)
        assert old in text, (name, old)
        self.write(name, text.replace(old, new, 1))

    def commit(self, msg):
        self.t += 3600
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def filler(self, msg):
        self.version += 1
        self.write("version.py", 'VERSION = "0.1.%d"\n' % self.version)
        self.write("notes/%02d.md" % self.version, "# %s\n" % msg)
        self.commit(msg)

    def broken(self, msg, fix_msg):
        self.write("extra.py", "def broken(:\n    pass\n")
        self.commit(msg)
        self.write("extra.py", "def fine():\n    pass\n")
        self.commit(fix_msg)


def build(path):
    os.makedirs(path)
    r = Repo(path)
    r.git("init", "-q", "-b", "main")
    r.write(".gitignore", ".deps/\n__pycache__/\nbadge.png\n")
    r.write("README.md", "# stats demo\n\nA tiny stats app used to practise bisecting.\n")
    r.write("mathlib.py", MATHLIB)
    r.write("cli.py", CLI)
    r.write("version.py", 'VERSION = "0.1.0"\n')
    r.write("data.txt", "3 9 4 12 7 1\n")
    r.write("build.py", BUILD)
    r.write("install.py", INSTALL)
    r.write("requirements.lock", "numberz==1.0.0\n")
    r.write("bench.py", BENCH)
    r.write("server.py", SERVER)
    r.write("render.py", RENDER)
    r.write("theme.py", "BADGE = (46, 160, 67)\nTEXT = (255, 255, 255)\n")
    r.commit("Initial stats app")

    r.filler("Add notes on usage")
    r.replace("theme.py", "(46, 160, 67)", "(46, 160, 68)")
    r.commit("Nudge badge green for contrast")
    r.broken("Start extra module", "Fix extra module syntax")
    r.filler("Document the CLI")
    r.write("requirements.lock", "numberz==1.1.0\n")
    r.commit("Bump numberz to 1.1.0")
    r.filler("Explain the bench script")
    r.filler("Note server usage")

    r.git("checkout", "-q", "-b", "stats-cleanup")
    r.filler("Plan stats cleanup")
    r.filler("List stats edge cases")
    # The bug commit: several harmless hunks plus one real bug.
    r.replace("mathlib.py", '"""Small statistics helpers."""',
              '"""Small statistics helpers.\n\nAll functions take a non-empty list of numbers.\n"""')
    r.replace("mathlib.py", "    return (s[mid - 1] + s[mid]) / 2\n", "    return s[mid]\n")
    r.write("mathlib.py", r.read("mathlib.py") + "\n\ndef mode(xs):\n    return max(set(xs), key=xs.count)\n")
    r.replace("README.md", "practise bisecting.\n", "practise bisecting.\n\nSee mathlib.py for the helpers.\n")
    r.commit("Tidy stats helpers")
    r.broken("Sketch stats extras", "Repair stats extras")
    r.filler("Finish stats cleanup notes")

    r.git("checkout", "-q", "main")
    r.filler("Fix typo in notes")
    r.filler("Add contributor notes")
    r.t += 3600
    r.git("merge", "-q", "--no-ff", "-X", "ours", "-m", "Merge branch 'stats-cleanup'", "stats-cleanup")

    r.replace("theme.py", "(46, 160, 68)", "(200, 50, 50)")
    r.commit("Refresh badge colours")
    for m in ("Clarify install steps", "Add FAQ", "Note data format", "Mention render script"):
        r.filler(m)
    r.write("requirements.lock", "numberz==2.0.0\n")
    r.commit("Bump numberz to 2.0.0")
    r.filler("Reword FAQ")
    r.filler("Add changelog")
    r.broken("Prototype export", "Fix export prototype")
    r.filler("Note export plans")
    r.replace("mathlib.py", "    seen = set()\n", "    seen = []\n")
    r.replace("mathlib.py", "            seen.add(x)\n", "            seen.append(x)\n")
    r.commit("Speed up dedupe bookkeeping")
    for i in range(10):
        r.filler("Polish docs part %d" % (i + 1))
    r.git("branch", "-D", "stats-cleanup")
    return {
        "bug": "Tidy stats helpers",
        "bug_first_parent": "Merge branch 'stats-cleanup'",
        "slowdown": "Speed up dedupe bookkeeping",
        "visual": "Refresh badge colours",
        "unbuildable": ["Start extra module", "Sketch stats extras", "Prototype export"],
        "root": "Initial stats app",
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    if os.path.exists(sys.argv[1]):
        sys.exit("make_demo_repo.py: %s already exists" % sys.argv[1])
    print(json.dumps(build(sys.argv[1]), indent=1))
