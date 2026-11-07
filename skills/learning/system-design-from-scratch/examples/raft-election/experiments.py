"""Measure Raft election behaviour under faults. Every number is simulated
time from faultsim, so it is the same on any machine for the same seeds.

    python3 experiments.py              # all sections
    python3 experiments.py torture      # one section
    python3 experiments.py --seeds 50   # fewer seeds, faster
"""

import argparse
import time
from collections import Counter

from raft import drive_clients, leaders, make_cluster  # also puts scripts/ on the path
from faultsim import InvariantViolation, Sim, random_faults  # noqa: E402

N = 5


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100.0 * len(xs)))] if xs else float("nan")


def first_leader_time(sim):
    return sim.leader_history[0][0] if sim.leader_history else None


def ms(x):
    return "none" if x is None else "%.0f" % (x * 1000)


# ---------------------------------------------------------------------------
def startup(seeds):
    print("## 1. Time to elect the first leader (5 nodes, delay 1-5 ms, %d seeds)\n" % seeds)
    print("| election timeout | p50 ms | p99 ms | max ms | no leader in 5 s | elections per success p50 / max |")
    print("|---|---|---|---|---|---|")
    for name, cfg in (("random 150-300 ms", {}),
                      ("random 150-160 ms", {"election_timeout": (0.150, 0.160)}),
                      ("fixed 150 ms", {"randomized": False})):
        times, stuck, tries = [], 0, []
        for seed in range(seeds):
            sim = Sim(seed=seed)
            make_cluster(sim, N, **cfg)
            sim.run(until=5.0)
            t = first_leader_time(sim)
            if t is None:
                stuck += 1
            else:
                times.append(t)
                tries.append(sim.stats["elections"])
        print("| %s | %s | %s | %s | %d of %d | %s / %s |" % (
            name, ms(pct(times, 50) if times else None), ms(pct(times, 99) if times else None),
            ms(max(times) if times else None), stuck, seeds,
            pct(tries, 50) if tries else "-", max(tries) if tries else "-"))
    print()


def failover(seeds):
    print("## 2. Failover: crash the leader at t=2 s, time until a new leader\n")
    print("| message loss | p50 ms | p99 ms | max ms | no new leader in 5 s |")
    print("|---|---|---|---|---|")
    for drop in (0.0, 0.1, 0.3):
        gaps, stuck = [], 0
        for seed in range(seeds):
            sim = Sim(seed=seed, drop=drop)
            make_cluster(sim, N)
            sim.run(until=2.0)
            old = leaders(sim)
            if not old:
                continue
            sim.crash(old[0].id)
            n_before = len(sim.leader_history)
            sim.run(until=7.0)
            new = sim.leader_history[n_before:]
            if new:
                gaps.append(new[0][0] - 2.0)
            else:
                stuck += 1
        print("| %d%% | %s | %s | %s | %d |" % (drop * 100, ms(pct(gaps, 50)), ms(pct(gaps, 99)), ms(max(gaps)), stuck))
    print("\nThe floor is the minimum election timeout (150 ms) plus one round trip: followers must first")
    print("notice the silence. Loss adds retries because lost votes force a new term.\n")


def rejoin(seeds):
    print("## 3. A follower is cut off for 3 s, then returns (leader changes in the 3 s after it returns)\n")
    print("| pre_vote | disrupted runs | leader changes after heal, total | cut-off node's term on return, median |")
    print("|---|---|---|---|")
    for pv in (False, True):
        disrupted, changes, terms = 0, 0, []
        for seed in range(seeds):
            sim = Sim(seed=seed)
            make_cluster(sim, N, pre_vote=pv)
            sim.run(until=2.0)
            lead = leaders(sim)
            if not lead:
                continue
            victim = next(i for i in sim.nodes if i != lead[0].id)
            sim.isolate(victim)
            sim.run(until=5.0)
            terms.append(sim.nodes[victim].term - lead[0].term)
            sim.heal()
            before = len(sim.leader_history)
            sim.run(until=8.0)
            c = len(sim.leader_history) - before
            changes += c
            disrupted += c > 0
        print("| %s | %d of %d | %d | +%d |" % ("on" if pv else "off", disrupted, seeds, changes, pct(terms, 50)))
    print("\nWithout PreVote the isolated node keeps starting elections, so its term climbs. When it")
    print("returns, its higher term makes the healthy leader step down for nothing.\n")


