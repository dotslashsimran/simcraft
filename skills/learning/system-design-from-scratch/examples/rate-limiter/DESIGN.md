# Rate limiter: token bucket vs sliding windows

A worked example of the skill's full loop: requirements and numbers, five small limiters, property tests, load tests, a fault test across three gateways, and the trade-offs. Every number below came from running the code in this folder. The commands to reproduce them are at the end.

## 1. Requirements and numbers

The system: an API gateway that limits each API key to 100 requests per second.

| Requirement | Number | Why |
|---|---|---|
| Keys | 10 million | active API keys or user ids |
| Limit | 100 per second per key | the product rule |
| Peak traffic | 1 million requests per second, all keys | sizing for the store |
| Added latency | under 1 ms at p99 | the limiter sits on every request |
| Accuracy | never more than 2x the limit; under-admitting is a bug too | customers pay for the limit |
| When the limiter is unreachable | a choice, see section 6 | fail open or fail closed |

Back of the envelope, before writing code:

- **Memory.** If each key needs about 100 bytes, 10 million keys need about 1 GB. That fits in one machine's RAM. A sliding log stores up to 100 timestamps per key at 8 bytes each, so at least 8 GB before overhead. That alone rules out the exact algorithm at this scale unless the limit is small.
- **Throughput.** 1 million decisions per second. In process, a decision is a dictionary lookup and some arithmetic, about 1 microsecond in Python, so one core could in theory make about 1 million decisions per second. A shared store is different: every decision is a network round trip, and a single threaded store instance handles on the order of 100 thousand simple commands per second. So a central design needs about 10 shards, split by key.
- **Latency.** A round trip inside one data center is roughly 0.2 to 0.5 ms. A central limiter spends about half of the 1 ms budget on the network alone. A local limiter spends nothing.

## 2. What was built

`limiters.py`, about 175 lines. All five share `allow(key) -> bool`, take an injectable clock, and count only admitted requests.

| Algorithm | State per key | Idea |
|---|---|---|
| Token bucket | 2 numbers | bucket of `burst` tokens refilled at `rate`; refill computed lazily |
| GCRA | 1 number | the same bucket stored as one "theoretical arrival time" |
| Fixed window | 2 numbers | count per aligned second, reset at each edge |
| Sliding log | up to `limit` timestamps | exact: count admitted requests in the last W seconds |
| Sliding window counter | 3 numbers | previous count weighted by overlap, plus current count |

`server.py` wraps any of them in a small asyncio HTTP server (200 or 429).

## 3. Properties and the tests that check them

`test_limiters.py`, 13 tests, each run over 200 seeded random traces that mix steady streams, bursts at a single instant and idle gaps. All pass.

| Property | How it is checked |
|---|---|
| Token bucket admits at most `burst + rate * T` in any interval of length T | one pass over admitted times: `(j + 1 - rate * t_j) + max over i of (rate * t_i - i) <= burst` |
| Token bucket's long run rate equals `rate` | offer 10x for 100 s, admitted is `rate * 100 + burst` within 1 |
| GCRA and token bucket make identical decisions | same 200 traces, decisions compared one by one |
| Sliding log never has more than `limit` in any window | two pointer scan |
| Sliding log admits exactly when there is room | compared to a brute force oracle on every request |
| Fixed window respects each aligned window, but lets 2x through across an edge | 100 at 0.999 s and 100 at 1.000 s are all admitted |
| Sliding counter keeps any window below 2x | worst seen on random traces: 1.80x |
| Keys are independent, and an idle key recovers its full allowance | all five |
| A backwards clock jump does not lock out a token bucket | wall clock stepped back one hour |
| A backwards clock jump does lock out GCRA | documented weakness, see section 7 |

The clock jump tests found a real bug in the first draft. The token bucket kept the larger of the old and new timestamps, so after the clock went back an hour it gave no tokens for an hour. The fix is to treat negative elapsed time as zero and resync.

## 4. Accuracy under four traffic patterns

