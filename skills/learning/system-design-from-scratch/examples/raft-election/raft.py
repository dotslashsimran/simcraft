"""Raft leader election plus the log replication needed to make it matter.

Follows Figure 2 of the Raft paper (Ongaro and Ousterhout, 2014), on top of
faultsim, so every run is deterministic for a seed.

What is here:
  - terms, RequestVote, randomized election timeouts, heartbeats
  - the election restriction (only vote for a log at least as up to date)
  - AppendEntries with the prev index/term consistency check, conflict
    truncation, and commit only for entries from the leader's own term
  - currentTerm, votedFor and the log persisted before replying
  - PreVote and CheckQuorum, the two extensions etcd turns on in production

What is left out: snapshots, membership changes, client sessions, batching,
and the fast backtracking of nextIndex (it steps back one entry at a time).

Switches that break it on purpose, so the tests can show they catch it:
  persist_vote=False   votedFor lives only in memory, lost on restart
  log_check=False      vote for any candidate, ignoring how complete its log is
  randomized=False     every node uses the same election timeout
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))

from faultsim import DiskFull, Node  # noqa: E402

FOLLOWER, PRE_CANDIDATE, CANDIDATE, LEADER = "follower", "precandidate", "candidate", "leader"


class Config:
    def __init__(self, **kw):
        self.election_timeout = (0.150, 0.300)  # seconds, the paper's example range
        self.heartbeat = 0.050
        self.randomized = True
        self.pre_vote = False
        self.check_quorum = False
        self.persist_vote = True
        self.log_check = True
        self.max_batch = 64
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError("unknown option %s" % k)
            setattr(self, k, v)


class Raft(Node):
    def __init__(self, node_id, cfg=None):
        Node.__init__(self, node_id)
        self.cfg = cfg or Config()

    # ------------------------------------------------------------------ boot
    def on_start(self):
        self.term = self.load("term", 0)
        self.voted_for = self.load("voted_for") if self.cfg.persist_vote else None
        self.entries = self.load("log", [])          # list of (term, command)
        self.role = FOLLOWER
        self.leader = None
        self.commit = 0
        self.votes = set()
        self.next_idx, self.match_idx = {}, {}
        self.last_leader_contact = -1e9
        self.acks = set()
        self.election_timer = None
        self.reset_election_timer()

    def quorum(self):
        return len(self.sim.nodes) // 2 + 1

    def last(self):
        return (self.entries[-1][0] if self.entries else 0), len(self.entries)

    def save(self, **fields):
        """Persist before acting on it. Raises DiskFull, and then we must not act."""
        for k, v in fields.items():
            if k == "voted_for" and not self.cfg.persist_vote:
                continue
            self.persist(k, v)

    # ---------------------------------------------------------------- timers
    def reset_election_timer(self):
        if self.election_timer:
            self.election_timer.cancel()
        lo, hi = self.cfg.election_timeout
        timeout = self.rng.uniform(lo, hi) if self.cfg.randomized else lo
        self.election_timer = self.set_timer(timeout, self.on_election_timeout)

    def on_election_timeout(self):
        if self.role == LEADER:
            return
        if self.cfg.pre_vote:
            self.start_pre_vote()
        else:
            self.start_election()
        self.reset_election_timer()

    # -------------------------------------------------------------- elections
    def start_pre_vote(self):
        # Ask "would you vote for me in term+1?" without bumping our term.
        # A node cut off from the cluster cannot win this, so it never
        # inflates its term and never disrupts a healthy leader on return.
        self.role = PRE_CANDIDATE
        self.votes = {self.id}
        lt, li = self.last()
        self.sim.stats["pre_votes"] += 1
        self.broadcast({"type": "vote_req", "pre": True, "term": self.term + 1, "last_term": lt, "last_idx": li})

    def start_election(self):
        try:
            self.save(term=self.term + 1, voted_for=self.id)
        except DiskFull:
            self.role = FOLLOWER
            return
        self.term += 1
        self.voted_for = self.id
        self.role = CANDIDATE
        self.leader = None
        self.votes = {self.id}
        self.sim.stats["elections"] += 1
        self.log_event("candidate")
        lt, li = self.last()
        self.broadcast({"type": "vote_req", "pre": False, "term": self.term, "last_term": lt, "last_idx": li})
        if len(self.votes) >= self.quorum():  # single node cluster
            self.become_leader()

    def log_ok(self, msg):
        if not self.cfg.log_check:
            return True
        lt, li = self.last()
        return (msg["last_term"], msg["last_idx"]) >= (lt, li)

    def on_vote_req(self, src, m):
        if m["pre"]:
            heard_recently = self.sim.now - self.last_leader_contact < self.cfg.election_timeout[0]
            grant = (m["term"] > self.term and self.log_ok(m) and not heard_recently
                     and self.role != LEADER)
            self.send(src, {"type": "vote", "pre": True, "term": m["term"], "granted": grant})
            return
        if m["term"] > self.term:
            if not self.step_down(m["term"]):
                return
        grant = False
        if m["term"] == self.term and self.voted_for in (None, src) and self.log_ok(m):
            try:
                self.save(voted_for=src)
            except DiskFull:
                return  # cannot promise what we cannot remember
            self.voted_for = src
            grant = True
            self.reset_election_timer()
        self.send(src, {"type": "vote", "pre": False, "term": self.term, "granted": grant})

    def on_vote(self, src, m):
        if m["pre"]:
            if self.role == PRE_CANDIDATE and m["term"] == self.term + 1 and m["granted"]:
                self.votes.add(src)
                if len(self.votes) >= self.quorum():
                    self.start_election()
            return
        if m["term"] > self.term:
            self.step_down(m["term"])
            return
        if self.role == CANDIDATE and m["term"] == self.term and m["granted"]:
            self.votes.add(src)
            if len(self.votes) >= self.quorum():
                self.become_leader()

    def step_down(self, term):
        try:
            self.save(term=term, voted_for=None)
        except DiskFull:
            return False
        if self.role == LEADER:
            self.log_event("steps down")
        self.term, self.voted_for, self.role = term, None, FOLLOWER
        return True

    def become_leader(self):
        self.role = LEADER
        self.leader = self.id
        self.sim.stats["leaders_elected"] += 1
        self.log_event("LEADER")
        self.sim.on_leader(self)
        _, li = self.last()
        self.next_idx = {p: li + 1 for p in self.peers()}
        self.match_idx = {p: 0 for p in self.peers()}
        self.propose("noop-%s-%d" % (self.id, self.term))  # lets it commit older entries
        self.acks = set()
        self.heartbeat()
        if self.cfg.check_quorum:
            self.set_timer(self.cfg.election_timeout[1], self.check_quorum, self.term)

    def check_quorum(self, term):
        # A leader that has not heard from a majority for a whole election
        # timeout steps down, instead of serving stale reads in a minority.
        if self.role != LEADER or self.term != term:
            return
        if len(self.acks | {self.id}) < self.quorum():
            self.log_event("lost quorum")
            self.role = FOLLOWER
            self.leader = None
            self.reset_election_timer()
            return
        self.acks = set()
        self.set_timer(self.cfg.election_timeout[1], self.check_quorum, term)

    # ------------------------------------------------------------ replication
    def propose(self, command):
        if self.role != LEADER:
            return False
        entry = (self.term, command)
        try:
            self.save(log=self.entries + [entry])
        except DiskFull:
            return False
        self.entries.append(entry)
        self.advance_commit()
        return True

    def heartbeat(self):
        if self.role != LEADER:
            return
        for p in self.peers():
            self.send_append(p)
        self.set_timer(self.cfg.heartbeat, self.heartbeat)

    def send_append(self, p):
        prev = self.next_idx[p] - 1
        entries = self.entries[prev:prev + self.cfg.max_batch]
        self.send(p, {"type": "append", "term": self.term, "prev_idx": prev,
                      "prev_term": self.entries[prev - 1][0] if prev > 0 else 0,
                      "entries": entries, "commit": self.commit})

    def on_append(self, src, m):
        if m["term"] < self.term:
            self.send(src, {"type": "append_resp", "term": self.term, "ok": False, "match": 0})
            return
        if m["term"] > self.term or self.role != FOLLOWER:
            if m["term"] > self.term:
                if not self.step_down(m["term"]):
                    return
            self.role = FOLLOWER
        self.leader = src
        self.last_leader_contact = self.sim.now
        self.reset_election_timer()
        prev = m["prev_idx"]
        if prev > len(self.entries) or (prev > 0 and self.entries[prev - 1][0] != m["prev_term"]):
            self.send(src, {"type": "append_resp", "term": self.term, "ok": False, "match": 0})
            return
        new_log = self.entries
        for k, entry in enumerate(m["entries"]):
            idx = prev + k  # 0 based position
            if idx < len(new_log) and new_log[idx][0] != entry[0]:
                new_log = new_log[:idx]  # conflict: drop it and everything after
            if idx >= len(new_log):
                new_log = new_log + [entry]
        if new_log is not self.entries:
            try:
                self.save(log=new_log)
            except DiskFull:
                return
            self.entries = new_log
        match = prev + len(m["entries"])
        if m["commit"] > self.commit:
            self.set_commit(min(m["commit"], match))
        self.send(src, {"type": "append_resp", "term": self.term, "ok": True, "match": match})

    def on_append_resp(self, src, m):
        if m["term"] > self.term:
            self.step_down(m["term"])
            return
        if self.role != LEADER or m["term"] != self.term:
            return
        self.acks.add(src)
        if m["ok"]:
            self.match_idx[src] = max(self.match_idx[src], m["match"])
            self.next_idx[src] = self.match_idx[src] + 1
            self.advance_commit()
        else:
            self.next_idx[src] = max(1, self.next_idx[src] - 1)
            self.send_append(src)

    def advance_commit(self):
        for n in range(len(self.entries), self.commit, -1):
            # Only count replicas for entries from our own term (paper, 5.4.2).
            if self.entries[n - 1][0] != self.term:
                break
            if 1 + sum(1 for p in self.match_idx if self.match_idx[p] >= n) >= self.quorum():
                self.set_commit(n)
                break

    def set_commit(self, n):
        if n > self.commit:
            self.commit = n
            self.sim.on_commit(self, n)

    # --------------------------------------------------------------- plumbing
    def on_message(self, src, m):
        getattr(self, "on_" + m["type"])(src, m)

    def log_event(self, what):
        self.sim.log(self.id, what, "term=%d" % self.term)


def make_cluster(sim, n=5, **cfg):
    """Add n Raft nodes and the safety checks. Returns the nodes."""
    sim.leaders_by_term = {}   # term -> node id
    sim.committed = {}         # index -> entry, as first committed by anyone
    sim.leader_history = []    # (time, term, node id)
    sim.violations = []

    def on_leader(node):
        other = sim.leaders_by_term.get(node.term)
        if other is not None and other != node.id:
            sim.violations.append("two leaders in term %d: %s and %s" % (node.term, other, node.id))
        sim.leaders_by_term[node.term] = node.id
        sim.leader_history.append((sim.now, node.term, node.id))
        for idx, entry in sim.committed.items():  # Leader Completeness
            if idx > len(node.entries) or node.entries[idx - 1] != entry:
                sim.violations.append("leader %s term %d is missing committed entry %d" % (node.id, node.term, idx))
                break

    def on_commit(node, n):
        start = getattr(node, "_checked", 0)
        if start > len(node.entries):  # restarted with a shorter view; recheck all
            start = 0
        node._checked = n
        for idx in range(start + 1, n + 1):  # State Machine Safety
            entry = node.entries[idx - 1]
            seen = sim.committed.setdefault(idx, entry)
            if seen != entry:
                sim.violations.append("index %d committed as %s on %s but %s elsewhere" % (idx, entry, node.id, seen))

    sim.on_leader, sim.on_commit = on_leader, on_commit
    sim.invariant("raft safety", lambda s: s.violations[0] if s.violations else None)
    config = Config(**cfg)
    return [sim.add(Raft(i, config)) for i in range(n)]


def leaders(sim):
    return [n for n in sim.live() if n.role == LEADER]


def drive_clients(sim, until, every=0.05):
    """Every `every` seconds, every node that thinks it leads gets a command."""
    counter = [0]

    def tick():
        for n in leaders(sim):
            counter[0] += 1
            n.propose("cmd%d" % counter[0])
        if sim.now + every < until:
            sim.schedule(every, tick)

    sim.schedule(every, tick)