def stale_leader(seeds):
    print("## 4. The leader and one follower are cut off from the other three for 3 s\n")
    print("| check_quorum | old leader still thinks it leads after, median ms | commands it accepted that were later thrown away |")
    print("|---|---|---|")
    for cq in (False, True):
        stale, lost = [], 0
        for seed in range(seeds):
            sim = Sim(seed=seed)
            make_cluster(sim, N, check_quorum=cq)
            drive_clients(sim, until=8.0)
            sim.run(until=2.0)
            lead = leaders(sim)
            if not lead:
                continue
            old = lead[0]
            buddy = next(i for i in sim.nodes if i != old.id)
            sim.partition([old.id, buddy])
            start, gone = sim.now, None
            while sim.now < 5.0:
                sim.run(until=sim.now + 0.01)
                if gone is None and old.role != "leader":
                    gone = sim.now - start
            stale.append(3.0 if gone is None else gone)
            accepted = {e for e in old.entries if e[1].startswith("cmd")}
            sim.heal()
            sim.run(until=8.0)
            committed = set(sim.committed.values())
            lost += len(accepted - committed)
        print("| %s | %s | %d |" % ("on" if cq else "off", ms(pct(stale, 50)), lost))
    print("\nNo command was lost from the client's point of view: those entries were never committed,")
    print("so no client was told they succeeded. But a stale leader that serves reads returns old data,")
    print("which is why etcd turns CheckQuorum on and uses ReadIndex or leases for reads.\n")


def drift(seeds):
    print("## 5. Clock drift: node 0's clock runs fast, leader crashes every 2 s for 20 s\n")
    print("| node 0 clock rate | share of elections node 0 won | safety violations |")
    print("|---|---|---|")
    for rate in (1.0, 1.5, 2.0):
        wins, total, bad = 0, 0, 0
        for seed in range(seeds):
            sim = Sim(seed=seed)
            make_cluster(sim, N)
            sim.set_clock(0, drift=rate)
            for t in range(2, 20, 2):
                sim.at(t, lambda s=sim: [s.crash(l.id) for l in leaders(s)])
                sim.at(t + 1.0, lambda s=sim: [s.restart(i) for i in s.nodes])
            try:
                sim.run(until=20.0)
            except InvariantViolation:
                bad += 1
            total += len(sim.leader_history)
            wins += sum(1 for _, _, who in sim.leader_history if who == 0)
        print("| %.1fx | %.0f%% (fair share 20%%) | %d |" % (rate, 100.0 * wins / max(1, total), bad))
    print("\nA fast clock fires its election timer first, so that node wins far more than its share.")
    print("Safety does not care: Raft never compares clocks across nodes, only terms.\n")


def torture(seeds, until=10.0):
    print("## 6. Random fault search, %d seeds per cell\n" % seeds)
    print("mixed: crashes, partitions, isolation, slow links, 30%% loss bursts, a clock running at 0.5x or 2x; 5%% base loss; %gs runs." % until)
    print("crash storm: crash and restart within 1-20 ms every 20-200 ms; 5-50 ms delays; 150-160 ms timeouts; 5 s runs.\n")
    print("| variant | nemesis | runs with a safety violation | first failing seed | first violation |")
    print("|---|---|---|---|---|")
    variants = (
        ("correct", {}),
        ("correct + pre_vote + check_quorum", {"pre_vote": True, "check_quorum": True}),
        ("BUG: votedFor not persisted", {"persist_vote": False}),
        ("BUG: no log up-to-date check", {"log_check": False}),
    )
    for name, cfg in variants:
        for nemesis in ("mixed", "crash storm"):
            bad, first, example = 0, None, ""
            for seed in range(seeds):
                if nemesis == "mixed":
                    sim = Sim(seed=seed, drop=0.05)
                    make_cluster(sim, N, **cfg)
                    end = until
                    random_faults(sim, end, every=(0.3, 1.5), lasting=(0.2, 1.5))
                else:
                    sim = Sim(seed=seed, delay=(0.005, 0.05))
                    make_cluster(sim, N, election_timeout=(0.150, 0.160), **cfg)
                    end = 5.0
                    random_faults(sim, end, every=(0.02, 0.2), lasting=(0.001, 0.02), kinds=("crash",))
                drive_clients(sim, end)
                try:
                    sim.run(until=end)
                except InvariantViolation as e:
                    bad += 1
                    if first is None:
                        first, example = seed, str(e).splitlines()[0].split(": ", 1)[-1]
            print("| %s | %s | %d of %d | %s | %s |" % (name, nemesis, bad, seeds,
                                                       "-" if first is None else first, example))
    print("\nReplay any failure with its seed: the run, the fault schedule and the trace are identical.\n")


SECTIONS = {"startup": startup, "failover": failover, "rejoin": rejoin, "stale": stale_leader,
            "drift": drift, "torture": torture}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("sections", nargs="*", default=list(SECTIONS))
    ap.add_argument("--seeds", type=int, default=200)
    a = ap.parse_args()
    t0 = time.time()
    for s in a.sections:
        n = a.seeds if s not in ("drift", "torture") else max(10, a.seeds // 2)
        SECTIONS[s](n)
    print("(wall time %.0f s)" % (time.time() - t0))