Simulated clock, limit 100 per second, token bucket and GCRA with burst 100. Deterministic.

| Pattern | Algorithm | Admitted | Ideal | Worst 1 s window |
|---|---|---|---|---|
| steady 2x | token_bucket | 1099 | 1000 | 199 |
| steady 2x | gcra | 1099 | 1000 | 199 |
| steady 2x | fixed_window | 1000 | 1000 | 101 |
| steady 2x | sliding_log | 1000 | 1000 | 100 |
| steady 2x | sliding_counter | 991 | 1000 | 100 |
| poisson 2x | token_bucket | 1099 | 1000 | 196 |
| poisson 2x | fixed_window | 1001 | 1000 | 132 |
| poisson 2x | sliding_log | 1000 | 1000 | 100 |
| poisson 2x | sliding_counter | 987 | 1000 | 104 |
| bursty (200 in 0.2 s, every second) | token_bucket | 1019 | 1000 | 120 |
| bursty | fixed_window | 1000 | 1000 | 101 |
| bursty | sliding_log | 1000 | 1000 | 100 |
| bursty | sliding_counter | **574** | 1000 | 100 |
| boundary attack (100 just before each edge, 100 on it) | token_bucket | 900 | 900 | 200 |
| boundary attack | fixed_window | 1000 | 900 | 200 |
| boundary attack | sliding_log | 900 | 900 | 100 |
| boundary attack | sliding_counter | 893 | 900 | 197 |

GCRA matched token bucket on every row, as the test requires. What the table shows:

- **A token bucket with burst = limit is not "100 per second".** Its real guarantee is `burst + rate * T`, which is 200 in any one second window. If the contract is "never more than 100 in any second", the burst must be small, and then legitimate bursty clients get rejected. Choosing the burst is the actual design decision.
- **Fixed window lets 2x through at every edge**, as expected. It also over-admitted by 11% overall in the boundary attack.
- **The sliding window counter under-admits bursty traffic badly: 57% of the allowed rate.** It assumes last second's requests were spread evenly. When they all came in the first 0.2 s, it still counts most of them as "inside the window" for most of this second. This is the opposite failure to the one people usually warn about.
- **The sliding window counter can also be fooled the other way.** In the boundary attack it admitted 100 at 0.999 s and 99 at 1.999 s: two bursts exactly one window apart, 199 inside a closed one second interval. It assumed the 100 were spread across the previous window, so by 1.999 s it thought almost all of them had left.
- **The sliding log was exact in every pattern.** Its cost is memory (section 5).

## 5. Cost and memory

Cost per `allow()` call in CPython 3.9 on a laptop, measured with `timeit`, load average about 3 on 8 cores. An earlier run at load average 30 was about 4 times slower across the board, which is why the machine's load belongs next to any real time number.

| Algorithm | 1 hot key, ns | 100k keys round robin, ns |
|---|---|---|
| token_bucket | 521 | 744 |
| gcra | 416 | 586 |
| fixed_window | 364 | 497 |
| sliding_log | 336 | 565 |
| sliding_counter | 552 | 712 |

Memory per key with each key at its full allowance (limit 100), measured with `tracemalloc`. Deterministic.

| Algorithm | Bytes per key | 10 million keys |
|---|---|---|
| token_bucket | 181 | 1.8 GB |
| gcra | 110 | 1.1 GB |
| fixed_window | 158 | 1.6 GB |
| sliding_log | 1237 | 12.4 GB |
| sliding_counter | 166 | 1.7 GB |

Every algorithm costs under a microsecond, so speed does not decide between them. Memory does: the sliding log needs 7 to 11 times more, and it grows with the limit. GCRA is the smallest because it stores one float.

## 6. Load tests

**In process, open loop.** Token bucket at 1000 per second, offered 5000 per second for 5 s with `loadgen.run_open`:

```
token_bucket  mode=open  target=5000/s
  completed 25000 in 5.00s = 5000.0/s
  generator lag p99 0.016 ms, max 0.5 ms
  outcomes: False=20000 (80.0%), True=5000 (20.0%)
  ms                   p50       p90       p95       p99     p99.9       max
  latency            0.010     0.016     0.017     0.020     0.034     0.539
  service time       0.001     0.001     0.002     0.002     0.006     0.020
```

Admitted rate was exactly 1000.0 per second under 5x overload. The call itself takes 1 to 2 microseconds up to p99.

The same test run earlier while the laptop was overloaded (load average 30) reported a p99 latency of 49 ms, with service time still 18 microseconds and generator lag p99 at 48 ms. The 49 ms was the load generator falling behind its own schedule, not the limiter. That is why `loadgen` prints generator lag: without it, that run would have read as "the limiter has a 49 ms p99".

**Over HTTP.** `server.py` with a token bucket at 1000 per second, driven by `loadgen --url`, same machine, load average about 3:

| Run | Throughput | Admitted (200) | p50 ms | p99 ms | p99.9 ms | max ms |
|---|---|---|---|---|---|---|
| closed loop, 16 connections | 23,795 per s | 5000 in 5 s | 0.66 | 0.77 | 0.84 | 1.3 |
| open loop at 50% of that (11,897 per s) | 11,897 per s | 5001 in 5 s | 0.24 | 0.32 | 14.5 | 23.3 |
| open loop at 80% (19,036 per s) | 19,036 per s | 5000 in 5 s | 0.52 | 0.76 | 7.3 | 16.2 |

What it shows:

- The limiter held at exactly 1000 per second at every load, while rejecting up to 96% of requests.
- One Python process with one asyncio loop serves about 24,000 requests per second. The limiter is about 0.5 microseconds of each request's roughly 40 microseconds of CPU; the HTTP parsing and the event loop are the cost.
- Closed loop reports a tidy p99.9 of 0.84 ms. Open loop at half the load shows a p99.9 of 14.5 ms. Those are short stalls (garbage collection, the OS scheduling something else) that the closed loop hid, because its 16 workers simply stopped sending while the server was stalled. This is coordinated omission, measured.
- p50 doubles from 50% to 80% load (0.24 to 0.52 ms), the queueing knee that section 1 of the reference predicts.

## 7. Faults: three gateways, one global limit

Three gateways share a global limit of 300 per second. Clients send 600 per second, and 70% of it lands on gateway g0 (skew is normal: one big customer, one hot region). Simulated with `faultsim`, seed 1, one way network delay 0.5 to 2 ms. The fault lasts from t=4 s to t=7 s.

| Strategy | Fault | Admitted per s, normal | Admitted per s, during fault | Decision p50 ms | p99 ms |
|---|---|---|---|---|---|
| local split: each gateway gets 100/s | none | 273 | 274 | 0 | 0 |
| local split | g0 partitioned | 273 | 274 | 0 | 0 |
| local, rebalanced every 0.5 s by gossip | none | 301 | 299 | 0 | 0 |
| local, rebalanced | g0 partitioned | 301 | 303 | 0 | 0 |
| central limiter, fail open | none | 300 | 300 | 2.54 | 3.79 |
| central limiter, fail open | g0 partitioned | 300 | **593** | 2.77 | 50.00 |
| central limiter, fail open | links slow (40 to 120 ms) | 300 | **591** | 2.93 | 50.00 |
| central limiter, fail closed | none | 300 | 300 | 2.54 | 3.79 |
| central limiter, fail closed | g0 partitioned | 300 | **177** | 2.77 | 50.00 |
| central limiter, fail closed | links slow | 300 | **0** | 2.93 | 50.00 |

What it shows:

