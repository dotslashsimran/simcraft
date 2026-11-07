#!/usr/bin/env python3
"""Run a test command under stressors that expose hidden flakiness causes.

Single run with chosen stressors:
  stress.py --tz Pacific/Kiritimati --clock month-end -- npm test
  stress.py --cpu 8 --nofile 256 --tmp fresh -- python3 -m pytest -q
  stress.py --seed 1234 -- go test ./... -count=1 -shuffle={seed}

Sweep (the fast way to classify a flake): runs a baseline, then each
stressor on its own K times, and prints which ones raise the failure rate.
  stress.py --sweep -k 5 -- python3 -m unittest -q
  stress.py --sweep -k 10 --only tz,clock -- node --test

Stressors:
  --tz ZONE         set TZ (try UTC, Pacific/Kiritimati +14, Pacific/Pago_Pago -11,
                    Asia/Kolkata +5:30, America/New_York for DST)
  --clock WHEN      move the wall clock: an ISO local time like 2026-01-31T23:59:50,
                    or a preset (see --list-clocks). Python via a sitecustomize shim,
                    Node via a --require shim, anything else via libfaketime if found.
  --freeze          with --clock, stop the clock instead of letting it tick
  --cpu N           start N busy-loop processes during the run (0 = one per core)
  --nofile N        lower the open-files limit (finds leaks of files and sockets)
  --tmp fresh|DIR   point TMPDIR/TEMP/TMP at a brand new dir, or at a shared one
  --locale L        set LC_ALL and LANG (try tr_TR.UTF-8, de_DE.UTF-8, C)
  --seed S          sets PYTHONHASHSEED, FLAKE_SEED, and replaces {seed} in the command
  --env K=V         any extra environment variable

Python 3.9+ standard library only. macOS and Linux.
"""
import argparse
import datetime as dt
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flake_rate import fisher_one_sided  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PY_SHIM = os.path.join(HERE, "shims", "python")
NODE_SHIM = os.path.join(HERE, "shims", "node", "clock_shim.cjs")


def next_year():
    return dt.date.today().year + 1


def clock_presets():
    y = next_year()
    return {
        # name: (local ISO time, timezone to force or None)
        "midnight": ("%d-06-15T23:59:55" % y, None),
        "after-midnight": ("%d-06-16T00:00:30" % y, None),
        "month-end": ("%d-01-31T23:59:50" % y, None),
        "feb-28": ("%d-02-28T12:00:00" % y, None),
        "leap-day": ("2028-02-29T12:00:00", None),
        "year-end": ("%d-12-31T23:59:50" % y, None),
        "dst-spring": ("2027-03-14T01:59:50", "America/New_York"),
        "dst-fall": ("2027-11-07T01:59:50", "America/New_York"),
        "month-30th": ("%d-04-30T10:00:00" % y, None),
        "y2038": ("2038-01-19T03:14:00", "UTC"),
    }


def to_epoch(iso, tz):
    naive = dt.datetime.fromisoformat(iso)
    if naive.tzinfo is not None:
        return naive.timestamp()
    if tz:
        from zoneinfo import ZoneInfo
        return naive.replace(tzinfo=ZoneInfo(tz)).timestamp()
    return time.mktime(naive.timetuple())  # local zone of this machine


