# Latency report (closed-loop)

Target: `http://127.0.0.1:8099/`  
Requests: 4496 (4496 ok), achieved 224.8 req/s

| | p50 | p90 | p99 | p99.9 | max | mean |
|---|---|---|---|---|---|---|
| latency (from intended start) | 1.85 ms | 5.91 ms | 37.31 ms | 408.87 ms | 439.14 ms | 4.44 ms |
| service time (from actual send) | 1.85 ms | 5.91 ms | 37.31 ms | 408.87 ms | | |

**Warnings**

- Closed-loop mode: the client stops sending while the server is slow, so stalls are under-counted (coordinated omission). Use these numbers only to compare with an open-loop run.
- p99.9 rests on fewer than 10 samples above it (4496 requests; want 10001 or more). Treat it as a rough guide.
- p99 is 8x the mean. An average would hide this tail completely.
