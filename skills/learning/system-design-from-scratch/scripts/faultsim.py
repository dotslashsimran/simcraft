#!/usr/bin/env python3
"""Deterministic network and fault simulator for message passing nodes.

A discrete event simulator: there is no real sleeping and no real sockets.
Time is a number that jumps from one event to the next, so ten simulated
minutes of a five node cluster run in well under a second, and the same
seed always gives exactly the same run. When a test fails it prints the
seed, and rerunning with that seed replays the failure event by event.
(FoundationDB tests its database this way, and so do many Raft libraries.)

What you can break:

  network    delay (min, max), drop rate, duplicate rate, per link overrides,
             reordering (falls out of random delay), partitions, one-way
             blocks, isolating a node
  nodes      crash (volatile state lost, timers cancelled, inbox dropped),
             restart (on_start runs again, the disk survives)
  clocks     per node offset (wall clock skew), drift (clock runs fast or
             slow, which also changes how long its timers really take),
             jumps (an NTP step)
  disk       disk full: persist() raises DiskFull

Write a node by subclassing Node:

    class Pinger(Node):
        def on_start(self):                 # boot and every restart
            self.count = self.load("count", 0)
            self.set_timer(0.1, self.tick)
        def tick(self):
            self.broadcast({"type": "ping"})
            self.set_timer(0.1, self.tick)
        def on_message(self, src, msg):
            self.count += 1
            self.persist("count", self.count)

    sim = Sim(seed=7, delay=(0.001, 0.010), drop=0.05)
    for i in range(3):
        sim.add(Pinger(i))
    sim.at(1.0, sim.partition, [0], [1, 2])
    sim.at(2.0, sim.heal)
    sim.invariant("count never negative", lambda s: all(n.count >= 0 for n in s.live()))
    sim.run(until=5.0)

Run `python3 faultsim.py --demo` for a worked example: a heartbeat
failure detector that cannot tell a crashed node from a partitioned one.
"""

import argparse
import copy
import heapq
import random
from collections import Counter, deque

__all__ = ["Sim", "Node", "DiskFull", "InvariantViolation", "random_faults"]


_IMMUTABLE = (int, float, str, bool, bytes, type(None), tuple, frozenset)


def _copy(v):
    """Deep copy for plain data, much faster than copy.deepcopy. Copying is
    what stops two nodes from sharing a mutable object, which no real network
    or disk would allow. Tuples are treated as immutable, so do not put lists
    inside tuples you send or persist."""
    t = type(v)
    if t in _IMMUTABLE:
        return v
    if t is list:
        return [x if type(x) in _IMMUTABLE else _copy(x) for x in v]
    if t is dict:
        return {k: (x if type(x) in _IMMUTABLE else _copy(x)) for k, x in v.items()}
    return copy.deepcopy(v)


class DiskFull(Exception):
    pass


class InvariantViolation(AssertionError):
    pass


class Timer:
    __slots__ = ("cancelled", "when")

    def __init__(self, when):
        self.cancelled = False
        self.when = when

    def cancel(self):
        self.cancelled = True


class Node:
    """Base class. Override on_start and on_message."""

    def __init__(self, node_id):
        self.id = node_id
        self.sim = None
        self.alive = True
        self.disk = {}          # survives crashes
        self.rng = None         # per node, seeded from the sim seed
        self._timers = set()
        self.clock_offset = 0.0
        self.clock_drift = 1.0
        self.disk_full = False

    # -- override these --------------------------------------------------
    def on_start(self):
        pass

    def on_message(self, src, msg):
        pass

    def on_crash(self):
        pass

    # -- helpers ---------------------------------------------------------
    def send(self, dst, msg):
        self.sim.send(self.id, dst, msg)

    def broadcast(self, msg):
        for other in self.sim.nodes:
            if other != self.id:
                self.sim.send(self.id, other, msg)

    def peers(self):
        return [n for n in self.sim.nodes if n != self.id]

    def set_timer(self, local_seconds, fn, *args):
        """Fire fn after local_seconds of *this node's* clock."""
        real = local_seconds / self.clock_drift
        t = self.sim.schedule(real, self._fire, fn, args)
        self._timers.add(t)
        return t

    def _fire(self, fn, args):
        if self.alive:
            fn(*args)

    def clock(self):
        """This node's wall clock, with skew and drift applied."""
        return self.sim.now * self.clock_drift + self.clock_offset

    def persist(self, key, value):
        if self.disk_full:
            self.sim.stats["disk_full_errors"] += 1
            raise DiskFull("node %s: no space left on device" % self.id)
        self.disk[key] = _copy(value)

    def load(self, key, default=None):
        return _copy(self.disk.get(key, default))

    def log(self, *parts):
        self.sim.log(self.id, *parts)


