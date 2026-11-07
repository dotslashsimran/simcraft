#!/usr/bin/env python3
"""The naive way: run each variant once, compare, repeat the whole thing 5 times.

Each line is what someone would post as "copysort is X% faster" or the reverse.
"""
import os
import subprocess
import sys
import time

prog = os.path.join(os.path.dirname(os.path.abspath(__file__)), "two_sorts.py")


def once(mode):
    t = time.perf_counter()
    subprocess.run([sys.executable, prog, mode], check=True, stdout=subprocess.DEVNULL)
    return time.perf_counter() - t


for attempt in range(1, 6):
    a, b = once("sorted"), once("copysort")
    faster, ratio = ("copysort", a / b) if b < a else ("sorted", b / a)
    print("attempt %d: sorted %.1f ms, copysort %.1f ms -> naive verdict: %s is %.0f%% faster" % (
        attempt, a * 1e3, b * 1e3, faster, 100 * (ratio - 1)))
