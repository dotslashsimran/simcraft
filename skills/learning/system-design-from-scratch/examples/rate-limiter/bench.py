"""Measure the rate limiters: accuracy, cost, memory, real load, and faults.

    python3 bench.py            # everything (about 40 seconds)
    python3 bench.py accuracy   # one section: accuracy cost memory load http distributed

Accuracy and the distributed section run on simulated time and give the
same numbers on every machine. Cost, load and http use the real clock and
depend on the machine and on what else it is doing.
"""

import asyncio
import os
import random
import subprocess
import sys
import time
import timeit
import tracemalloc
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "scripts"))

from faultsim import Node, Sim  # noqa: E402
from limiters import ALL, ManualClock, TokenBucket  # noqa: E402
from loadgen import Histogram, HttpTarget, run_closed, run_open  # noqa: E402
from test_limiters import max_in_any_window  # noqa: E402

LIMIT, WINDOW = 100, 1.0


# ---------------------------------------------------------------------------
def traffic_patterns():
    rng = random.Random(42)
    steady = [i / 200.0 for i in range(2000)]  # 2x the limit, evenly spaced, 10 s
    poisson, t = [], 0.0
    while t < 10:
        t += rng.expovariate(200)
        poisson.append(t)
    bursty = [s + i / 1000.0 for s in range(10) for i in range(200)]  # 200 in first 0.2 s of each second
    boundary = []
    for s in range(1, 10):  # 100 just before each edge, 100 right on it
        boundary += [s - 0.001] * 100 + [float(s)] * 100
    # name -> (arrival times, seconds covered, so ideal admitted = LIMIT * seconds)
    return {"steady 2x": (steady, 10), "poisson 2x": (poisson, 10), "bursty": (bursty, 10),
            "boundary attack": (boundary, 9)}


def accuracy():
    print("## Accuracy (simulated clock, limit %d per %gs, token bucket burst = %d)\n" % (LIMIT, WINDOW, LIMIT))
    print("| pattern | algorithm | admitted | ideal | worst 1 s window |")
    print("|---|---|---|---|---|")
    for pname, (trace, seconds) in traffic_patterns().items():
        for name, make in ALL.items():
            clock = ManualClock()
            lim = make(LIMIT, WINDOW, clock)
            adm = []
            for t in trace:
                clock.t = t
                if lim.allow("k"):
                    adm.append(t)
            print("| %s | %s | %d | %d | %d |" % (pname, name, len(adm), LIMIT * seconds,
                                               max_in_any_window(adm, WINDOW)))
    print()


def cost():
    print("## Cost per allow() call (real clock, this machine)\n")
    print("| algorithm | 1 hot key, ns | 100k keys round robin, ns |")
    print("|---|---|---|")
    keys = ["user%d" % i for i in range(100000)]
    for name, make in ALL.items():
        lim = make(LIMIT, WINDOW, time.monotonic)
        n = 200000
        one = min(timeit.repeat(lambda: lim.allow("k"), number=n, repeat=3)) / n * 1e9
        lim = make(LIMIT, WINDOW, time.monotonic)
        it = iter(keys * 2)
        many = min(timeit.repeat(lambda: lim.allow(next(it)), number=len(keys), repeat=1)) / len(keys) * 1e9
        print("| %s | %.0f | %.0f |" % (name, one, many))
    print()


def memory():
    print("## Memory per key, each key at its full allowance (limit %d)\n" % LIMIT)
    print("| algorithm | bytes per key | 10M keys would need |")
    print("|---|---|---|")
    nkeys = 5000
    for name, make in ALL.items():
        clock = ManualClock(0.5)
        tracemalloc.start()
        lim = make(LIMIT, WINDOW, clock)
        for i in range(nkeys):
            k = "u%07d" % i
            for _ in range(LIMIT):
                lim.allow(k)
        size = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        per = size / float(nkeys)
        print("| %s | %.0f | %.1f GB |" % (name, per, per * 1e7 / 1e9))
    print("\n(Includes the key string and Python object overhead, about 60 bytes of it per key.)\n")


def load():
    print("## In-process load test: token bucket 1000/s, offered 5000/s for 5 s\n")
    tb = TokenBucket(1000, 100)
    res = asyncio.run(run_open(lambda: tb.allow("k"), rate=5000, duration=5, warmup=0.5, label="token_bucket"))
    print("```")
    print(res.report(chart=False))
    print("```")
    admitted = res.outcomes.get("True", 0)
    print("\nAdmitted %.1f per second (limit 1000; the 0.5 s warmup drained the burst).\n" % (admitted / res.elapsed))


def http():
    port = 18000 + os.getpid() % 1000
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py"), "--port", str(port), "--limit", "1000"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        proc.stdout.readline()
        url = "http://127.0.0.1:%d/?key=alice" % port
        print("## HTTP: server.py (token bucket 1000/s) driven by loadgen\n")
        print("```")
        res = asyncio.run(run_closed(HttpTarget(url, 16), concurrency=16, duration=5, warmup=1, label="closed loop, 16 conns"))
        print(res.report(chart=False))
        cap = res.throughput
        print()
        for frac in (0.5, 0.8):
            rate = int(cap * frac)
            r = asyncio.run(run_open(HttpTarget(url, 64), rate=rate, duration=5, warmup=1,
                                     label="open loop %d%% of capacity" % (frac * 100)))
            print(r.report(chart=False))
            print()
        print("```\n")
    finally:
        proc.terminate()
        proc.wait()


