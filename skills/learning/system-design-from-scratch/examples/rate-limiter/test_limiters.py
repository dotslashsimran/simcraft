"""Property tests for the rate limiters.

Each test drives a limiter with a manual clock over many random request
traces (seeded, so failures replay) and checks a property that must hold
for every trace, not just a hand-picked example.

    python3 -m unittest -v test_limiters.py
"""

import random
import unittest
from collections import deque

from limiters import (ALL, GCRA, FixedWindow, ManualClock, SlidingLog,
                      SlidingWindowCounter, TokenBucket)

SEEDS = range(200)


def random_trace(rng, n=400, horizon=5.0):
    """Arrival times mixing a steady stream, tight bursts and quiet gaps."""
    times = []
    t = 0.0
    while len(times) < n and t < horizon:
        mode = rng.random()
        if mode < 0.5:
            t += rng.expovariate(rng.choice([5, 50, 500]))
            times.append(t)
        elif mode < 0.8:
            k = rng.randint(2, 40)
            times.extend([t] * k)       # burst at one instant
        else:
            t += rng.uniform(0.1, 1.5)  # quiet gap
    return times[:n]


def admitted_times(limiter, clock, times, key="k"):
    out = []
    for t in times:
        clock.t = t
        if limiter.allow(key):
            out.append(t)
    return out


def max_in_any_window(times, window):
    """Largest number of timestamps inside any half-open window (t - W, t]."""
    q, best = deque(), 0
    for t in times:
        q.append(t)
        while q[0] <= t - window:
            q.popleft()
        best = max(best, len(q))
    return best


