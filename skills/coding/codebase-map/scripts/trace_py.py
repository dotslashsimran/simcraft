#!/usr/bin/env python3
"""Run a real Python command and print the path it takes through YOUR code: every function in the repo that
was called, in first-call order, indented by call depth, with file:line. Library and stdlib frames are
hidden, so the output reads like the hops of a request trace.

Usage:
  python3 trace_py.py --repo <repo> [--depth 8] [--max 200] [--out trace.txt] -- <script.py | -m module> [args...]

Example:
  python3 trace_py.py --repo . -- -m myapp.cli sync --dry-run
  python3 trace_py.py --repo . -- manage.py runserver --noreload     (stop it with Ctrl+C; the trace prints on exit)

Standard library only (Python 3.9+). It runs the target in this same interpreter, so run it from the
project's own virtual env (e.g. `.venv/bin/python trace_py.py ...`) if the project has dependencies.
For web apps, start the server under this script, send one real request, then stop the server.
"""
import argparse
import os
import runpy
import sys
import threading


def main():
    if "--" not in sys.argv:
        sys.exit(__doc__)
    split = sys.argv.index("--")
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--depth", type=int, default=8, help="hide calls nested deeper than this (repo frames only)")
    ap.add_argument("--max", type=int, default=200, help="stop recording after this many distinct calls")
    ap.add_argument("--out")
    a = ap.parse_args(sys.argv[1:split])
    target = sys.argv[split + 1:]
    if not target:
        sys.exit("nothing to run after --")

    repo = os.path.realpath(a.repo) + os.sep
    skip = (os.path.realpath(__file__),)
    seen, lines = set(), []
    depth = [0]
    stack = []

    def mine(code):
        if code.co_filename.startswith("<") or (code.co_name.startswith("<") and code.co_name != "<module>"):
            return False  # frozen import machinery, comprehensions and lambdas are noise
        f = os.path.realpath(code.co_filename)
        return f.startswith(repo) and f not in skip and "site-packages" not in f and "/.venv/" not in f

    def prof(frame, event, arg):
        if event == "call":
            code = frame.f_code
            ok = mine(code)
            stack.append(ok)
            if ok:
                depth[0] += 1
                key = (code.co_filename, code.co_firstlineno, depth[0])
                if key not in seen and depth[0] <= a.depth and len(lines) < a.max:
                    seen.add(key)
                    rel = os.path.relpath(code.co_filename, repo)
                    lines.append(f"{'  ' * (depth[0] - 1)}{code.co_name}  {rel}:{code.co_firstlineno}")
        elif event == "return" and stack:
            if stack.pop():
                depth[0] -= 1

    sys.argv = target
    if target[0] == "-m":
        sys.argv = [target[1]] + target[2:]
    sys.path.insert(0, os.getcwd())
    threading.setprofile(prof)
    sys.setprofile(prof)
    code = 0
    try:
        if target[0] == "-m":
            runpy.run_module(target[1], run_name="__main__", alter_sys=True)
        else:
            sys.path[0] = os.path.dirname(os.path.realpath(target[0]))
            runpy.run_path(target[0], run_name="__main__")
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    except KeyboardInterrupt:
        pass
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
        report = "\n".join(["# Call path through repo code (first call of each function, by depth)", ""] + lines)
        if len(lines) >= a.max:
            report += f"\n... stopped after {a.max} calls (raise --max)"
        if a.out:
            with open(a.out, "w") as fh:
                fh.write(report + "\n")
            print(f"\ntrace written to {a.out}", file=sys.stderr)
        else:
            print("\n" + report, file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
