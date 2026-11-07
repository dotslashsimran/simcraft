# Raft leader election, built and broken on purpose

A worked example of the skill's loop for a consensus system. `raft.py` is about 360 lines: elections, the log replication needed for elections to matter, PreVote and CheckQuorum. It runs on `faultsim`, so every number here is simulated time and is identical on any machine for the same seeds. Commands to reproduce are at the end.

## 1. Requirements and numbers

The system: a small replicated store for configuration and leases, like etcd or the controller quorum in Kafka.

| Requirement | Number |
|---|---|
| Nodes | 5, so it survives any 2 failures (a majority of 3 is still up) |
| Safety | never two leaders in one term; never lose or change a committed entry |
| Failover | a new leader within 1 s at p99 after the leader dies |
| Network | one way delay 1 to 5 ms in the tests (much slower than one data center, on purpose) |
| Load | small: configuration writes, tens per second |

Back of the envelope:

- **Fault budget.** n = 2f + 1. Five nodes tolerate two failures, seven tolerate three. Every extra node adds one more copy to every write, so clusters stay at 3 or 5.
- **Timeouts.** The Raft paper asks for broadcast time much less than election timeout, which is much less than the time between failures. With round trips of 2 to 10 ms, an election timeout of 150 to 300 ms is 15 to 150 round trips: long enough that a slow heartbeat is not mistaken for a dead leader, short enough for sub second failover. etcd defaults to a 100 ms heartbeat and a 1000 ms election timeout, because real networks and disks have worse tails than this simulation.
- **Heartbeat traffic.** The leader sends one heartbeat every 50 ms to each of 4 followers: 80 messages per second. Negligible.
- **Disk.** A node must write its term and vote to disk before it answers a vote request. On an SSD an fsync is roughly 0.1 to a few ms, so this is cheap in time, but it is the part people forget (see section 6).

## 2. What was built

- **Terms and RequestVote.** A follower that hears nothing for a random 150 to 300 ms becomes a candidate, bumps its term, votes for itself and asks the others. A majority makes it leader.
- **The election restriction.** A node only votes for a candidate whose log is at least as up to date as its own (higher last term, or equal term and at least as long).
- **AppendEntries.** Heartbeats carry entries with the previous index and term. A follower rejects a mismatch, drops conflicting entries, and appends. The leader commits an entry once a majority stores it, but only entries from its own term (paper section 5.4.2).
- **Persistence.** `currentTerm`, `votedFor` and the log go to disk before the node acts on them. If the disk is full, the node refuses to act.
- **PreVote.** Before bumping its term, a node asks "would you vote for me?". Nodes that heard from a leader recently say no.
- **CheckQuorum.** A leader that has not heard from a majority for one election timeout steps down.

Left out: snapshots, membership changes, read paths (ReadIndex, leases), batching, and fast `nextIndex` backtracking.

Three switches break it on purpose, so we can check the tests catch it: `persist_vote=False`, `log_check=False`, `randomized=False`.

## 3. Invariants and tests

The cluster checks these after every simulated event, and stops with the seed and the last 30 events when one breaks:

1. **Election Safety:** at most one leader per term.
2. **State Machine Safety:** no two nodes ever commit different entries at the same index.
3. **Leader Completeness:** a new leader's log holds every entry committed before it was elected.

`test_raft.py`, 10 tests, all pass:

| Test | Result |
|---|---|
| healthy cluster elects exactly one leader within 1 s, 15 seeds | pass |
| leader crash gives a new leader with a higher term, 15 seeds | pass |
| fixed timeouts split the vote forever (5 s, more than 50 elections, no leader) | pass |
| a leader cut off with one follower cannot commit; the majority elects a new leader; after healing all nodes share one committed prefix | pass |
| with PreVote, a follower returning from 3 s of isolation does not depose the leader | pass |
| one full disk plus a leader crash: a new leader is elected, and it is never the full disk node | pass |
| two full disks plus a crash: no leader (no quorum can persist), and no safety violation | pass |
| correct Raft survives random faults: 20 + 20 mixed seeds, 100 crash storm seeds | pass |
| the checker catches the missing log check | pass |
| the checker catches the forgotten vote (seed 85, found by the search below) | pass |