class Sim:
    def __init__(self, seed=0, delay=(0.001, 0.005), drop=0.0, duplicate=0.0, trace=200):
        self.seed = seed
        self.rng = random.Random(seed)
        self.now = 0.0
        self.nodes = {}
        self.delay = delay
        self.drop = drop
        self.duplicate = duplicate
        self.links = {}          # (src, dst) -> {"delay":..., "drop":...}
        self.blocked = set()     # (src, dst) pairs that cannot talk
        self.stats = Counter()
        self.trace = deque(maxlen=trace)
        self._heap = []
        self._seq = 0
        self._invariants = []

    # -- events ------------------------------------------------------------
    def schedule(self, delay, fn, *args):
        t = Timer(self.now + max(0.0, delay))
        self._seq += 1
        heapq.heappush(self._heap, (t.when, self._seq, t, fn, args))
        return t

    def at(self, when, fn, *args):
        return self.schedule(when - self.now, fn, *args)

    def run(self, until=None, max_events=None):
        events = 0
        while self._heap:
            when, _, timer, fn, args = self._heap[0]
            if until is not None and when > until:
                break
            heapq.heappop(self._heap)
            if timer.cancelled:
                continue
            self.now = when
            fn(*args)
            events += 1
            self.stats["events"] += 1
            for name, check in self._invariants:
                problem = check(self)
                if problem is not None and problem is not True:
                    self._violate(name, problem)
            if max_events and events >= max_events:
                break
        if until is not None:
            self.now = max(self.now, until)
        return events

    def invariant(self, name, fn):
        """fn(sim) returns None/True if fine, or a string/False if broken.
        Checked after every event."""
        self._invariants.append((name, fn))

    def _violate(self, name, problem):
        detail = "" if problem is False else ": %s" % problem
        lines = ["invariant '%s' broken at t=%.6f (seed=%s)%s" % (name, self.now, self.seed, detail),
                 "last events:"]
        lines.extend("  " + l for l in self.format_trace(30))
        raise InvariantViolation("\n".join(lines))

    def log(self, who, *parts):
        self.trace.append((self.now, who, parts))

    def format_trace(self, n=None):
        items = list(self.trace)[-n:] if n else list(self.trace)
        return ["%10.6f  %-6s %s" % (t, w, " ".join(str(p) for p in parts)) for t, w, parts in items]

    # -- nodes -------------------------------------------------------------
    def add(self, node, start_at=0.0):
        node.sim = self
        node.rng = random.Random("%s-%s" % (self.seed, node.id))
        self.nodes[node.id] = node
        self.at(start_at, self._start, node.id)
        return node

    def _start(self, node_id):
        n = self.nodes[node_id]
        n.alive = True
        n.on_start()

    def live(self):
        return [n for n in self.nodes.values() if n.alive]

    def crash(self, node_id):
        n = self.nodes[node_id]
        if not n.alive:
            return
        n.alive = False
        for t in n._timers:
            t.cancel()
        n._timers.clear()
        n.on_crash()
        self.stats["crashes"] += 1
        self.log(node_id, "CRASH")

    def restart(self, node_id):
        n = self.nodes[node_id]
        if n.alive:
            return
        self.stats["restarts"] += 1
        self.log(node_id, "RESTART")
        self._start(node_id)

    # -- clocks and disks --------------------------------------------------
    def set_clock(self, node_id, offset=None, drift=None):
        """offset: seconds added to the wall clock. drift: 1.1 means the clock
        (and so every timer) runs 10% fast."""
        n = self.nodes[node_id]
        if offset is not None:
            n.clock_offset = offset
        if drift is not None:
            n.clock_drift = drift
        self.log(node_id, "CLOCK offset=%s drift=%s" % (n.clock_offset, n.clock_drift))

    def jump_clock(self, node_id, seconds):
        self.nodes[node_id].clock_offset += seconds
        self.log(node_id, "CLOCK JUMP %+g" % seconds)

    def set_disk_full(self, node_id, full=True):
        self.nodes[node_id].disk_full = full
        self.log(node_id, "DISK FULL" if full else "DISK OK")

    # -- network -----------------------------------------------------------
    def set_link(self, src, dst, delay=None, drop=None, both_ways=True):
        for a, b in ((src, dst), (dst, src)) if both_ways else ((src, dst),):
            cfg = self.links.setdefault((a, b), {})
            if delay is not None:
                cfg["delay"] = delay
            if drop is not None:
                cfg["drop"] = drop

    def partition(self, *groups):
        """partition([0, 1], [2, 3, 4]): nodes in different groups cannot
        talk. Nodes not listed form one more group together."""
        listed = {n for g in groups for n in g}
        rest = [n for n in self.nodes if n not in listed]
        groups = [list(g) for g in groups] + ([rest] if rest else [])
        side = {n: i for i, g in enumerate(groups) for n in g}
        self.blocked = {(a, b) for a in self.nodes for b in self.nodes if side[a] != side[b]}
        self.log("net", "PARTITION", groups)

    def isolate(self, node_id):
        self.partition([node_id])

    def block(self, src, dst):
        """One-way: src's messages to dst are lost, dst to src still works."""
        self.blocked.add((src, dst))
        self.log("net", "BLOCK %s->%s" % (src, dst))

    def heal(self):
        self.blocked = set()
        self.log("net", "HEAL")

    def send(self, src, dst, msg):
        self.stats["sent"] += 1
        cfg = self.links.get((src, dst), {})
        drop = cfg.get("drop", self.drop)
        if (src, dst) in self.blocked:
            self.stats["blocked"] += 1
            return
        if drop and self.rng.random() < drop:
            self.stats["dropped"] += 1
            return
        copies = 2 if self.duplicate and self.rng.random() < self.duplicate else 1
        lo, hi = cfg.get("delay", self.delay)
        for _ in range(copies):
            self.schedule(self.rng.uniform(lo, hi), self._deliver, src, dst, _copy(msg))

    def _deliver(self, src, dst, msg):
        n = self.nodes.get(dst)
        # A partition that starts while a message is in flight also eats it.
        if n is None or not n.alive or (src, dst) in self.blocked:
            self.stats["lost_in_flight"] += 1
            return
        self.stats["delivered"] += 1
        n.on_message(src, msg)


