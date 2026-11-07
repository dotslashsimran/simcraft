#!/usr/bin/env python3
"""Three ways to "uppercase a million words". Only two of them do it.

    python3 lazy_work.py map-unused   # map() is lazy: nothing is uppercased
    python3 lazy_work.py map-used     # same map, but the result is consumed
    python3 lazy_work.py listcomp     # list comprehension, result consumed

Each mode prints a checksum of what it produced, so bench.py --same-output
can tell when two variants did different work.
"""
import sys

WORDS = ["word%d" % i for i in range(1_000_000)]


def main(mode):
    if mode == "map-unused":
        out = map(str.upper, WORDS)            # lazy iterator; never consumed
        print("built", type(out).__name__)
    elif mode == "map-used":
        out = list(map(str.upper, WORDS))
        print("checksum", sum(len(w) for w in out), out[-1])
    elif mode == "listcomp":
        out = [w.upper() for w in WORDS]
        print("checksum", sum(len(w) for w in out), out[-1])
    elif mode == "empty":                      # same startup and setup, no work
        print("empty")
    else:
        sys.exit("unknown mode")


if __name__ == "__main__":
    main(sys.argv[1])