# ---------------------------------------------------------------------------
# Distributed: three gateways share one global limit
# ---------------------------------------------------------------------------

GLOBAL, GATEWAYS, SHARES, OFFERED = 300, ["g0", "g1", "g2"], [0.7, 0.15, 0.15], 600


class Gateway(Node):
    def __init__(self, node_id, strategy, timeout=0.05):
        Node.__init__(self, node_id)
        self.strategy, self.timeout = strategy, timeout

    def on_start(self):
        clock = lambda: self.sim.now  # noqa: E731
        share = GLOBAL / float(len(GATEWAYS))
        self.local = TokenBucket(share, share * 0.1, clock)
        self.pending, self.seq, self.arrivals, self.demand = {}, 0, 0, {}
        if self.strategy == "local_rebalanced":
            self.set_timer(0.5, self.rebalance)

    def arrive(self):
        self.arrivals += 1
        now = self.sim.now
        if self.strategy.startswith("local"):
            self.decide(now, self.local.allow())
            return
        self.seq += 1
        self.pending[self.seq] = now
        self.send("L", {"ask": self.seq})
        self.set_timer(self.timeout, self.expire, self.seq)

    def expire(self, req):
        if req in self.pending:
            self.sim.stats["timeouts"] += 1
            self.decide(self.pending.pop(req), self.strategy == "central_fail_open")

    def decide(self, arrived, ok):
        self.sim.decisions.append((self.sim.now, ok))
        self.sim.latency.record((self.sim.now - arrived) * 1e6)

    def rebalance(self):
        # Tell the others how busy we were; set our share from the last reports.
        self.demand[self.id] = self.arrivals
        self.broadcast({"demand": self.arrivals})
        self.arrivals = 0
        total = sum(self.demand.values()) or 1
        rate = max(1.0, GLOBAL * self.demand[self.id] / float(total))
        self.local.rate, self.local.burst = rate, rate * 0.1
        self.set_timer(0.5, self.rebalance)

    def on_message(self, src, msg):
        if "demand" in msg:
            self.demand[src] = msg["demand"]
        elif msg.get("reply") in self.pending:
            self.decide(self.pending.pop(msg["reply"]), msg["ok"])


class Central(Node):
    def on_start(self):
        self.bucket = TokenBucket(GLOBAL, GLOBAL * 0.1, lambda: self.sim.now)

    def on_message(self, src, msg):
        if "ask" in msg:
            self.send(src, {"reply": msg["ask"], "ok": self.bucket.allow()})


def run_distributed(strategy, fault, seed=1, until=10.0):
    sim = Sim(seed=seed, delay=(0.0005, 0.002))
    sim.decisions, sim.latency = [], Histogram()
    gws = [sim.add(Gateway(g, strategy)) for g in GATEWAYS]
    sim.add(Central("L"))
    rng = random.Random(seed)
    t = 0.0
    while t < until:  # Poisson arrivals, 70% land on g0
        t += rng.expovariate(OFFERED)
        g = rng.choices(gws, SHARES)[0]
        sim.at(t, g.arrive)
    if fault == "partition g0":
        sim.at(4.0, sim.isolate, "g0")
        sim.at(7.0, sim.heal)
    elif fault == "slow links":
        for g in GATEWAYS:
            sim.at(4.0, sim.set_link, g, "L", (0.04, 0.12))
            sim.at(7.0, sim.set_link, g, "L", sim.delay)
    sim.run(until=until + 1)
    calm = [ok for (tt, ok) in sim.decisions if 1 <= tt < 4 or 8 <= tt < 10]
    hit = [ok for (tt, ok) in sim.decisions if 4 <= tt < 7]
    return sum(calm) / 5.0, sum(hit) / 3.0, sim.latency, sim.stats["timeouts"]


def distributed():
    print("## Distributed: 3 gateways, one global limit of %d/s, %d/s offered, 70%% of it on g0\n" % (GLOBAL, OFFERED))
    print("Simulated with faultsim, seed 1. Fault window is t=4..7 s.\n")
    print("| strategy | fault | admitted/s outside fault | admitted/s during fault | decision p50 ms | p99 ms | timeouts |")
    print("|---|---|---|---|---|---|---|")
    for strategy in ("local_split", "local_rebalanced", "central_fail_open", "central_fail_closed"):
        for fault in ("none", "partition g0", "slow links"):
            if strategy.startswith("local") and fault == "slow links":
                continue
            calm, hit, lat, timeouts = run_distributed(strategy, fault)
            print("| %s | %s | %.0f | %.0f | %.2f | %.2f | %d |" % (
                strategy, fault, calm, hit, lat.percentile(50) / 1000.0, lat.percentile(99) / 1000.0, timeouts))
    print()


SECTIONS = {"accuracy": accuracy, "cost": cost, "memory": memory, "load": load, "http": http,
            "distributed": distributed}

if __name__ == "__main__":
    for name in sys.argv[1:] or list(SECTIONS):
        SECTIONS[name]()
