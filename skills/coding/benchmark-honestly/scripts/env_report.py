#!/usr/bin/env python3
"""Capture the machine and environment details a benchmark report must disclose.

Standard library only (Python 3.9+). Works on macOS and Linux, partly on others.

    python3 env_report.py                      # markdown to stdout
    python3 env_report.py --json env.json      # also write JSON
    python3 env_report.py --redact             # hide hostname and username
    python3 env_report.py --tool "node --version" --tool "go version"

It prints WARNINGS for things that make numbers untrustworthy: running on
battery, low power mode, high load, other busy processes, a powersave CPU
governor, thermal warnings, and a debug build of Python.
"""
import argparse
import getpass
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import sysconfig
import time


def _run(cmd, timeout=5):
    """Run a command and return stripped stdout, or None if it fails."""
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=timeout, check=False)
        return out.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return None


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _sysctl(name):
    return _run(["sysctl", "-n", name])


def cpu_info():
    info = {"machine": platform.machine(), "logical_cores": os.cpu_count()}
    system = platform.system()
    if system == "Darwin":
        info["model"] = _sysctl("machdep.cpu.brand_string")
        info["physical_cores"] = _int(_sysctl("hw.physicalcpu"))
        # Apple Silicon has performance and efficiency cores.
        levels = []
        for i in range(4):
            name = _sysctl("hw.perflevel%d.name" % i)
            count = _sysctl("hw.perflevel%d.physicalcpu" % i)
            if not name or "unknown" in name.lower():
                break
            levels.append("%s %s" % (count, name))
        if levels:
            info["core_types"] = ", ".join(levels)
        freq = _sysctl("hw.cpufrequency_max")
        if freq and freq.isdigit():
            info["max_freq_mhz"] = int(freq) // 1_000_000
    elif system == "Linux":
        text = _read("/proc/cpuinfo") or ""
        m = re.search(r"^model name\s*:\s*(.+)$", text, re.M)
        if not m:
            m = re.search(r"^(?:Model|Hardware)\s*:\s*(.+)$", text, re.M)
        info["model"] = m.group(1).strip() if m else None
        cores = set(re.findall(r"^core id\s*:\s*(\d+)", text, re.M))
        sockets = set(re.findall(r"^physical id\s*:\s*(\d+)", text, re.M))
        if cores:
            info["physical_cores"] = len(cores) * max(1, len(sockets))
        gov = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        if gov:
            info["governor"] = gov
        no_turbo = _read("/sys/devices/system/cpu/intel_pstate/no_turbo")
        boost = _read("/sys/devices/system/cpu/cpufreq/boost")
        if no_turbo is not None:
            info["turbo"] = "off" if no_turbo == "1" else "on"
        elif boost is not None:
            info["turbo"] = "on" if boost == "1" else "off"
        smt = _read("/sys/devices/system/cpu/smt/active")
        if smt is not None:
            info["smt"] = "on" if smt == "1" else "off"
        try:
            info["affinity"] = len(os.sched_getaffinity(0))
        except Exception:
            pass
        isolated = _read("/sys/devices/system/cpu/isolated")
        if isolated:
            info["isolated_cpus"] = isolated
    else:
        info["model"] = platform.processor() or None
    return info


def _int(s):
    try:
        return int(s)
    except Exception:
        return None


def memory_info():
    system = platform.system()
    if system == "Darwin":
        total = _int(_sysctl("hw.memsize"))
        pressure = _run(["memory_pressure"], timeout=5)
        free_pct = None
        if pressure:
            m = re.search(r"System-wide memory free percentage:\s*(\d+)%", pressure)
            if m:
                free_pct = int(m.group(1))
        return {"total_gb": round(total / 2**30, 1) if total else None, "free_percent": free_pct}
    if system == "Linux":
        text = _read("/proc/meminfo") or ""
        vals = dict((k, int(v)) for k, v in re.findall(r"^(\w+):\s+(\d+) kB", text, re.M))
        total = vals.get("MemTotal")
        avail = vals.get("MemAvailable")
        return {"total_gb": round(total / 2**20, 1) if total else None,
                "free_percent": round(100 * avail / total) if total and avail else None}
    return {}


