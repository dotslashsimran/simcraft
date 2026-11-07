#!/usr/bin/env python3
"""The naive way: timeit the expression and believe the number.

This is what a quick "is map faster than a list comprehension?" check looks
like. It reports map as thousands of times faster because map() returns a lazy
iterator and does no work. It also times "2 ** 64", which CPython folds into a
constant at compile time, so the loop measures nothing.
"""
import timeit

setup = "WORDS = ['word%d' % i for i in range(1_000_000)]"
n = 5
t_map = min(timeit.repeat("map(str.upper, WORDS)", setup, number=n, repeat=3)) / n
t_lc = min(timeit.repeat("[w.upper() for w in WORDS]", setup, number=n, repeat=3)) / n
print("map(str.upper, WORDS)       %10.3f us per call" % (t_map * 1e6))
print("[w.upper() for w in WORDS]  %10.3f us per call" % (t_lc * 1e6))
print("naive conclusion: map is %.0fx faster" % (t_lc / t_map))
print()
t_const = min(timeit.repeat("2 ** 64", number=1_000_000, repeat=5)) / 1_000_000
t_var = min(timeit.repeat("x ** 64", "x = 2", number=1_000_000, repeat=5)) / 1_000_000
print("2 ** 64 (folded at compile time)  %6.1f ns" % (t_const * 1e9))
print("x ** 64 (really computed)         %6.1f ns" % (t_var * 1e9))
print("naive conclusion: big powers cost %.1f ns" % (t_const * 1e9))