class Stress:
    """Holds one stressor configuration and runs a command under it."""

    def __init__(self, tz=None, clock=None, freeze=False, cpu=None, nofile=None,
                 tmp=None, locale=None, seed=None, env=None, backend="auto"):
        self.tz, self.clock, self.freeze = tz, clock, freeze
        self.cpu, self.nofile, self.tmp = cpu, nofile, tmp
        self.locale, self.seed, self.env = locale, seed, env or []
        self.backend = backend

    def describe(self):
        parts = []
        for k in ("tz", "clock", "cpu", "nofile", "tmp", "locale", "seed"):
            v = getattr(self, k)
            if v is not None:
                parts.append("%s=%s" % (k, v))
        if self.freeze:
            parts.append("frozen")
        parts += self.env
        return " ".join(parts) or "baseline"

    def prepare(self, cmd):
        env = dict(os.environ)
        cmd = list(cmd)
        cleanup = []
        tz = self.tz
        if self.clock:
            presets = clock_presets()
            iso = self.clock
            if iso in presets:
                iso, forced_tz = presets[iso]
                tz = tz or forced_tz
            epoch = to_epoch(iso, tz)
            env["FLAKE_FAKE_NOW_EPOCH"] = repr(epoch)
            if self.freeze:
                env["FLAKE_FAKE_FREEZE"] = "1"
            faketime = shutil.which("faketime")
            if self.backend == "faketime" or (self.backend == "auto" and faketime and not _is_py_or_node(cmd)):
                if not faketime:
                    raise SystemExit("faketime not installed (brew install libfaketime / apt install faketime)")
                stamp = dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
                cmd = [faketime, ("" if self.freeze else "@") + stamp] + cmd
            else:
                env["PYTHONPATH"] = PY_SHIM + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
                env["NODE_OPTIONS"] = ("--require " + _q(NODE_SHIM) + " " + env.get("NODE_OPTIONS", "")).strip()
        if tz:
            env["TZ"] = tz
        if self.locale:
            env["LC_ALL"] = env["LANG"] = self.locale
        if self.seed is not None:
            env["PYTHONHASHSEED"] = str(self.seed)
            env["FLAKE_SEED"] = str(self.seed)
            cmd = [a.replace("{seed}", str(self.seed)) for a in cmd]
        if self.tmp:
            if self.tmp == "fresh":
                d = tempfile.mkdtemp(prefix="flake-tmp-")
                cleanup.append(lambda d=d: shutil.rmtree(d, ignore_errors=True))
            else:
                d = os.path.abspath(self.tmp)
                os.makedirs(d, exist_ok=True)
            for k in ("TMPDIR", "TEMP", "TMP"):
                env[k] = d
        for kv in self.env:
            k, _, v = kv.partition("=")
            env[k] = v
        return cmd, env, cleanup

    def run(self, cmd, cwd=None, timeout=None, capture=False):
        cmd, env, cleanup = self.prepare(cmd)
        if any("{seed}" in a for a in cmd):
            s = random.randrange(1, 2**31)
            cmd = [a.replace("{seed}", str(s)) for a in cmd]
            env["FLAKE_SEED"] = str(s)
        loaders = []
        n = self.cpu
        if n is not None:
            n = n or os.cpu_count() or 2
            for _ in range(n):
                loaders.append(subprocess.Popen([sys.executable, "-c", "while True: pass"]))
        nofile = self.nofile

        def limits():
            if nofile:
                import resource
                soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                resource.setrlimit(resource.RLIMIT_NOFILE, (min(nofile, hard), hard))

        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, cwd=cwd, env=env, preexec_fn=limits, timeout=timeout,
                               stdout=subprocess.PIPE if capture else None,
                               stderr=subprocess.STDOUT if capture else None)
            code, out = p.returncode, (p.stdout or b"").decode("utf-8", "replace")
        except subprocess.TimeoutExpired as e:
            code, out = 124, (e.stdout or b"").decode("utf-8", "replace") + "\n[stress] Timeout"
        finally:
            for l in loaders:
                l.kill()
            for l in loaders:
                l.wait()
            for c in cleanup:
                c()
        return code, out, time.monotonic() - t0


def _q(path):
    return '"%s"' % path if " " in path else path


def _is_py_or_node(cmd):
    exe = os.path.basename(cmd[0]) if cmd else ""
    return bool(re.match(r"(python[\d.]*|node|npm|npx|pnpm|yarn|pytest|jest|vitest|bun)$", exe))


def first_error(out):
    lines = out.splitlines()
    for rx in (r"^\s*(FAIL|ERROR):|^\s*--- FAIL|^\s*not ok|^\s*✖|panic:|FAILED",
               r"[A-Za-z]*Error\b|[Ee]xpected|assert"):
        for line in lines:
            if re.search(rx, line):
                return line.strip()[:140]
    lines = [l for l in out.strip().splitlines() if l.strip()]
    return lines[-1][:140] if lines else ""


