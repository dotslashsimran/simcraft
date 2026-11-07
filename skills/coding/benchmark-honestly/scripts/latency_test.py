#!/usr/bin/env python3
"""Open-loop latency test that does not hide stalls (avoids coordinated omission).

Standard library only (Python 3.9+).

    python3 latency_test.py --url http://127.0.0.1:8080/ --rate 200 --duration 30
    python3 latency_test.py --cmd "python3 handler.py" --rate 5 --duration 60
    python3 latency_test.py --url ... --closed-loop --concurrency 4   # the naive way, for comparison

Open loop (the default): requests are scheduled at fixed times, start + i/rate,
no matter how the server is doing. Latency is measured from the time the
request was SUPPOSED to start, so if the server stalls for 500 ms, every
request that should have gone out during the stall carries that wait. This is
what real users see, because they do not politely wait for the previous user
to finish.

Closed loop (--closed-loop): each worker sends a request, waits for the reply,
then sends the next. During a stall the client simply stops sending, so only a
handful of slow samples are recorded and the high percentiles look far better
than reality. That is coordinated omission. The mode exists so you can see the
difference on your own system.

The report gives p50/p90/p99/p99.9/max of latency (from intended start) and of
service time (from actual send), error counts, the achieved rate, and warnings
when the test itself was the bottleneck or the sample is too small for a
percentile.
"""
import argparse
import http.client
import json
import math
import os
import shlex
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

PCTS = [50, 90, 99, 99.9]


def pct(sorted_xs, q):
    """Nearest-rank percentile (the value at or below which q% of samples fall)."""
    if not sorted_xs:
        return float("nan")
    k = max(1, int(math.ceil(q / 100.0 * len(sorted_xs))))
    return sorted_xs[k - 1]


class HttpTarget:
    def __init__(self, url, method, body, timeout, headers):
        u = urllib.parse.urlsplit(url)
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.path = (u.path or "/") + ("?" + u.query if u.query else "")
        self.method, self.body, self.timeout = method, body, timeout
        self.headers = headers
        self.local = threading.local()

    def _conn(self):
        c = getattr(self.local, "conn", None)
        if c is None:
            cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
            c = cls(self.host, self.port, timeout=self.timeout)
            self.local.conn = c
        return c

    def call(self):
        """Returns None on success or an error label."""
        for attempt in (0, 1):
            c = self._conn()
            try:
                c.request(self.method, self.path, body=self.body, headers=self.headers)
                r = c.getresponse()
                r.read()
                if r.getheader("connection", "").lower() == "close":
                    c.close()
                    self.local.conn = None
                return None if r.status < 400 else "HTTP %d" % r.status
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                c.close()
                self.local.conn = None
                if attempt == 1:
                    return "connection reset"
            except Exception as e:
                c.close()
                self.local.conn = None
                return type(e).__name__
        return "unknown"


class CmdTarget:
    def __init__(self, cmd, shell, timeout):
        self.argv = cmd if shell else shlex.split(cmd)
        self.shell, self.timeout = shell, timeout

    def call(self):
        try:
            r = subprocess.run(self.argv, shell=self.shell, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=self.timeout)
            return None if r.returncode == 0 else "exit %d" % r.returncode
        except subprocess.TimeoutExpired:
            return "timeout"


def open_loop(target, rate, duration, warmup, workers):
    results = []  # (intended, sent, done, error)
    lock = threading.Lock()
    busy = [0]
    max_busy = [0]
    max_lag = [0.0]

    def job(intended):
        sent = time.perf_counter()
        with lock:
            busy[0] += 1
            max_busy[0] = max(max_busy[0], busy[0])
        err = target.call()
        done = time.perf_counter()
        with lock:
            busy[0] -= 1
            results.append((intended, sent, done, err))

    total = int((warmup + duration) * rate)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        start = time.perf_counter() + 0.05
        for i in range(total):
            intended = start + i / rate
            now = time.perf_counter()
            if intended > now:
                time.sleep(intended - now)
            else:
                max_lag[0] = max(max_lag[0], now - intended)
            pool.submit(job, intended)
    cut = start + warmup
    kept = [r for r in results if r[0] >= cut]
    return kept, {"max_in_flight": max_busy[0], "dispatch_lag_s": max_lag[0],
                  "test_span_s": duration}


