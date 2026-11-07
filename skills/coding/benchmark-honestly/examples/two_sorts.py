#!/usr/bin/env python3
"""Two ways to get a sorted copy of 300,000 floats. They do the same work.

    python3 two_sorts.py sorted     # new = sorted(data)
    python3 two_sorts.py copysort   # new = data[:]; new.sort()
    python3 two_sorts.py sorted-x4  # positive control: sorted() 4 times instead of 3
"""
import random
import sys

rng = random.Random(7)
data = [rng.random() for _ in range(300_000)]
for _ in range(4 if sys.argv[1] == "sorted-x4" else 3):
    if sys.argv[1] in ("sorted", "sorted-x4"):
        new = sorted(data)
    else:
        new = data[:]
        new.sort()
print(new[0], new[-1])