def power_info():
    """Power source, low power mode and thermal state. Best effort."""
    info = {}
    system = platform.system()
    if system == "Darwin":
        batt = _run(["pmset", "-g", "batt"]) or ""
        if "AC Power" in batt:
            info["source"] = "AC power"
        elif "Battery Power" in batt:
            info["source"] = "battery"
        settings = _run(["pmset", "-g"]) or ""
        m = re.search(r"lowpowermode\s+(\d)", settings)
        if m:
            info["low_power_mode"] = m.group(1) == "1"
        therm = _run(["pmset", "-g", "therm"]) or ""
        if "No thermal warning level has been recorded" in therm:
            info["thermal"] = "no warning recorded"
        elif therm:
            m = re.search(r"(?:thermal|CPU_Speed_Limit)\D*(\d+)", therm, re.I)
            info["thermal"] = therm.splitlines()[-1].strip() if not m else "warning: %s" % m.group(0)
    elif system == "Linux":
        for supply in ("AC", "AC0", "ADP1", "ACAD"):
            online = _read("/sys/class/power_supply/%s/online" % supply)
            if online is not None:
                info["source"] = "AC power" if online == "1" else "battery"
                break
        temps = []
        base = "/sys/class/thermal"
        try:
            for zone in sorted(os.listdir(base)):
                t = _read(os.path.join(base, zone, "temp"))
                if t and t.lstrip("-").isdigit():
                    temps.append(int(t) / 1000)
        except Exception:
            pass
        if temps:
            info["max_zone_temp_c"] = max(temps)
    return info


def load_info():
    info = {}
    try:
        info["loadavg_1_5_15"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        pass
    # Busiest processes, by command name only (no paths, no arguments).
    out = _run(["ps", "-Ao", "pcpu=,comm="])
    busy = []
    if out:
        me = os.getpid()
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            try:
                pct = float(parts[0])
            except ValueError:
                continue
            name = os.path.basename(parts[1].strip())
            if pct >= 5.0 and name not in ("ps",):
                busy.append((pct, name))
        busy.sort(reverse=True)
    info["busy_processes"] = ["%s (%.0f%%)" % (n, p) for p, n in busy[:8]]
    return info


def os_info():
    system = platform.system()
    info = {"system": system, "kernel": platform.release()}
    if system == "Darwin":
        info["version"] = "macOS " + (platform.mac_ver()[0] or "?")
    elif system == "Linux":
        text = _read("/etc/os-release") or ""
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.M)
        info["version"] = m.group(1) if m else "Linux"
        aslr = _read("/proc/sys/kernel/randomize_va_space")
        if aslr is not None:
            info["aslr"] = aslr
        virt = _run(["systemd-detect-virt"])
        if virt and virt != "none":
            info["virtualization"] = virt
    else:
        info["version"] = platform.platform()
    if os.path.exists("/.dockerenv"):
        info["container"] = "docker"
    return info


def python_info():
    return {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "compiler": platform.python_compiler(),
        "debug_build": bool(sysconfig.get_config_var("Py_DEBUG")),
        "optimized_flags": "-O" in (sysconfig.get_config_var("CFLAGS") or "") or
                           "-O2" in (sysconfig.get_config_var("OPT") or "") or
                           "-O3" in (sysconfig.get_config_var("OPT") or ""),
    }


def tool_versions(tools):
    out = {}
    for t in tools:
        v = _run(shlex.split(t), timeout=10)
        out[t] = (v.splitlines()[0] if v else "not available")
    return out


def collect(tools=(), redact=False):
    host = socket.gethostname()
    try:
        user = getpass.getuser()
    except Exception:
        user = "?"
    report = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "host": "<redacted>" if redact else host,
        "user": "<redacted>" if redact else user,
        "os": os_info(),
        "cpu": cpu_info(),
        "memory": memory_info(),
        "power": power_info(),
        "load": load_info(),
        "python": python_info(),
        "tools": tool_versions(tools),
    }
    if redact:
        # Process names can reveal what else the person runs; keep only the count.
        n = len(report["load"]["busy_processes"])
        report["load"]["busy_processes"] = ["<redacted>"] * n
    report["warnings"] = warnings_for(report)
    report["cannot_control"] = cannot_control(report)
    return report