- **A static split under-admits under skew.** g0 is capped at 100 while it receives 420, and the other two waste their unused share. The total was 273, not 300.
- **Rebalancing by demand fixes that, and survives a partition well.** A cut-off gateway keeps its last share, so the total stays near the limit. Its error is bounded by how stale the shares are.
- **A central limiter is exact when healthy and costs a round trip on every request.** Here that was 2.5 ms at p50, already more than the 1 ms budget from section 1.
- **When it is unreachable, fail open doubles the admitted rate and fail closed takes the service down.** With slow links, fail closed admitted nothing for three seconds because every answer arrived after the 50 ms timeout. That is a full outage caused by a slow dependency, not a dead one.

## 8. Decision

For the requirements in section 1:

- **Algorithm:** token bucket stored as GCRA, with burst set from the contract (small if "never more than N per second" is strict, larger if clients are bursty by nature). It is O(1) in time and memory, exact for its own definition, and testable against a simple invariant.
- **Placement:** local decisions in each gateway, with shares rebalanced from a shared store every few hundred milliseconds. This keeps the decision off the network, stays near the global limit under skew, and degrades gently in a partition.
- **When the store is unreachable:** keep using the last known share (bounded over-admission) rather than failing open or closed.
- **Clocks:** monotonic clock in process. If the state ever lives in a shared store, use the store's clock for every decision so all gateways agree.

## 9. Where this breaks

- Sliding log at a limit of 10,000 per second per key: about 80 KB per key. Not viable at millions of keys.
- Rebalanced local limiting with a very large number of gateways: each gateway's share gets small, and a burst landing on one gateway is rejected even when the global budget is free.
- GCRA or a sliding log on a wall clock that steps backwards: GCRA locks the key out for the size of the jump (tested). Use a monotonic clock or a single shared clock.
- Fixed and sliding counters keyed by `int(now // window)`: if gateways' clocks disagree, they disagree about which window they are in.
- The HTTP server is one asyncio loop in one process, about 24,000 requests per second here. It is a test harness, not a gateway.

## 10. What production systems do differently

- **Redis.** The documentation for `INCR` describes a fixed window limiter pattern using `INCR` and `EXPIRE`. Token bucket or GCRA in Redis is usually a Lua script, so the read, refill and write happen atomically on the server, using the server's own clock.
- **Stripe** described running token bucket limiters on Redis in its 2017 engineering post "Scaling your API with rate limiters".
- **Cloudflare** described its sliding window counter in the 2017 post "How we built rate limiting capable of scaling to millions of domains". It reports that on its real traffic, about 0.003% of requests were wrongly allowed or limited. That is consistent with this experiment: the approximation is fine for traffic that is spread out, and wrong for traffic bunched at window edges, which real traffic across millions of clients rarely is.
- **Envoy** offers both a local rate limit filter (a token bucket in each proxy) and a global rate limit service that proxies call over gRPC, and its documentation suggests combining them: local limits absorb most load, the global service enforces the overall quota.
- **Shared limits** in production are almost always approximate and sharded by key. Nobody routes a million requests per second through one counter.

## 11. Interview summary

> I would use a token bucket, implemented as GCRA so each key is one timestamp, about 100 bytes, so 10 million keys fit in about 1 GB. The guarantee is burst plus rate times T, so I choose the burst from the contract. Fixed windows let 2x through at the edges, and a sliding log is exact but needs memory proportional to the limit, about 12 GB at 100 per second for 10 million keys. A sliding window counter is a good cheap approximation, but it under-admits bursty clients: I measured 57% of the allowed rate for clients that send everything in the first fifth of each second. To share a limit across gateways I would decide locally and rebalance shares from Redis every few hundred milliseconds, because a central check adds a round trip to every request, and when it is unreachable you must choose between doubling traffic and an outage. I would use a monotonic clock, or the store's clock, because a backwards step locks out GCRA keys.

## Reproduce

```
python3 -m unittest -v test_limiters.py
python3 bench.py                 # all sections, about a minute
python3 bench.py accuracy distributed memory    # the deterministic ones
python3 server.py --limit 1000 --port 8080 &
python3 ../../scripts/loadgen.py --url "http://127.0.0.1:8080/?key=a" --rate 1500 --duration 5
```