class TokenBucketProperties(unittest.TestCase):
    def test_never_exceeds_burst_plus_rate_times_interval(self):
        # For admitted times t_i <= t_j: (j - i + 1) <= burst + rate * (t_j - t_i).
        # Rearranged: (j + 1 - rate*t_j) + max_i(rate*t_i - i) <= burst, one pass.
        for seed in SEEDS:
            rng = random.Random(seed)
            rate, burst = rng.choice([1, 10, 100]), rng.choice([1, 5, 20])
            clock = ManualClock()
            adm = admitted_times(TokenBucket(rate, burst, clock), clock, random_trace(rng))
            best = float("-inf")
            for j, tj in enumerate(adm):
                best = max(best, rate * tj - j)
                self.assertLessEqual(j + 1 - rate * tj + best, burst + 1e-6, "seed %d" % seed)

    def test_long_run_rate_converges_under_saturation(self):
        # Offer 10x the rate for 100 s: admitted must be rate*T + burst, give or take one.
        clock = ManualClock()
        adm = admitted_times(TokenBucket(50, 10, clock), clock, [i / 500.0 for i in range(50000)])
        self.assertAlmostEqual(len(adm), 50 * 100 + 10, delta=1)

    def test_gcra_makes_identical_decisions(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            rate, burst = rng.choice([1, 10, 100]), rng.choice([1, 5, 20])
            trace = random_trace(rng)
            c1, c2 = ManualClock(), ManualClock()
            a = admitted_times(TokenBucket(rate, burst, c1), c1, trace)
            b = admitted_times(GCRA(rate, burst, c2), c2, trace)
            self.assertEqual(a, b, "seed %d" % seed)

    def test_backwards_clock_jump_does_not_lock_out(self):
        clock = ManualClock(10000.0)
        tb = TokenBucket(rate=10, burst=10, clock=clock)
        for _ in range(10):
            tb.allow()
        clock.t -= 3600  # NTP steps the wall clock back an hour
        tb.allow()       # a request arrives right after the jump (denied, bucket empty)
        clock.t += 1.0   # one real second later
        self.assertTrue(tb.allow(), "token bucket should refill normally after a backwards jump")

    def test_gcra_locks_out_after_backwards_clock_jump(self):
        # Documented weakness: GCRA's stored time is now an hour in the future.
        # This is why it must run on a monotonic clock (or one shared server clock).
        clock = ManualClock(10000.0)
        g = GCRA(rate=10, burst=10, clock=clock)
        for _ in range(10):
            g.allow()
        clock.t -= 3600
        g.allow()
        clock.t += 1.0
        self.assertFalse(g.allow())


class SlidingLogProperties(unittest.TestCase):
    def test_no_window_exceeds_limit(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            limit, window = rng.choice([1, 5, 50]), rng.choice([0.1, 1.0])
            clock = ManualClock()
            adm = admitted_times(SlidingLog(limit, window, clock), clock, random_trace(rng))
            self.assertLessEqual(max_in_any_window(adm, window), limit, "seed %d" % seed)

    def test_admits_exactly_when_oracle_says_there_is_room(self):
        # Not only safe but not stingy: compare with a brute force oracle.
        for seed in range(50):
            rng = random.Random(seed)
            limit, window = rng.choice([1, 5, 50]), 1.0
            clock = ManualClock()
            sl = SlidingLog(limit, window, clock)
            oracle = []
            for t in random_trace(rng):
                clock.t = t
                expect = sum(1 for a in oracle if a > t - window) < limit
                got = sl.allow("k")
                self.assertEqual(got, expect, "seed %d t=%f" % (seed, t))
                if got:
                    oracle.append(t)


class FixedWindowProperties(unittest.TestCase):
    def test_each_aligned_window_within_limit(self):
        for seed in SEEDS:
            rng = random.Random(seed)
            limit = rng.choice([1, 5, 50])
            clock = ManualClock()
            adm = admitted_times(FixedWindow(limit, 1.0, clock), clock, random_trace(rng))
            counts = {}
            for t in adm:
                counts[int(t)] = counts.get(int(t), 0) + 1
            self.assertLessEqual(max(counts.values() or [0]), limit)

    def test_boundary_burst_reaches_twice_the_limit(self):
        clock = ManualClock()
        fw = FixedWindow(100, 1.0, clock)
        trace = [0.999] * 100 + [1.0] * 100
        adm = admitted_times(fw, clock, trace)
        self.assertEqual(max_in_any_window(adm, 1.0), 200)


class SlidingCounterProperties(unittest.TestCase):
    def test_current_window_never_exceeds_limit_so_any_window_below_2x(self):
        worst = 0
        for seed in SEEDS:
            rng = random.Random(seed)
            limit = rng.choice([5, 50])
            clock = ManualClock()
            adm = admitted_times(SlidingWindowCounter(limit, 1.0, clock), clock, random_trace(rng))
            m = max_in_any_window(adm, 1.0)
            worst = max(worst, m / float(limit))
            self.assertLessEqual(m, 2 * limit)
        print("\n  sliding counter worst window / limit on random traces: %.2f" % worst)

    def test_boundary_burst_is_blocked(self):
        clock = ManualClock()
        sc = SlidingWindowCounter(100, 1.0, clock)
        adm = admitted_times(sc, clock, [0.999] * 100 + [1.0] * 100)
        self.assertLessEqual(max_in_any_window(adm, 1.0), 101)


class AllLimiters(unittest.TestCase):
    def test_keys_are_independent(self):
        for name, make in ALL.items():
            clock = ManualClock()
            lim = make(3, 1.0, clock)
            a = [lim.allow("a") for _ in range(5)]
            b = [lim.allow("b") for _ in range(5)]
            self.assertEqual(a, b, name)
            self.assertEqual(sum(a), 3, name)

    def test_idle_key_recovers_full_allowance(self):
        for name, make in ALL.items():
            clock = ManualClock()
            lim = make(10, 1.0, clock)
            admitted_times(lim, clock, [0.5] * 50)
            clock.t = 10.0
            self.assertEqual(sum(lim.allow("k") for _ in range(10)), 10, name)


if __name__ == "__main__":
    unittest.main()