def warnings_for(r):
    w = []
    cores = r["cpu"].get("logical_cores") or 1
    load = r["load"].get("loadavg_1_5_15")
    if load and load[0] > 0.25 * cores:
        w.append("1-minute load average is %.1f on %d logical cores. Other work is competing "
                 "for the CPU; close it or expect wide spread." % (load[0], cores))
    busy = r["load"].get("busy_processes") or []
    if busy and busy[0] == "<redacted>":
        w.append("%d other processes are using 5%% or more CPU right now." % len(busy))
    elif busy:
        w.append("Busy processes right now: %s." % ", ".join(busy[:5]))
    p = r["power"]
    if p.get("source") == "battery":
        w.append("Running on battery. Laptops often lower clock speeds on battery; plug in.")
    if p.get("low_power_mode"):
        w.append("Low power mode is on. Turn it off for benchmarking.")
    if str(p.get("thermal", "")).startswith("warning"):
        w.append("A thermal warning is recorded (%s). The CPU may be throttled." % p["thermal"])
    if p.get("max_zone_temp_c") and p["max_zone_temp_c"] > 85:
        w.append("A thermal zone is at %.0f C. The CPU may be throttling." % p["max_zone_temp_c"])
    gov = r["cpu"].get("governor")
    if gov and gov not in ("performance",):
        w.append("CPU governor is '%s'. For stable numbers use 'performance' "
                 "(sudo cpupower frequency-set -g performance)." % gov)
    mem = r["memory"].get("free_percent")
    if mem is not None and mem < 10:
        w.append("Only %d%% memory is free. Swapping will distort results." % mem)
    if r["python"].get("debug_build"):
        w.append("This Python is a debug build. Its timings say nothing about release builds.")
    if r["os"].get("virtualization") or r["os"].get("container"):
        w.append("Running inside a VM or container. Neighbours on the same host add noise.")
    return w


def cannot_control(r):
    if r["os"]["system"] == "Darwin":
        notes = [
            "macOS has no supported way to pin a process to a core or to disable turbo/boost.",
            "Apple Silicon schedules work on performance or efficiency cores as it sees fit; "
            "a run that lands on efficiency cores is much slower. Keep other load low so "
            "the benchmark stays on performance cores.",
            "Clearing the file cache needs 'sudo purge'. Without it, use F_NOCACHE reads or a "
            "fresh file to measure cold reads.",
        ]
        return notes
    if r["os"]["system"] == "Linux":
        return ["Pin with 'taskset -c N', set the governor to performance, disable turbo, and "
                "drop caches with 'echo 3 | sudo tee /proc/sys/vm/drop_caches' when you need "
                "cold-cache numbers. Note which of these you did."]
    return []


def to_markdown(r):
    cpu, mem, pw, osi, py = r["cpu"], r["memory"], r["power"], r["os"], r["python"]
    rows = [
        ("Captured", r["captured_at"]),
        ("Host / user", "%s / %s" % (r["host"], r["user"])),
        ("OS", "%s (kernel %s)%s" % (osi.get("version"), osi.get("kernel"),
                                    ", " + osi["virtualization"] if osi.get("virtualization") else "")),
        ("CPU", "%s, %s" % (cpu.get("model") or "?", cpu.get("machine"))),
        ("Cores", "%s physical, %s logical%s" % (
            cpu.get("physical_cores", "?"), cpu.get("logical_cores"),
            " (" + cpu["core_types"] + ")" if cpu.get("core_types") else "")),
    ]
    for key, label in (("governor", "CPU governor"), ("turbo", "Turbo/boost"),
                       ("smt", "SMT"), ("affinity", "CPUs allowed"),
                       ("isolated_cpus", "Isolated CPUs"), ("max_freq_mhz", "Max clock (MHz)")):
        if cpu.get(key) is not None:
            rows.append((label, str(cpu[key])))
    rows.append(("Memory", "%s GB total, %s%% free" % (mem.get("total_gb", "?"),
                                                      mem.get("free_percent", "?"))))
    rows.append(("Power", ", ".join("%s: %s" % (k.replace("_", " "), v) for k, v in pw.items())
                 or "unknown"))
    la = r["load"].get("loadavg_1_5_15")
    rows.append(("Load average (1/5/15 min)", " / ".join(str(x) for x in la) if la else "?"))
    rows.append(("Python", "%s %s%s" % (py["implementation"], py["version"],
                                       " (DEBUG BUILD)" if py["debug_build"] else "")))
    for t, v in r["tools"].items():
        rows.append((t.split()[0], v))
    lines = ["| Setting | Value |", "|---|---|"]
    lines += ["| %s | %s |" % (k, str(v).replace("|", "\\|")) for k, v in rows]
    if r["warnings"]:
        lines += ["", "**Environment warnings**", ""] + ["- " + w for w in r["warnings"]]
    if r["cannot_control"]:
        lines += ["", "**Not controlled on this OS**", ""] + ["- " + n for n in r["cannot_control"]]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", help="write the report as JSON to this file")
    ap.add_argument("--md", help="write the markdown table to this file")
    ap.add_argument("--redact", action="store_true", help="hide hostname and username")
    ap.add_argument("--tool", action="append", default=[],
                    help="a version command to record, e.g. --tool 'node --version' (repeatable)")
    args = ap.parse_args()
    r = collect(args.tool, args.redact)
    md = to_markdown(r)
    print(md)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(r, f, indent=2)
    if args.md:
        with open(args.md, "w") as f:
            f.write(md + "\n")


if __name__ == "__main__":
    main()
