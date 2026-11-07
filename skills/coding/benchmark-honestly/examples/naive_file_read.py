#!/usr/bin/env python3
"""The naive way: write a test file, time variant A once, then B once.

A always runs first, so A reads from disk and B reads from the warm page cache.
Then the order is swapped on a brand-new file, and the "winner" flips.

    python3 naive_file_read.py <scratch-dir> 1024
"""
import os
import subprocess
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
tool = os.path.join(here, "file_read.py")
scratch, mib = sys.argv[1], sys.argv[2]


def once(path, buf):
    t = time.perf_counter()
    subprocess.run([sys.executable, tool, "read", path, str(buf)], check=True,
                   stdout=subprocess.DEVNULL)
    return time.perf_counter() - t


for trial, order in enumerate([(65536, 1048576), (1048576, 65536)], 1):
    path = os.path.join(scratch, "naive_%d.bin" % trial)
    subprocess.run([sys.executable, tool, "make", path, mib], check=True)
    times = {buf: once(path, buf) for buf in order}
    a, b = order
    print("trial %d: %4d KiB buffer first: %.3f s, then %4d KiB buffer: %.3f s  -> "
          "naive verdict: %d KiB buffer is %.1fx faster" % (
              trial, a // 1024, times[a], b // 1024, times[b],
              (min(times, key=times.get)) // 1024, max(times.values()) / min(times.values())))
    os.remove(path)