def random_faults(sim, until, every=(1.0, 3.0), lasting=(0.3, 1.5),
                  kinds=("crash", "partition", "isolate", "slow", "drop", "skew")):
    """Jepsen style nemesis: at random times apply one random fault, then
    undo it after a random duration. Uses the sim's seeded rng, so the
    schedule is part of the replayable run."""
    rng = random.Random("%s-nemesis" % sim.seed)
    t = rng.uniform(*every)
    ids = sorted(sim.nodes)
    while t < until:
        kind = rng.choice(kinds)
        dur = rng.uniform(*lasting)
        victim = rng.choice(ids)
        if kind == "crash":
            sim.at(t, sim.crash, victim)
            sim.at(t + dur, sim.restart, victim)
        elif kind == "partition":
            shuffled = ids[:]
            rng.shuffle(shuffled)
            cut = rng.randint(1, len(ids) - 1)
            sim.at(t, sim.partition, shuffled[:cut], shuffled[cut:])
            sim.at(t + dur, sim.heal)
        elif kind == "isolate":
            sim.at(t, sim.isolate, victim)
            sim.at(t + dur, sim.heal)
        elif kind == "slow":
            for other in ids:
                if other != victim:
                    sim.at(t, sim.set_link, victim, other, (0.05, 0.2))
                    sim.at(t + dur, sim.set_link, victim, other, sim.delay)
        elif kind == "drop":
            old = sim.drop
            sim.at(t, setattr, sim, "drop", 0.3)
            sim.at(t + dur, setattr, sim, "drop", old)
        elif kind == "skew":
            sim.at(t, sim.set_clock, victim, None, rng.choice([0.5, 2.0]))
            sim.at(t + dur, sim.set_clock, victim, None, 1.0)
        t += dur + rng.uniform(*every)


# ---------------------------------------------------------------------------
# Demo: a heartbeat failure detector
# ---------------------------------------------------------------------------

class Heartbeater(Node):
    INTERVAL = 0.1
    TIMEOUT = 0.35

    def on_start(self):
        self.last_seen = {p: self.sim.now for p in self.peers()}
        self.suspected = set()
        self.set_timer(self.INTERVAL, self.tick)

    def tick(self):
        self.broadcast({"type": "hb"})
        for p, seen in self.last_seen.items():
            # Uses sim time on purpose: a failure detector should measure
            # elapsed time with a monotonic clock, not the wall clock.
            if self.sim.now - seen > self.TIMEOUT and p not in self.suspected:
                self.suspected.add(p)
                self.log("suspects", p)
                self.sim.stats["suspicions"] += 1
        self.set_timer(self.INTERVAL, self.tick)

    def on_message(self, src, msg):
        self.last_seen[src] = self.sim.now
        if src in self.suspected:
            self.suspected.discard(src)
            self.log("unsuspects", src)


def demo(seed):
    print("Heartbeat failure detector, 3 nodes, heartbeat every 100 ms, suspect after 350 ms.\n")
    for title, fault in (
        ("node 2 crashes at t=1.0", lambda s: s.at(1.0, s.crash, 2)),
        ("node 2 is partitioned away at t=1.0", lambda s: s.at(1.0, s.isolate, 2)),
        ("30% message loss, no crash", lambda s: setattr(s, "drop", 0.3)),
    ):
        sim = Sim(seed=seed, delay=(0.001, 0.02))
        for i in range(3):
            sim.add(Heartbeater(i))
        fault(sim)
        sim.run(until=3.0)
        print("== %s" % title)
        for line in sim.format_trace():
            if "suspects" in line or "CRASH" in line or "PARTITION" in line:
                print("  " + line)
        print("  suspicions=%d  sent=%d dropped=%d blocked=%d\n" % (
            sim.stats["suspicions"], sim.stats["sent"], sim.stats["dropped"], sim.stats["blocked"]))
    print("Nodes 0 and 1 log the same thing for a crash and for a partition: from the inside")
    print("the two are indistinguishable. Message loss alone also causes false suspicions,")
    print("which is why real detectors (phi accrual in Cassandra) output a suspicion level")
    print("instead of a yes/no, and why timeouts are a tuning trade-off, not a fact.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="run the failure detector demo")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    if a.demo:
        demo(a.seed)
    else:
        ap.print_help()
