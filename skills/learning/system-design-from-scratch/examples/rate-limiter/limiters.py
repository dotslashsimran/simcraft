"""Five rate limiters, smallest correct versions, one shared interface.

    limiter.allow(key) -> True (admit) or False (reject, send HTTP 429)

Every limiter takes a `clock` so tests can drive time by hand. The default
is time.monotonic, which never goes backwards. Wall clock time can (NTP
steps, a VM resumed from a snapshot), and the tests show what each
algorithm does when it does.

Only admitted requests are counted. A limiter that also counts rejected
requests keeps a client that retries in a tight loop blocked forever.

State per key, which is what decides memory at a million keys:

    TokenBucket            2 numbers   (tokens, last refill time)
    GCRA                   1 number    (theoretical arrival time)
    FixedWindow            2 numbers   (window id, count)
    SlidingWindowCounter   3 numbers   (window id, previous count, current count)
    SlidingLog             up to `limit` timestamps
"""

import time
from collections import deque

EPS = 1e-9  # float slack so that exact boundaries are not lost to rounding


class TokenBucket:
    """Bucket holds up to `burst` tokens and refills at `rate` per second.
    A request takes `cost` tokens. Refill is computed lazily on each call,
    so there is no background timer and an idle key costs nothing.

    Guarantee: over any interval of length T, admitted <= burst + rate * T.
    """

    def __init__(self, rate, burst, clock=time.monotonic):
        self.rate, self.burst, self.clock = float(rate), float(burst), clock
        self.state = {}

    def allow(self, key="default", cost=1):
        now = self.clock()
        st = self.state.get(key)
        if st is None:
            st = self.state[key] = [self.burst, now]
        # If the clock moved backwards, count it as zero elapsed time and
        # resync. Keeping the old timestamp would freeze refills until the
        # clock caught up again, which could be hours.
        elapsed = max(0.0, now - st[1])
        st[0] = min(self.burst, st[0] + elapsed * self.rate)
        st[1] = now
        if st[0] + EPS >= cost:
            st[0] -= cost
            return True
        return False


class GCRA:
    """Generic Cell Rate Algorithm: a token bucket stored as one timestamp.

    Keep a "theoretical arrival time" (TAT): when the bucket would be full
    again if no more requests came. Admit if TAT is at most `tau` ahead of
    now, where tau = (burst - 1) / rate. Makes the same decisions as
    TokenBucket (tested), with half the state and no float token count.
    """

    def __init__(self, rate, burst, clock=time.monotonic):
        self.interval = 1.0 / rate
        self.tau = (burst - 1) * self.interval
        self.clock = clock
        self.state = {}

    def allow(self, key="default", cost=1):
        now = self.clock()
        tat = max(self.state.get(key, now), now)
        # Same test as "tokens >= cost", since tokens = burst - (tat - now) / interval.
        if tat - now <= self.tau - (cost - 1) * self.interval + EPS:
            self.state[key] = tat + cost * self.interval
            return True
        return False


class FixedWindow:
    """Count requests in aligned windows [k*W, (k+1)*W). Reset at each edge.

    Cheap and simple (Redis INCR + EXPIRE), but a client can send `limit`
    at the end of one window and `limit` at the start of the next: 2x the
    limit inside one window length.
    """

    def __init__(self, limit, window, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, float(window), clock
        self.state = {}

    def allow(self, key="default", cost=1):
        w = int(self.clock() // self.window)
        st = self.state.get(key)
        if st is None or st[0] != w:
            st = self.state[key] = [w, 0]
        if st[1] + cost <= self.limit:
            st[1] += cost
            return True
        return False


class SlidingLog:
    """Keep the timestamp of every admitted request in the last W seconds.
    Exact: no window of length W ever holds more than `limit`. Memory is
    O(limit) per key, which is the price of exactness.
    """

    def __init__(self, limit, window, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, float(window), clock
        self.state = {}

    def allow(self, key="default", cost=1):
        now = self.clock()
        log = self.state.get(key)
        if log is None:
            log = self.state[key] = deque()
        while log and log[0] <= now - self.window:
            log.popleft()
        if len(log) + cost <= self.limit:
            log.extend([now] * cost)
            return True
        return False


class SlidingWindowCounter:
    """Approximate a sliding window from two fixed window counters.

    estimate = previous_count * (fraction of previous window still inside
    the sliding window) + current_count. Assumes the previous window's
    requests were spread evenly, so it is wrong when they were bunched.
    O(1) memory, no 2x boundary burst.
    """

    def __init__(self, limit, window, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, float(window), clock
        self.state = {}

    def allow(self, key="default", cost=1):
        now = self.clock()
        w = int(now // self.window)
        st = self.state.get(key)
        if st is None:
            st = self.state[key] = [w, 0, 0]
        if st[0] != w:
            st[1] = st[2] if st[0] == w - 1 else 0
            st[2] = 0
            st[0] = w
        frac = (now - w * self.window) / self.window
        estimate = st[1] * (1.0 - frac) + st[2]
        if estimate + cost <= self.limit + EPS:
            st[2] += cost
            return True
        return False


ALL = {
    "token_bucket": lambda limit, window, clock: TokenBucket(limit / window, limit, clock),
    "gcra": lambda limit, window, clock: GCRA(limit / window, limit, clock),
    "fixed_window": lambda limit, window, clock: FixedWindow(limit, window, clock),
    "sliding_log": lambda limit, window, clock: SlidingLog(limit, window, clock),
    "sliding_counter": lambda limit, window, clock: SlidingWindowCounter(limit, window, clock),
}


class ManualClock:
    """A clock the test moves by hand."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t