def sweep(args):
    cmd = args.cmd
    only = set(args.only.split(",")) if args.only else None
    # flags given next to --sweep (--tz, --env, --locale, --seed, --clock) apply to every config,
    # so you can pin one cause (say --tz UTC) and sweep for the next one
    base = dict(tz=args.tz, clock=args.clock, locale=args.locale, seed=args.seed,
                env=args.env, backend=args.clock_backend)

    def mk(**kw):
        d = dict(base)
        d.update(kw)
        return Stress(**d)

    configs = [("baseline", mk())]

    def want(group):
        return only is None or group in only

    if want("tz"):
        for tz in ("UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago", "Asia/Kolkata", "America/New_York"):
            configs.append(("tz", mk(tz=tz)))
    if want("clock"):
        for c in ("midnight", "after-midnight", "month-end", "feb-28", "leap-day", "year-end", "dst-spring", "dst-fall"):
            configs.append(("clock", mk(clock=c)))
    if want("cpu"):
        configs.append(("cpu", mk(cpu=(os.cpu_count() or 2) * 2)))
    if want("tmp"):
        configs.append(("tmp", mk(tmp="fresh")))
    if want("nofile"):
        configs.append(("nofile", mk(nofile=64)))
    if want("locale"):
        configs.append(("locale", mk(locale="tr_TR.UTF-8")))

    k = args.k
    print("sweep: %d configs x %d runs. command: %s\n" % (len(configs), k, " ".join(cmd)))
    rows = []
    base_rate = None
    for group, st in configs:
        fails, sigs, seeds = 0, {}, []
        for _ in range(k):
            code, out, _ = st.run(cmd, cwd=args.cwd, timeout=args.timeout, capture=True)
            if code != 0:
                fails += 1
                s = first_error(out)
                sigs[s] = sigs.get(s, 0) + 1
                m = re.search(r"seed[=: ]+(\d+)", out)
                if m:
                    seeds.append(m.group(1))
            sys.stderr.write("." if code == 0 else "F"); sys.stderr.flush()
        sys.stderr.write("\n")
        rate = fails / k
        if group == "baseline":
            base_rate = rate
        rows.append((group, st.describe(), fails, rate, sigs, seeds))

    base_fails = rows[0][2]
    # a stressor counts only if a one-sided Fisher exact test says p < 0.05
    raises = lambda f: fisher_one_sided(f, k, base_fails, k) < 0.05
    lowers = lambda f: fisher_one_sided(base_fails, k, f, k) < 0.05
    print("\n%-8s %-34s %8s  %s" % ("group", "config", "fails", "first error line"))
    for group, desc, fails, rate, sigs, seeds in rows:
        top = max(sigs.items(), key=lambda kv: kv[1])[0] if sigs else ""
        flag = ""
        if group != "baseline" and raises(fails):
            flag = "  <== raises failures"
        elif group != "baseline" and lowers(fails):
            flag = "  <== lowers failures"
        print("%-8s %-34s %3d/%-4d %s%s" % (group, desc[:34], fails, k, top[:70], flag))
    print()
    raised = {g for g, d, f, r, s, sd in rows if g != "baseline" and raises(f)}
    lowered = [d for g, d, f, r, s, sd in rows if g != "baseline" and lowers(f)]
    if base_rate == 1.0:
        print("baseline always fails: this is a plain failure in this environment, not a flake.")
        if lowered:
            print("it passes under: " + ", ".join(lowered) + ". The test depends on that setting.")
        top = lambda sigs: max(sigs.items(), key=lambda kv: kv[1])[0] if sigs else ""
        base_top = re.sub(r"\(.*?\)$", "", top(rows[0][4]))
        differ = [d for g, d, f, r, sg, sd in rows[1:] if sg and re.sub(r"\(.*?\)$", "", top(sg)) != base_top]
        if differ:
            print("it fails DIFFERENTLY under: " + ", ".join(differ) + ". That is a second, hidden bug;")
            print("pin the first cause (e.g. --tz UTC next to --sweep) and sweep again.")
    explain = {
        "tz": "timezone dependence: code or test uses local time where it means UTC, or the reverse",
        "clock": "date and clock dependence: test uses 'now' and breaks at month or year ends, DST or midnight",
        "cpu": "timing dependence: sleeps, short timeouts or races that only lose when the machine is busy",
        "tmp": "filesystem dependence: test expects files left by earlier runs, or a fixed temp path",
        "nofile": "resource leak: files or sockets are not closed",
        "locale": "locale dependence: string case, number or date formatting depends on the locale",
    }
    for g in ("tz", "clock", "cpu", "tmp", "nofile", "locale"):
        if g in raised:
            print("likely cause: " + explain[g])
    if not raised and base_rate and 0 < base_rate < 1:
        print("no single stressor moved the rate. Next: run alone vs in suite, shuffle order")
        print("(find_polluter.py), and run in parallel (flake_rate.py -j) to find shared resources.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tz")
    ap.add_argument("--clock")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--clock-backend", default="auto", choices=["auto", "shim", "faketime"])
    ap.add_argument("--cpu", type=int)
    ap.add_argument("--nofile", type=int)
    ap.add_argument("--tmp")
    ap.add_argument("--locale")
    ap.add_argument("--seed")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--cwd")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("-k", type=int, default=5, help="runs per config in --sweep")
    ap.add_argument("--only", help="comma list of sweep groups: tz,clock,cpu,tmp,nofile,locale")
    ap.add_argument("--list-clocks", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if args.list_clocks:
        for name, (iso, tz) in clock_presets().items():
            print("%-11s %s%s" % (name, iso, "  (TZ=%s)" % tz if tz else ""))
        return 0
    if args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    if not args.cmd:
        ap.error("give a command after --")
    if args.sweep:
        return sweep(args)
    st = Stress(tz=args.tz, clock=args.clock, freeze=args.freeze, cpu=args.cpu, nofile=args.nofile,
                tmp=args.tmp, locale=args.locale, seed=args.seed, env=args.env, backend=args.clock_backend)
    sys.stderr.write("[stress] %s\n" % st.describe())
    code, _, secs = st.run(args.cmd, cwd=args.cwd, timeout=args.timeout)
    sys.stderr.write("[stress] exit %s after %.1fs\n" % (code, secs))
    return code


if __name__ == "__main__":
    sys.exit(main())