## 4. Measurements

### Time to elect the first leader (200 seeds)

| Election timeout | p50 ms | p99 ms | max ms | No leader in 5 s | Elections needed, p50 / max |
|---|---|---|---|---|---|
| random 150 to 300 ms | 176 | 261 | 264 | 0 of 200 | 1 / 2 |
| random 150 to 160 ms | 158 | 610 | 773 | 0 of 200 | 2 / 18 |
| fixed 150 ms | none | none | none | **200 of 200** | never |

Randomness is not a detail. With identical timeouts every node becomes a candidate at the same instant, votes for itself, and no one ever wins. A 10 ms spread works most of the time but has a long tail (18 elections in the worst run). A spread as wide as the timeout itself settles in one or two rounds. The Raft paper's own measurements (section 9.3) show the same shape.

### Failover after a leader crash at t = 2 s (200 seeds)

| Message loss | p50 ms | p99 ms | max ms |
|---|---|---|---|
| 0% | 158 | 311 | 325 |
| 10% | 164 | 610 | 678 |
| 30% | 287 | 1759 | 1783 |

The floor is the minimum election timeout plus a round trip: followers have to notice the silence before they can act. Loss mostly hurts the tail, because a lost vote means a split vote and a whole new timeout. At 30% loss the p99 misses the 1 s requirement, so a lossy network needs a longer timeout, which raises the floor. That is the trade-off etcd's tuning guide is about.

### A follower is cut off for 3 s and comes back (200 seeds)

| PreVote | Runs where the healthy leader was deposed | Cut-off node's term on return, median |
|---|---|---|
| off | **200 of 200** | +13 |
| on | 0 of 200 | +0 |

Without PreVote, the isolated node times out over and over, raising its term each time. When it returns, every node sees the higher term and the working leader steps down for no reason. That is one needless election per network blip, in every single run.

### The leader and one follower are cut off from the other three for 3 s (200 seeds)

| CheckQuorum | Old leader still believes it leads, median | Commands it accepted that were later thrown away, all runs |
|---|---|---|
| off | 3000 ms (the whole partition) | 12203 |
| on | 570 ms | 2163 |

No committed data was lost: the minority leader can never commit, so no client was told those commands succeeded. The danger is reads. A leader that answers reads from its own state for 3 seconds serves stale data. CheckQuorum cuts that window to about two check periods, and ReadIndex or leases close it fully.

### Clock drift: node 0's clock runs fast, leader crashed every 2 s for 20 s (100 seeds)

| Node 0 clock rate | Share of elections node 0 won | Safety violations |
|---|---|---|
| 1.0x | 20% (fair share) | 0 |
| 1.5x | 46% | 0 |
| 2.0x | 50% | 0 |

A fast clock makes a node's election timer fire first, so it wins most elections. Safety is unaffected because Raft never compares clocks between nodes, only terms. Clocks matter for liveness and for lease based reads, never for election safety.

## 5. Random fault search (100 seeds per cell)

Mixed nemesis: crashes, partitions, isolation, slow links, bursts of 30% loss, and a clock at 0.5x or 2x, plus 5% base loss, 10 s runs. Crash storm: crash and restart within 1 to 20 ms, every 20 to 200 ms, with 5 to 50 ms delays and 150 to 160 ms timeouts, 5 s runs. Clients send a command every 50 ms to every node that thinks it leads.

