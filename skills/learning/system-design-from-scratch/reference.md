# system-design-from-scratch: reference

1. [Numbers](#1-numbers)
2. [Measuring honestly](#2-measuring-honestly)
3. [Invariants by system](#3-invariants-by-system)
4. [What production systems do](#4-what-production-systems-do)
5. [Script reference](#5-script-reference)
6. [Design doc template](#6-design-doc-template)

## 1. Numbers

### Rough latency ladder

Orders of magnitude only. They move with hardware, so measure the ones the design depends on.

| Operation | Rough time |
|---|---|
| Main memory reference | ~100 ns |
| Python dict lookup plus a little arithmetic | ~0.1 to 1 µs |
| Read 1 MB sequentially from memory | ~10 to 100 µs |
| SSD random read | ~20 to 100 µs |
| fsync on an SSD | ~0.1 to 10 ms, depends heavily on the device |
| Round trip inside one data center | ~0.2 to 0.5 ms |
| HDD seek | ~5 to 10 ms |
| Round trip across a continent | ~30 to 80 ms; across an ocean ~100 to 200 ms |

### Formulas worth knowing by heart

- **Little's law:** items in the system = arrival rate x time in the system (L = λW). 1000 requests per second that each take 50 ms means 50 in flight. Use it to size thread pools, connection pools and queues.
- **Queueing (M/M/1):** time in system = service time / (1 - utilization). At 50% busy, latency doubles. At 90%, it is 10x. This is why systems are sized for 50 to 70% peak utilization, and why `loadgen --rates` sweeps show a knee.
- **Fan-out tail:** if one call has a 1% chance of being slow, a request that waits for 100 such calls is slow with probability 1 - 0.99^100 = 63% ("The Tail at Scale", Dean and Barroso, 2013). Hedged requests and timeouts exist because of this line.
- **Quorums:** with N replicas, writes to W and reads from R overlap when R + W > N. Majority quorums (N = 2f + 1) survive f failures.
- **Availability in series and parallel:** two dependencies at 99.9% in series give 99.8%. Two independent replicas at 99% in parallel give 99.99%, if their failures really are independent, which they often are not.
- **Storage per day:** writes per second x bytes per write x 86,400. 1000 writes/s of 1 KB is about 86 GB per day before replication.
- **ID space:** base62 with 7 characters is 62^7, about 3.5 x 10^12. A 64 bit id has 1.8 x 10^19 values.

### Back-of-envelope checklist

For each design, write a line for each of these, with the arithmetic:

1. Peak QPS (average x 2 to 10 for peaks) and read to write ratio.
2. Data size now and in 3 years, with replication.
3. Memory: hot set size, bytes per entry including overhead (Python objects are about 50 to 100 bytes plus contents; a compact C struct can be 10x smaller).
4. Per request work: round trips, disk syncs, bytes moved.
5. Nodes needed = peak QPS / safe QPS per node (at about 60% utilization).
6. The one number that rules out the naive design.

## 2. Measuring honestly

### Open loop vs closed loop

- **Closed loop** (`--concurrency N`): each worker waits for a reply before sending again. When the system stalls for 1 s, the workers stop sending, so one slow sample is recorded instead of hundreds. This is **coordinated omission** (Gil Tene's term). Use closed loop to find maximum throughput, never for latency claims.
- **Open loop** (`--rate R`): requests start on a fixed schedule and latency is counted from the scheduled start. A stall delays every request scheduled during it, as it would for real users. This is how wrk2 works.
- `loadgen` reports **service time** (send to reply) next to **latency** (scheduled start to reply). A big gap between them means requests were queueing.
- `loadgen` reports **generator lag**. Python's event loop on a busy laptop can fall behind by tens of milliseconds. If generator lag p99 is close to the latency p99, the result measures the generator. Run fewer requests per second, close other programs, or run the generator on another machine.

### Percentiles

- Never average percentiles across runs or nodes. Merge the histograms and read the percentile from the merged one (`Histogram.merge`).
- Report p50, p99, p99.9 and max, and how many samples: a p99.9 from 2,000 samples rests on 2 values.
- HdrHistogram's idea: buckets of fixed relative width, so error is bounded as a fraction (0.8% with `sub_bits=7`) across microseconds to minutes, and memory grows only with the log of the range.

### Real time vs simulated time

- Use **simulated time** (`faultsim`, or a manual clock) for correctness, timing logic, and anything distributed. It is deterministic, fast (minutes of cluster time per second), and immune to machine noise.
- Use **real time** (`loadgen`) for cost per operation and throughput of real code. Record the machine's load average (`uptime`) next to the numbers.
- Warm up before measuring (`--warmup`): caches, JIT-less Python's first allocations, connection setup.

### Proving a property

- **Property tests:** generate hundreds of seeded random inputs that mix steady load, bursts at one instant and idle gaps. Check the invariant on every one.
- **Oracle tests:** compare against a brute force implementation that is obviously correct and slow.
- **Fault search:** `random_faults` across many seeds, invariants checked after every event. Then aim a nemesis at each invariant (for example crash and restart within milliseconds to test durability of promises; partitions to test log divergence).
- **Mutation check:** break the code on purpose (skip a persist, remove a check). If no test fails, the tests are too weak.
- **Statistics of "no failures":** 0 failures in N independent runs puts the failure rate below about 3/N with 95% confidence (the rule of three).

## 3. Invariants by system

For each: the smallest honest build, what must always hold, what to load, and what to break.

### Rate limiter
- Build: token bucket (or GCRA), fixed window, sliding log, sliding window counter. Worked example in `examples/rate-limiter`.
- Invariants: token bucket admits at most burst + rate x T in any interval T; sliding log never has more than the limit in any window; long run admitted rate equals the limit under overload (no under-admission); keys independent.
- Load: open loop at 2 to 5x the limit; measure admitted rate and added latency.
- Faults: backwards clock jump, several gateways sharing one limit with the store partitioned or slow (fail open vs fail closed).

### Job queue with retries and visibility timeouts
- Build: `enqueue`, `receive` (hides the job for a visibility timeout and returns a receipt), `ack(receipt)`, `nack`, retry with exponential backoff and jitter, dead letter queue after K attempts. Workers as faultsim nodes.
- Invariants: every enqueued job is eventually acked or dead-lettered (no loss); a job is never visible to two workers at once while its timeout has not expired; an ack with a stale receipt does nothing; attempts never exceed K.
- What delivery really is: at least once. A worker that finishes after its timeout runs the job a second time somewhere else. Make handlers idempotent and measure the duplicate rate as a function of timeout vs job duration.
- Load: jobs per second at fixed worker count; queue depth and age of the oldest job over time (Little's law tells you the steady depth).
- Faults: kill a worker mid-job, pause a worker longer than the timeout (the classic GC pause), drop acks, clock skew between queue and workers.

### Consistent hashing ring
- Build: ring of hashed points with V virtual nodes per server, lookup by binary search (`bisect`). Compare with modulo hashing, rendezvous hashing and jump consistent hash.
- Invariants: lookups are deterministic; adding a server moves only keys that now map to it (about 1/(n+1) of keys); removing one moves only its keys.
- Measure: keys moved per change (modulo hashing moves almost all of them); load imbalance max/mean vs V (it shrinks as V grows, at the cost of a bigger ring); lookup cost.
- Faults: a node flapping in and out; hot keys (one key with 10% of traffic breaks any hash based balance; show it).

### LSM tree key-value store
- Build: write-ahead log, sorted memtable, flush to immutable sorted files (SSTables) with a sparse index and a bloom filter, reads that check memtable then newest to oldest files, tombstones for deletes, and size tiered or leveled compaction.
- Invariants: a get returns the latest put or nothing after a delete, compared to a dict oracle over random operation sequences, including across flushes, compactions and restarts; after a crash, every acknowledged write is present.
- Measure: write, read and space amplification; read latency with and without bloom filters (about 10 bits per key gives roughly a 1% false positive rate); the effect of compaction on p99 write latency.
- Faults: crash between writing an SSTable and updating the manifest, crash during compaction, torn last WAL record, disk full during flush.

### Write-ahead log
- Build: append records with length and checksum (CRC32 from `zlib`), fsync policy (every write, group commit every N ms, never), recovery that stops at the first bad record, and segment rotation.
- Invariants: recovery returns exactly a prefix of what was appended; every write acknowledged under the "every write" policy survives a crash; recovery is idempotent.
- Measure: appends per second and p99 for each fsync policy (group commit trades a few ms of latency for many times the throughput).
- Faults: crash at every byte offset of the last record (truncate the file in a loop), flip a bit mid-file, fsync that fails. Read about PostgreSQL's 2018 "fsyncgate": after a failed fsync on Linux, retrying can falsely succeed because dirty pages were dropped, so PostgreSQL now panics instead of retrying.

### Raft leader election
- Build: worked example in `examples/raft-election`.
- Invariants: at most one leader per term; no two nodes commit different entries at one index; a new leader holds every committed entry.
- Measure: time to first leader, failover time vs loss, needless elections after a partition heals.
- Faults: leader crash, minority partition, crash storms (finds unpersisted votes), clock drift (liveness only).

### Cache with eviction policies
- Build: LRU (`OrderedDict`), LFU, FIFO, CLOCK, and optionally S3-FIFO or W-TinyLFU. Add TTL.
- Invariants: size never exceeds capacity; a get never returns an expired or evicted value; LRU evicts exactly the least recently used (compare with an oracle).
- Measure: hit ratio vs cache size on a Zipf workload (`random.paretovariate` or a Zipf sampler) and on a scan-heavy workload (scans flush LRU; frequency based policies resist); cost per operation.
- Faults: cache stampede when a hot key expires (N simultaneous misses all hit the database); fix with request coalescing or probabilistic early refresh and measure the backend QPS spike before and after.

### Pub/sub
- Build: topics, partitions as append-only lists, subscribers with offsets, consumer groups that split partitions, at-most-once vs at-least-once delivery depending on when the offset is committed.
- Invariants: per partition order is preserved; with commit-after-process, no acknowledged message is lost; in a group, each partition has at most one owner at a time.
- Measure: end-to-end latency and throughput vs number of subscribers; slow consumer behaviour (unbounded buffer vs backpressure vs dropping).
- Faults: subscriber crash before and after committing; broker restart; duplicate delivery rate; rebalance during traffic.

### URL shortener at scale
- Build: id generation (counter ranges handed out per node, or Snowflake-style time + node + sequence), base62 encoding, a store, redirect handler with a cache.
- Invariants: no two long URLs ever get the same code (unless deduplication is intended); a created code always resolves; ids from different nodes never collide.
- Measure: redirect p99 with and without cache at a 100:1 read to write ratio; hit ratio on Zipf traffic; how many codes 7 base62 characters give at the expected creation rate.
- Faults: an id node crashes after handing out a range (gaps are fine, duplicates are not); clock moves backwards under Snowflake ids (must wait or refuse); cache node lost (cold start load on the store).

## 4. What production systems do

Only what their public docs and papers describe. Check the current docs before quoting a default value in a design doc.

**Redis.** Executes commands on one main thread, which makes each command atomic and makes Lua scripts a common way to do read-modify-write (token buckets). Eviction policies include `allkeys-lru`, `allkeys-lfu` and `volatile-ttl`; its LRU is approximate, sampling a few keys (`maxmemory-samples`) rather than keeping a full list. Persistence is RDB snapshots and/or an append-only file with `appendfsync` set to `always`, `everysec` or `no`, the same durability vs throughput choice as the WAL above. Plain Pub/Sub does not store messages: a subscriber that is down misses them. Streams keep messages, support consumer groups, and track delivered but unacknowledged entries, which can be claimed by another consumer after a timeout (visibility timeout in another form).

**Kafka.** Each partition is an append-only log split into segment files, ordered only within the partition. Consumers in a group split partitions and commit offsets, so delivery semantics depend on when they commit. Replication uses a leader per partition and an in-sync replica set; `acks=all` with `min.insync.replicas` decides how many copies a write needs. Retention is by time or size, or by key with log compaction. Cluster metadata moved from ZooKeeper to KRaft, a Raft based controller quorum.

**Cassandra.** A token ring with virtual nodes (`num_tokens`) and a Murmur3 partitioner places data, following the Dynamo paper (DeCandia et al., 2007). Writes go to a commit log and a memtable, which flushes to immutable SSTables merged by compaction (size tiered, leveled, or time window), each with a bloom filter: an LSM tree. Consistency is tunable per request (ONE, QUORUM, ALL). Deletes are tombstones kept for `gc_grace_seconds`. Gossip spreads membership, a phi accrual failure detector outputs a suspicion level instead of yes or no, and hinted handoff and read repair fix replicas that missed writes.

**etcd.** Raft for consensus, with `PreVote` and `CheckQuorum` available in its Raft library, a default heartbeat of 100 ms and election timeout of 1000 ms, a WAL plus snapshots on disk, MVCC with revisions, and leases with TTLs. Its docs recommend 3 or 5 members and warn that slow disks (fsync latency) cause leader elections.

**Amazon SQS.** Standard queues deliver at least once; a received message is hidden for a visibility timeout (default 30 seconds, up to 12 hours) and reappears if not deleted. A redrive policy moves a message to a dead letter queue after `maxReceiveCount` receives. FIFO queues add ordering per message group and deduplication within a 5 minute window.

**LevelDB and RocksDB.** LSM trees with leveled compaction, where each level is about 10 times larger than the one above (RocksDB's default multiplier), bloom filters per file, and a WAL. RocksDB's docs discuss the read, write and space amplification trade-off between compaction styles. The design traces back to O'Neil et al.'s LSM-tree paper (1996) and Google's Bigtable paper (2006).

**Caches.** Memcached uses a segmented LRU. Caffeine (Java) uses W-TinyLFU (Einziger et al.), which admits new items only if they look more popular than the eviction victim. S3-FIFO (SOSP 2023) showed simple FIFO queues can match or beat LRU on many real traces.

**IDs.** Twitter's Snowflake packs a timestamp, a machine id and a per-millisecond sequence into 64 bits, so ids are roughly time ordered and need no coordination.

**Testing distributed systems.** FoundationDB runs its whole database inside a deterministic simulator and explores faults by seed, the approach `faultsim` copies in miniature. Jepsen (Kyle Kingsbury) tests real deployments with real partitions and clock skew and checks histories for consistency violations.

**Papers worth citing.** Raft (Ongaro and Ousterhout, 2014) and Ongaro's PhD thesis (PreVote, membership changes); Dynamo (2007); Bigtable (2006); The Tail at Scale (2013); Consistent hashing (Karger et al., 1997); Jump consistent hash (Lamping and Veach, 2014); Maglev (2016).

## 5. Script reference

### loadgen.py

```
python3 scripts/loadgen.py --target file.py:func --rate 1000 --duration 10
python3 scripts/loadgen.py --target file.py:func --concurrency 32 --duration 10
python3 scripts/loadgen.py --url http://127.0.0.1:8080/x --rates 500,1000,2000 --duration 5 --json out.json
python3 scripts/loadgen.py --target builtin:queue --rates 200,500,800,900 --duration 5   # see the knee
```

- Targets: `file.py:function`, `module:function`, `builtin:queue` (a single-server queue with 1 ms exponential service time), or `--url`. A target function may be sync or async; if it takes one argument it receives the request number. Short bool, int or str return values are counted as outcomes; exceptions are errors; HTTP 5xx is an error, other statuses are outcomes.
- Flags: `--warmup` seconds (default 1), `--max-inflight` (open loop sheds beyond this and reports it), `--connections` (HTTP pool), `--threads` (run a blocking sync target in a thread pool), `--json`, `--plot`.
- Library: `run_open(fn, rate, duration, warmup)`, `run_closed(fn, concurrency, duration, warmup)`, `HttpTarget(url)`, `Histogram`. Each run returns a `Result` with `.report()`, `.to_dict()`, `.latency`, `.service`, `.send_lag`, `.outcomes`, `.errors`, `.per_second`.
- Limits: one Python process, one event loop. On a laptop expect a few thousand requests per second for HTTP and tens of thousands for in-process sync targets before generator lag dominates. For more, run several copies and merge their JSON histograms, or use a compiled tool.

### faultsim.py

```python
from faultsim import Sim, Node, DiskFull, InvariantViolation, random_faults

class MyNode(Node):
    def on_start(self): ...               # boot and after every restart; reload from self.load()
    def on_message(self, src, msg): ...   # msg is plain data (dict, list, tuple, numbers, strings)

sim = Sim(seed=1, delay=(0.001, 0.005), drop=0.0, duplicate=0.0)
nodes = [sim.add(MyNode(i)) for i in range(5)]
sim.invariant("name", lambda s: None if ok else "what broke")   # checked after every event
sim.at(2.0, sim.crash, 0); sim.at(3.0, sim.restart, 0)
sim.at(4.0, sim.partition, [0, 1], [2, 3, 4]); sim.at(6.0, sim.heal)
random_faults(sim, until=20)            # seeded nemesis
sim.run(until=20)
```

- Node helpers: `send`, `broadcast`, `peers`, `set_timer(local_seconds, fn, *args)` (returns a handle with `.cancel()`), `clock()` (skewed wall clock), `persist(key, value)` (raises `DiskFull`), `load(key, default)`, `log(...)`, `self.rng` (per node, seeded).
- Network: `set_link(a, b, delay=..., drop=...)`, `partition(*groups)`, `isolate(n)`, `block(a, b)` (one way), `heal()`. Messages in flight when a partition starts are lost.
- Clocks and disks: `set_clock(n, offset=, drift=)` (drift 2.0 makes the node's timers fire twice as fast), `jump_clock(n, seconds)`, `set_disk_full(n)`.
- Crashes cancel timers and drop the inbox; the `disk` dict survives. Model "acknowledged but not durable" by persisting later than you reply.
- Debugging: on a broken invariant the exception names the seed, the time and the last 30 trace lines. `sim.stats` counts sent, delivered, dropped, blocked, lost in flight, crashes and more.
- Tuples are treated as immutable when copying messages and disk values, so do not put lists inside tuples.

## 6. Design doc template

```
# <System>: <the trade-off in a few words>

## 1. Requirements and numbers        table + back-of-envelope arithmetic
## 2. What was built                  files, components, what was left out
## 3. Invariants and tests            each property, how it is checked, result; bugs the tests found
## 4. Load results                    commands, tables, machine load; what the knee is
## 5. Fault results                   scenario table; one paragraph per surprise
## 6. Decision                        the chosen design and the reason, in terms of the numbers
## 7. Where it breaks                 the first limit you would hit, and at what scale
## 8. What production systems do      cited, with how they differ from this build
## 9. Interview summary               five sentences that use the measured numbers
## Reproduce                          exact commands
```

A good interview summary names the requirement that drove the choice, the design, one alternative and why it lost (with a number), the main failure mode and its mitigation, and one thing measured that surprised you.
