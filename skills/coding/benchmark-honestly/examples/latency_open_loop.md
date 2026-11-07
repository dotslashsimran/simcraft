# Latency report (open-loop)

Target: `http://127.0.0.1:8099/`  
Requests: 4000 (4000 ok), achieved 200.0 req/s of 200.0 requested

| | p50 | p90 | p99 | p99.9 | max | mean |
|---|---|---|---|---|---|---|
| latency (from intended start) | 3.36 ms | 212.34 ms | 392.50 ms | 411.86 ms | 415.99 ms | 46.21 ms |
| service time (from actual send) | 2.14 ms | 209.73 ms | 391.82 ms | 410.90 ms | | |

**Warnings**

- The scheduler fell behind by up to 75.09 ms. The client machine cannot keep this rate steadily; results above that lag are suspect.
- p99.9 rests on fewer than 10 samples above it (4000 requests; want 10001 or more). Treat it as a rough guide.
- p99 is 8x the mean. An average would hide this tail completely.