| Variant | Nemesis | Runs with a violation | First failing seed | First violation |
|---|---|---|---|---|
| correct | mixed | 0 of 100 | | |
| correct | crash storm | 0 of 100 | | |
| correct + PreVote + CheckQuorum | mixed | 0 of 100 | | |
| correct + PreVote + CheckQuorum | crash storm | 0 of 100 | | |
| BUG: votedFor not persisted | mixed | 0 of 100 | | |
| BUG: votedFor not persisted | crash storm | **2 of 100** | 85 | two leaders in term 14: 2 and 3 |
| BUG: no log up-to-date check | mixed | **87 of 100** | 0 | leader 3 term 4 is missing committed entry 91 |
| BUG: no log up-to-date check | crash storm | 0 of 100 | | |

Two lessons:

- **The nemesis has to match the bug.** The forgotten vote only matters if a node crashes and restarts in the gap between two candidates' requests in the same term. Random partitions never found it in 100 runs. A crash storm found it in 2 of 100. The trace for seed 85 shows node 1 crashing and restarting within 3 ms while candidates 2 and 3 were both asking for votes in term 14, and then both of them winning. Without the log check, the bug needs logs that differ, which a partition creates and a crash storm (no partitions) does not.
- **"0 of 100" is evidence, not proof.** 0 failures in 100 runs only tells you the failure rate under that nemesis is probably below about 3%. The planted bug failed only 2% of the time. The defence is many more seeds, a nemesis aimed at each invariant, and keeping every failing seed as a regression test, which `test_raft.py` does for seed 85.

## 6. Where this breaks

- **Disks that lie.** The model assumes `persist` is durable once it returns. A real disk or filesystem that acknowledges before data is on stable storage gives the same failure as `persist_vote=False`.
- **Full disks count as failed nodes.** Two full disks in a 5 node cluster use up the whole fault budget: one more crash and there is no leader (tested).
- **Asymmetric partitions.** A node that can send but not receive keeps timing out and, without PreVote, keeps disrupting the cluster. `faultsim` can model it with `block(a, b)`; this example does not test it.
- **Membership changes, snapshots and reads** are not implemented, and each has its own known pitfalls.
- **Throughput.** One entry per append round trip, no pipelining. Fine for configuration, not for a data path.

## 7. What production systems do differently

- **etcd** (the go.etcd.io/raft library) has `PreVote` and `CheckQuorum` options, defaults to a 100 ms heartbeat and a 1000 ms election timeout, and serves linearizable reads with ReadIndex instead of reading the leader's local state. Its library is deliberately only the state machine; the application supplies storage and networking, the same split as `raft.py` and `faultsim`.
- **Kafka** replaced ZooKeeper with KRaft, a Raft based controller quorum, for cluster metadata. Partition data itself is replicated with a different, leader based ISR protocol.
- **Raft vs other options.** ZooKeeper uses Zab; many databases use Multi-Paxos or Raft per shard (Raft groups per range in CockroachDB and TiKV). The core election idea (terms or ballots, majority votes, a durable promise not to vote twice) is the same in all of them.
- **Testing.** FoundationDB built its database inside a deterministic simulator from the start, and Jepsen tests real clusters with real faults. This example is a very small version of the first approach.

## 8. Interview summary

> Raft elects a leader per term. A follower that hears nothing for a randomized timeout becomes a candidate, increments its term, and asks for votes; a node votes at most once per term, only for a candidate whose log is at least as up to date, and writes that vote to disk before replying. A majority wins, and since two majorities always overlap, there can be only one leader per term. Randomized timeouts are essential: with fixed timeouts I measured no leader at all in 200 of 200 runs. Failover is about one election timeout plus a round trip, 158 ms at p50 with a 150 to 300 ms timeout, and message loss stretches the tail. In production I would turn on PreVote, because without it a node returning from a partition deposed the leader in every run, and CheckQuorum, so a leader in a minority steps down in about half a second instead of serving stale reads. I tested safety with a deterministic simulator that checks the invariants after every event, and confirmed it catches two planted bugs, one of which only showed up under a crash storm.

## Reproduce

```
python3 -m unittest -v test_raft.py           # about 25 s
python3 experiments.py                        # all sections, 200 seeds, about a minute
python3 experiments.py torture --seeds 400    # more seeds for the fault search
```