def closed_loop(target, concurrency, duration, warmup):
    results = []
    lock = threading.Lock()
    start = time.perf_counter()
    stop = start + warmup + duration
    cut = start + warmup

    def worker():
        while True:
            sent = time.perf_counter()
            if sent >= stop:
                return
            err = target.call()
            done = time.perf_counter()
            if sent >= cut:
                with lock:
                    results.append((sent, sent, done, err))

    threads = [threading.Thread(target=worker) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, {"max_in_flight": concurrency, "dispatch_lag_s": 0.0, "test_span_s": duration}


def ms(x):
    return "%.2f ms" % (x * 1e3) if x < 10 else "%.2f s" % x


def analyse(results, meta, args):
    ok = [r for r in results if r[3] is None]
    errs = {}
    for r in results:
        if r[3] is not None:
            errs[r[3]] = errs.get(r[3], 0) + 1
    lat = sorted(r[2] - r[0] for r in ok)
    svc = sorted(r[2] - r[1] for r in ok)
    span = meta["test_span_s"]
    out = {
        "mode": "closed-loop" if args.closed_loop else "open-loop",
        "target": args.url or args.cmd,
        "requested_rate": None if args.closed_loop else args.rate,
        "achieved_rate": len(results) / span if span else None,
        "requests": len(results), "ok": len(ok), "errors": errs,
        "latency": {("p%g" % q): pct(lat, q) for q in PCTS},
        "service_time": {("p%g" % q): pct(svc, q) for q in PCTS},
        "max_latency": lat[-1] if lat else None,
        "mean_latency": sum(lat) / len(lat) if lat else None,
        "max_in_flight": meta["max_in_flight"],
        "dispatch_lag_s": meta["dispatch_lag_s"],
        "settings": {"rate": args.rate, "duration": args.duration, "warmup": args.warmup,
                     "workers": args.workers, "concurrency": args.concurrency},
    }
    w = []
    if args.closed_loop:
        w.append("Closed-loop mode: the client stops sending while the server is slow, so "
                 "stalls are under-counted (coordinated omission). Use these numbers only to "
                 "compare with an open-loop run.")
    else:
        if meta["max_in_flight"] >= args.workers:
            w.append("All %d workers were busy at some point, so some requests waited in the "
                     "client. Latency is still counted from the intended start, which is right "
                     "if the server would have queued them too; raise --workers to be sure the "
                     "client is not the bottleneck." % args.workers)
        if meta["dispatch_lag_s"] > max(0.002, 0.5 / args.rate):
            w.append("The scheduler fell behind by up to %s. The client machine cannot keep "
                     "this rate steadily; results above that lag are suspect." % ms(meta["dispatch_lag_s"]))
        if out["achieved_rate"] and out["achieved_rate"] < 0.95 * args.rate:
            w.append("Achieved %.1f req/s of the %.1f requested." % (out["achieved_rate"], args.rate))
    for q in PCTS:
        need = int(math.ceil(10 / (1 - q / 100.0)))
        if len(lat) < need:
            w.append("p%g rests on fewer than 10 samples above it (%d requests; want %d or more). "
                     "Treat it as a rough guide." % (q, len(lat), need))
            break
    if errs:
        w.append("%d request(s) failed: %s. Failed requests are left out of the percentiles; "
                 "a fast error is not a fast success." % (sum(errs.values()), errs))
    if out["mean_latency"] and lat and out["latency"]["p99"] > 5 * out["mean_latency"]:
        w.append("p99 is %.0fx the mean. An average would hide this tail completely." % (
            out["latency"]["p99"] / out["mean_latency"]))
    out["warnings"] = w
    return out


def markdown(o):
    L = ["# Latency report (%s)" % o["mode"], "",
         "Target: `%s`  " % o["target"],
         "Requests: %d (%d ok), achieved %.1f req/s%s" % (
             o["requests"], o["ok"], o["achieved_rate"] or 0,
             "" if o["requested_rate"] is None else " of %.1f requested" % o["requested_rate"]),
         "", "| | p50 | p90 | p99 | p99.9 | max | mean |", "|---|---|---|---|---|---|---|"]
    lat, svc = o["latency"], o["service_time"]
    L.append("| latency (from intended start) | %s | %s | %s | %s | %s | %s |" % (
        ms(lat["p50"]), ms(lat["p90"]), ms(lat["p99"]), ms(lat["p99.9"]),
        ms(o["max_latency"]), ms(o["mean_latency"])))
    L.append("| service time (from actual send) | %s | %s | %s | %s | | |" % (
        ms(svc["p50"]), ms(svc["p90"]), ms(svc["p99"]), ms(svc["p99.9"])))
    if o["warnings"]:
        L += ["", "**Warnings**", ""] + ["- " + w for w in o["warnings"]]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="HTTP(S) URL to call")
    g.add_argument("--cmd", help="command to run once per request")
    ap.add_argument("--rate", type=float, default=50, help="requests per second (open loop, default 50)")
    ap.add_argument("--duration", type=float, default=20, help="measured seconds (default 20)")
    ap.add_argument("--warmup", type=float, default=2, help="seconds sent first and discarded (default 2)")
    ap.add_argument("--workers", type=int, default=256, help="max requests in flight (open loop, default 256)")
    ap.add_argument("--closed-loop", action="store_true", help="naive mode, to show coordinated omission")
    ap.add_argument("--concurrency", type=int, default=1, help="workers in closed-loop mode (default 1)")
    ap.add_argument("--method", default="GET")
    ap.add_argument("--data", help="request body")
    ap.add_argument("--header", action="append", default=[], help="'Name: value' (repeatable)")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--shell", action="store_true", help="run --cmd through /bin/sh -c")
    ap.add_argument("--json", help="write results (with every latency sample) to this file")
    ap.add_argument("--md", help="write a markdown report")
    args = ap.parse_args()

    if args.url:
        headers = dict(h.split(":", 1) for h in args.header)
        headers = {k.strip(): v.strip() for k, v in headers.items()}
        target = HttpTarget(args.url, args.method, args.data.encode() if args.data else None,
                            args.timeout, headers)
    else:
        target = CmdTarget(args.cmd, args.shell, args.timeout)

    print("%s: %s for %.0fs after %.0fs warmup..." % (
        "closed loop, %d worker(s)" % args.concurrency if args.closed_loop else
        "open loop, %.0f req/s" % args.rate, args.url or args.cmd, args.duration, args.warmup),
        file=sys.stderr, flush=True)
    if args.closed_loop:
        results, meta = closed_loop(target, args.concurrency, args.duration, args.warmup)
    else:
        results, meta = open_loop(target, args.rate, args.duration, args.warmup, args.workers)
    o = analyse(results, meta, args)
    md = markdown(o)
    print(md)
    if args.json:
        o["samples_latency_s"] = [r[2] - r[0] for r in results if r[3] is None]
        with open(args.json, "w") as f:
            json.dump(o, f, indent=1)
    if args.md:
        with open(args.md, "w") as f:
            f.write(md)


if __name__ == "__main__":
    main()
