"""Tests for raft.py. Seeded, so a failure prints a seed you can replay.

    python3 -m unittest -v test_raft.py
"""

import unittest

from raft import drive_clients, leaders, make_cluster  # also puts scripts/ on the path
from faultsim import InvariantViolation, Sim, random_faults  # noqa: E402

SEEDS = range(15)


def cluster(seed, n=5, sim_kw=None, **cfg):
    sim = Sim(seed=seed, **(sim_kw or {}))
    nodes = make_cluster(sim, n, **cfg)
    return sim, nodes


class Election(unittest.TestCase):
    def test_healthy_cluster_elects_one_leader_quickly(self):
        for seed in SEEDS:
            sim, nodes = cluster(seed)
            sim.run(until=2.0)
            self.assertEqual(len(leaders(sim)), 1, "seed %d" % seed)
            self.assertLess(sim.leader_history[0][0], 1.0, "seed %d" % seed)
            self.assertEqual(len({n.term for n in nodes}), 1, "all nodes agree on the term")

    def test_new_leader_after_leader_crash(self):
        for seed in SEEDS:
            sim, _ = cluster(seed)
            sim.run(until=1.5)
            old = leaders(sim)[0]
            sim.crash(old.id)
            sim.run(until=3.0)
            now = leaders(sim)
            self.assertEqual(len(now), 1, "seed %d" % seed)
            self.assertNotEqual(now[0].id, old.id)
            self.assertGreater(now[0].term, old.term)

    def test_fixed_timeouts_split_the_vote_forever(self):
        # Every node times out at the same instant and votes for itself.
        sim, _ = cluster(0, randomized=False)
        sim.run(until=5.0)
        self.assertEqual(sim.leader_history, [])
        self.assertGreater(sim.stats["elections"], 50)


class Partitions(unittest.TestCase):
    def test_minority_leader_cannot_commit_and_steps_down_on_heal(self):
        for seed in SEEDS:
            sim, nodes = cluster(seed)
            drive_clients(sim, until=6.0)
            sim.run(until=1.5)
            old = leaders(sim)[0]
            buddy = next(n.id for n in nodes if n.id != old.id)
            sim.partition([old.id, buddy])
            commit_at_cut = old.commit
            sim.run(until=3.5)
            self.assertEqual(old.commit, commit_at_cut, "a minority must not commit (seed %d)" % seed)
            majority_leaders = [n for n in leaders(sim) if n.id not in (old.id, buddy)]
            self.assertEqual(len(majority_leaders), 1, "seed %d" % seed)
            sim.heal()
            sim.run(until=6.0)
            self.assertEqual(len(leaders(sim)), 1)
            self.assertNotEqual(old.role, "leader")
            # Everyone converges on the same committed prefix.
            c = min(n.commit for n in nodes)
            self.assertGreater(c, commit_at_cut)
            self.assertEqual(len({tuple(n.entries[:c]) for n in nodes}), 1)

    def test_pre_vote_stops_a_returning_node_from_deposing_the_leader(self):
        for seed in SEEDS:
            sim, nodes = cluster(seed, pre_vote=True)
            sim.run(until=1.5)
            lead = leaders(sim)[0]
            victim = next(n.id for n in nodes if n.id != lead.id)
            sim.isolate(victim)
            sim.run(until=4.5)
            sim.heal()
            sim.run(until=7.0)
            self.assertIs(leaders(sim)[0], lead, "seed %d" % seed)


class Disks(unittest.TestCase):
    def test_one_full_disk_plus_a_leader_crash_still_elects(self):
        for seed in SEEDS:
            sim, nodes = cluster(seed)
            sim.run(until=1.5)
            lead = leaders(sim)[0]
            full = next(n.id for n in nodes if n.id != lead.id)
            sim.set_disk_full(full)
            sim.crash(lead.id)
            sim.run(until=4.0)
            now = leaders(sim)
            self.assertEqual(len(now), 1, "seed %d" % seed)
            self.assertNotEqual(now[0].id, full, "a node that cannot persist its term cannot lead")

    def test_full_disks_spend_the_fault_budget(self):
        # 5 nodes tolerate 2 failures. Two full disks plus one crash is three:
        # no quorum can promise anything durably, so no leader, and no unsafe one.
        for seed in SEEDS:
            sim, nodes = cluster(seed)
            sim.run(until=1.5)
            lead = leaders(sim)[0]
            for i in [n.id for n in nodes if n.id != lead.id][:2]:
                sim.set_disk_full(i)
            sim.crash(lead.id)
            sim.run(until=5.0)
            self.assertEqual(leaders(sim), [], "seed %d" % seed)
            self.assertGreater(sim.stats["disk_full_errors"], 0)


class RandomFaults(unittest.TestCase):
    def run_many(self, seeds, crash_storm=False, **cfg):
        failures = []
        for seed in seeds:
            if crash_storm:
                sim, _ = cluster(seed, sim_kw={"delay": (0.005, 0.05)}, election_timeout=(0.150, 0.160), **cfg)
                end = 5.0
                random_faults(sim, end, every=(0.02, 0.2), lasting=(0.001, 0.02), kinds=("crash",))
            else:
                sim, _ = cluster(seed, sim_kw={"drop": 0.05}, **cfg)
                end = 8.0
                random_faults(sim, end, every=(0.3, 1.5), lasting=(0.2, 1.5))
            drive_clients(sim, end)
            try:
                sim.run(until=end)
            except InvariantViolation:
                failures.append(seed)
        return failures

    def test_correct_raft_survives_random_faults(self):
        self.assertEqual(self.run_many(range(20)), [])
        self.assertEqual(self.run_many(range(20), pre_vote=True, check_quorum=True), [])
        self.assertEqual(self.run_many(range(100), crash_storm=True), [])

    def test_checker_catches_missing_log_check(self):
        self.assertTrue(self.run_many(range(10), log_check=False))

    def test_checker_catches_unpersisted_vote(self):
        # Seed 85 is the first failing seed found by experiments.py.
        self.assertEqual(self.run_many([85], crash_storm=True, persist_vote=False), [85])
        self.assertEqual(self.run_many([85], crash_storm=True), [])


if __name__ == "__main__":
    unittest.main()
