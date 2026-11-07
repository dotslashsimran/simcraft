#!/usr/bin/env python3
"""Async load generator with HdrHistogram-style latency buckets.

Drives either a Python function or an HTTP endpoint and reports throughput
and latency percentiles. Standard library only. Draws a chart if matplotlib
is installed, otherwise prints a text chart.

Two ways to apply load:

  closed loop   N workers, each sends the next request when the last one
                returns. Measures "how fast can it go at concurrency N".
                Hides queueing delay: when the system slows down, the
                workers slow down with it (coordinated omission).

  open loop     Requests start on a fixed schedule (--rate per second),
                whether or not earlier ones have finished. Latency is
                measured from the time the request was *supposed* to start,
                so a stall shows up in every request that waited behind it.
                This is what real users see.

Examples:

  # function target: file.py:function or module:function
  python3 loadgen.py --target myservice.py:handle --rate 2000 --duration 5

  # HTTP target, closed loop with 32 connections
  python3 loadgen.py --url http://127.0.0.1:8080/ --concurrency 32 --duration 10

  # find the knee: sweep offered load against a built-in single-server queue
  python3 loadgen.py --target builtin:queue --rates 200,400,600,800,900 --duration 3

  # as a library
  from loadgen import run_open, run_closed, Histogram
  result = asyncio.run(run_open(fn, rate=1000, duration=5))
  print(result.report())

The target function may be sync or async. If it takes one argument it is
given the request number, which is handy for spreading load over keys.
Its return value is counted as the "outcome" when it is a short bool, int
or str (for example a rate limiter's True/False). Exceptions count as errors.
"""

import argparse
import asyncio
import importlib
import importlib.util
import inspect
import json
import math
import os
import random
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

PERCENTILES = (50, 90, 95, 99, 99.9)


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------

class Histogram:
    """Log-linear histogram of integer microsecond values.

    Values below 2**(sub_bits+1) are stored exactly. Above that, each power
    of two range is split into 2**sub_bits equal buckets, so every recorded
    value is within 1 / 2**sub_bits of the truth (0.8% for sub_bits=7).
    This is the same idea as HdrHistogram: fixed relative error, memory that
    grows with the log of the range, and histograms that merge by adding.
    Percentiles report the top of the bucket (never under-report latency),
    capped at the exact max.
    """

    def __init__(self, sub_bits=7):
        self.sub_bits = sub_bits
        self.counts = {}
        self.n = 0
        self.total = 0
        self.min = None
        self.max = 0

    def _bucket(self, v):
        shift = v.bit_length() - self.sub_bits - 1
        if shift <= 0:
            return v, 1
        return (v >> shift) << shift, 1 << shift

    def record(self, micros, count=1):
        v = max(0, int(round(micros)))
        lower, _ = self._bucket(v)
        self.counts[lower] = self.counts.get(lower, 0) + count
        self.n += count
        self.total += v * count
        self.min = v if self.min is None else min(self.min, v)
        self.max = max(self.max, v)

    def merge(self, other):
        for k, c in other.counts.items():
            self.counts[k] = self.counts.get(k, 0) + c
        self.n += other.n
        self.total += other.total
        if other.min is not None:
            self.min = other.min if self.min is None else min(self.min, other.min)
        self.max = max(self.max, other.max)
        return self

    def percentile(self, p):
        if self.n == 0:
            return 0
        target = max(1, math.ceil(p / 100.0 * self.n))
        seen = 0
        for lower in sorted(self.counts):
            seen += self.counts[lower]
            if seen >= target:
                _, width = self._bucket(lower)
                return min(lower + width - 1, self.max)
        return self.max

    def mean(self):
        return self.total / self.n if self.n else 0.0

    def buckets_pow2(self):
        """Counts grouped by power of two, for a compact text chart."""
        out = Counter()
        for lower, c in self.counts.items():
            out[max(0, lower.bit_length() - 1)] += c
        return sorted(out.items())

    def to_dict(self):
        return {
            "count": self.n,
            "min_us": self.min or 0,
            "mean_us": round(self.mean(), 1),
            "max_us": self.max,
            "percentiles_us": {str(p): self.percentile(p) for p in PERCENTILES},
        }


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class Result:
    def __init__(self, mode, label):
        self.mode = mode
        self.label = label
        self.latency = Histogram()   # from intended start (open) or send (closed)
        self.service = Histogram()   # from actual send to completion
        self.send_lag = Histogram()  # open loop: how late the generator itself was
        self.per_second = {}         # second -> Histogram of latency
        self.outcomes = Counter()
        self.errors = Counter()
        self.sent = 0
        self.completed = 0
        self.shed = 0                # open loop: skipped because max in-flight hit
        self.elapsed = 0.0
        self.target_rate = None
        self.concurrency = None
        self.max_send_lag_ms = 0.0

    def add(self, sec, latency_us, service_us, outcome):
        self.completed += 1
        self.latency.record(latency_us)
        self.service.record(service_us)
        h = self.per_second.get(sec)
        if h is None:
            h = self.per_second[sec] = Histogram()
        h.record(latency_us)
        if outcome.startswith("error:"):
            self.errors[outcome[6:]] += 1
        else:
            self.outcomes[outcome] += 1

    @property
    def throughput(self):
        return self.completed / self.elapsed if self.elapsed > 0 else 0.0

    def to_dict(self):
        return {
            "label": self.label,
            "mode": self.mode,
            "target_rate": self.target_rate,
            "concurrency": self.concurrency,
            "elapsed_s": round(self.elapsed, 3),
            "sent": self.sent,
            "completed": self.completed,
            "shed_by_generator": self.shed,
            "throughput_per_s": round(self.throughput, 1),
            "max_send_lag_ms": round(self.max_send_lag_ms, 2),
            "outcomes": dict(self.outcomes),
            "errors": dict(self.errors),
            "latency": self.latency.to_dict(),
            "service_time": self.service.to_dict(),
            "generator_lag": self.send_lag.to_dict(),
            "timeline": [
                {"second": s, "count": h.n, "p50_us": h.percentile(50), "p99_us": h.percentile(99)}
                for s, h in sorted(self.per_second.items())
            ],
        }

    def report(self, chart=True):
        lines = []
        head = "%s  mode=%s" % (self.label, self.mode)
        if self.target_rate:
            head += "  target=%g/s" % self.target_rate
        if self.concurrency:
            head += "  concurrency=%d" % self.concurrency
        lines.append(head)
        lines.append("  completed %d in %.2fs = %.1f/s" % (self.completed, self.elapsed, self.throughput))
        if self.shed:
            lines.append("  shed by generator (max in-flight reached): %d" % self.shed)
        if self.mode == "open" and self.send_lag.n:
            lag99 = self.send_lag.percentile(99) / 1000.0
            lines.append("  generator lag p99 %.3f ms, max %.1f ms%s" % (
                lag99, self.max_send_lag_ms,
                "  (high: latency below this is noise from the generator, not the target)" if lag99 > 1 else ""))
        if self.outcomes:
            total = sum(self.outcomes.values())
            parts = ["%s=%d (%.1f%%)" % (k, v, 100.0 * v / total) for k, v in self.outcomes.most_common(6)]
            lines.append("  outcomes: " + ", ".join(parts))
        if self.errors:
            lines.append("  errors: " + ", ".join("%s=%d" % kv for kv in self.errors.most_common(6)))
        lines.append("  %-14s %9s %9s %9s %9s %9s %9s" % ("ms", "p50", "p90", "p95", "p99", "p99.9", "max"))
        for name, h in (("latency", self.latency), ("service time", self.service)):
            if self.mode == "closed" and name == "service time":
                continue
            vals = [h.percentile(p) / 1000.0 for p in PERCENTILES] + [h.max / 1000.0]
            lines.append("  %-14s " % name + " ".join("%9.3f" % v for v in vals))
        if chart and self.latency.n:
            lines.append("  latency distribution:")
            lines.extend("    " + l for l in text_histogram(self.latency))
            if len(self.per_second) > 1:
                lines.append("  per-second p99 (ms): " + timeline_text(self))
        return "\n".join(lines)


def fmt_us(us):
    if us < 1000:
        return "%dus" % us
    if us < 1000000:
        return "%.1fms" % (us / 1000.0)
    return "%.2fs" % (us / 1e6)


def text_histogram(h, width=40):
    rows = h.buckets_pow2()
    if not rows:
        return []
    top = max(c for _, c in rows)
    out = []
    for exp, c in rows:
        lo, hi = (1 << exp) if exp else 0, (1 << (exp + 1)) - 1
        bar = "#" * max(1, int(round(width * c / top)))
        out.append("%9s-%-9s %-*s %d" % (fmt_us(lo), fmt_us(hi), width, bar, c))
    return out


def timeline_text(result):
    return " ".join("%.1f" % (h.percentile(99) / 1000.0) for _, h in sorted(result.per_second.items()))


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

def _outcome(value):
    if value is None:
        return "ok"
    if isinstance(value, (bool, int, str)) and len(str(value)) <= 32:
        return str(value)
    return "ok"


def wrap_callable(fn, threads=False):
    """Return an async fn(i) -> outcome string."""
    try:
        nparams = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        nparams = 0
    is_async = inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(getattr(fn, "__call__", None))

    async def call(i):
        args = (i,) if nparams >= 1 else ()
        if is_async:
            return _outcome(await fn(*args))
        if threads:
            return _outcome(await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args)))
        return _outcome(fn(*args))

    return call


def load_target(spec):
    """'path/to/file.py:func', 'package.module:func' or 'builtin:queue'."""
    mod_part, _, name = spec.rpartition(":")
    if not mod_part:
        raise SystemExit("--target must look like file.py:function or module:function")
    if mod_part == "builtin":
        return BUILTINS[name]()
    if mod_part.endswith(".py") or os.sep in mod_part:
        path = os.path.abspath(mod_part)
        sys.path.insert(0, os.path.dirname(path))
        modname = os.path.splitext(os.path.basename(path))[0]
        spec_ = importlib.util.spec_from_file_location(modname, path)
        module = importlib.util.module_from_spec(spec_)
        sys.modules[modname] = module
        spec_.loader.exec_module(module)
    else:
        sys.path.insert(0, os.getcwd())
        module = importlib.import_module(mod_part)
    return getattr(module, name)


def _builtin_queue(service_ms=1.0, seed=1):
    """A single-server FIFO queue with exponential service time.

    Capacity is about 1000/service_ms requests per second. Useful for seeing
    the latency knee: queueing theory (M/M/1) says mean time in system is
    S / (1 - utilization), so latency doubles at 50% load and is 10x at 90%.
    """
    rng = random.Random(seed)
    lock = asyncio.Lock()
    state = {"busy_until": 0.0}

    async def serve():
        # Model the server as a timeline rather than real sleeps per request,
        # so the result does not depend on the event loop's sleep resolution.
        loop = asyncio.get_running_loop()
        async with lock:
            now = loop.time()
            start = max(now, state["busy_until"])
            state["busy_until"] = start + rng.expovariate(1000.0 / service_ms)
            done = state["busy_until"]
        await asyncio.sleep(max(0.0, done - loop.time()))
        return "ok"

    return serve


BUILTINS = {"queue": _builtin_queue}


class HttpTarget:
    """Minimal keep-alive HTTP/1.1 client pool on asyncio streams."""

    def __init__(self, url, max_conns=64, method="GET", body=b"", headers=(), timeout=10.0):
        u = urlsplit(url)
        if u.scheme not in ("http", "https"):
            raise SystemExit("only http:// and https:// urls are supported")
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.ssl = u.scheme == "https"
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        hdrs = ["%s %s HTTP/1.1" % (method, path), "Host: %s" % u.netloc,
                "Connection: keep-alive", "Content-Length: %d" % len(body)]
        hdrs.extend(headers)
        self.request_bytes = ("\r\n".join(hdrs) + "\r\n\r\n").encode() + body
        self.max_conns = max_conns
        self.sem = None  # made on first use, inside the running event loop
        self.idle = []
        self.timeout = timeout

    async def _read_response(self, reader):
        status_line = await reader.readline()
        if not status_line:
            raise ConnectionError("connection closed")
        status = int(status_line.split()[1])
        length, chunked, close = None, False, False
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").partition(":")
            k, v = k.strip().lower(), v.strip().lower()
            if k == "content-length":
                length = int(v)
            elif k == "transfer-encoding" and "chunked" in v:
                chunked = True
            elif k == "connection" and v == "close":
                close = True
        if chunked:
            while True:
                size = int((await reader.readline()).split(b";")[0], 16)
                await reader.readexactly(size + 2)
                if size == 0:
                    break
        elif length is not None:
            await reader.readexactly(length)
        else:
            await reader.read()
            close = True
        return status, close

    async def __call__(self, i=0):
        if self.sem is None:
            self.sem = asyncio.Semaphore(self.max_conns)
        async with self.sem:
            conn = self.idle.pop() if self.idle else None
            if conn is None:
                conn = await asyncio.open_connection(self.host, self.port, ssl=self.ssl or None)
            reader, writer = conn
            try:
                writer.write(self.request_bytes)
                await writer.drain()
                status, close = await asyncio.wait_for(self._read_response(reader), self.timeout)
            except BaseException:
                writer.close()
                raise
            if close:
                writer.close()
            else:
                self.idle.append(conn)
        if status >= 500:
            raise RuntimeError("http_%d" % status)
        return str(status)


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

async def _one(call, i, intended, t0, warm_end, result):
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        outcome = await call(i)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # count, keep going
        outcome = "error:" + (str(e) if str(e).startswith("http_") else type(e).__name__)
    done = loop.time()
    if intended >= warm_end:
        result.add(int(done - t0), (done - intended) * 1e6, (done - started) * 1e6, outcome)


async def run_open(fn, rate, duration, warmup=0.0, max_inflight=10000, label="target", threads=False):
    """Start requests on a fixed schedule. Latency counts from the scheduled time."""
    call = wrap_callable(fn, threads)
    # A plain sync function cannot overlap with anything anyway, so call it
    # inline instead of paying for a task per request. This keeps the
    # generator on schedule at much higher rates.
    inline = not threads and not inspect.iscoroutinefunction(fn) \
        and not inspect.iscoroutinefunction(getattr(fn, "__call__", None))
    loop = asyncio.get_running_loop()
    result = Result("open", label)
    result.target_rate = rate
    t0 = loop.time()
    warm_end = t0 + warmup
    end = warm_end + duration
    tasks = set()
    i = 0
    while True:
        intended = t0 + i / float(rate)
        if intended >= end:
            break
        now = loop.time()
        if intended > now:
            # Sleeping straight to the deadline overshoots by milliseconds on
            # a busy laptop. Sleep most of the way, then yield in a tight loop
            # (completions still run) until the scheduled time.
            if intended - now > 0.002:
                await asyncio.sleep(intended - now - 0.0015)
            while loop.time() < intended:
                await asyncio.sleep(0)
        elif i % 64 == 0:
            await asyncio.sleep(0)  # behind schedule: still let completions run
        lag = loop.time() - intended
        result.send_lag.record(lag * 1e6)
        result.max_send_lag_ms = max(result.max_send_lag_ms, lag * 1000)
        if len(tasks) >= max_inflight:
            result.shed += 1
        else:
            if intended >= warm_end:
                result.sent += 1
            if inline:
                # Step the coroutine by hand: with a sync target it finishes
                # (and records its result) without ever suspending.
                c = _one(call, i, intended, t0, warm_end, result)
                try:
                    c.send(None)
                except StopIteration:
                    pass
                else:
                    c.close()
                    raise RuntimeError("target suspended; pass an async function instead")
            else:
                t = asyncio.ensure_future(_one(call, i, intended, t0, warm_end, result))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        i += 1
    if tasks:
        await asyncio.wait(tasks)
    result.elapsed = duration  # throughput over the scheduled window
    return result


async def run_closed(fn, concurrency, duration, warmup=0.0, label="target", threads=False):
    """N workers each send the next request as soon as the last one returns."""
    call = wrap_callable(fn, threads)
    loop = asyncio.get_running_loop()
    result = Result("closed", label)
    result.concurrency = concurrency
    t0 = loop.time()
    warm_end = t0 + warmup
    end = warm_end + duration
    counter = [0]

    async def worker():
        while loop.time() < end:
            i = counter[0]
            counter[0] += 1
            start = loop.time()
            if start >= warm_end:
                result.sent += 1
            await _one(call, i, start, t0, warm_end, result)
            if i % 256 == 0:
                await asyncio.sleep(0)

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    result.elapsed = max(1e-9, loop.time() - warm_end)
    return result


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def plot(results, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed, skipping %s (text report above has the same numbers)" % path)
        return False
    fig, (a, b) = plt.subplots(1, 2, figsize=(12, 4.5))
    for r in results:
        name = r.label if len(results) == 1 else "%s @ %s" % (r.label, r.target_rate or r.concurrency)
        secs = sorted(r.per_second)
        a.plot(secs, [r.per_second[s].percentile(99) / 1000.0 for s in secs], label=name + " p99")
        a.plot(secs, [r.per_second[s].percentile(50) / 1000.0 for s in secs], "--", label=name + " p50")
        ps = [50, 75, 90, 95, 99, 99.5, 99.9, 99.99]
        xs = [1.0 / (1 - p / 100.0) for p in ps]
        b.plot(xs, [r.latency.percentile(p) / 1000.0 for p in ps], marker="o", label=name)
    a.set_xlabel("second")
    a.set_ylabel("latency ms")
    a.set_title("latency over time")
    a.legend(fontsize=7)
    b.set_xscale("log")
    b.set_xticks([1.0 / (1 - p / 100.0) for p in (50, 90, 99, 99.9, 99.99)])
    b.set_xticklabels(["p50", "p90", "p99", "p99.9", "p99.99"])
    b.set_ylabel("latency ms")
    b.set_title("latency by percentile")
    b.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    print("wrote", path)
    return True


def sweep_table(results):
    lines = ["%10s %12s %9s %9s %9s %9s %8s" % ("offered/s", "achieved/s", "p50 ms", "p99 ms", "p99.9 ms", "max ms", "errors")]
    for r in results:
        lines.append("%10g %12.1f %9.3f %9.3f %9.3f %9.3f %8d" % (
            r.target_rate, r.throughput, r.latency.percentile(50) / 1000.0, r.latency.percentile(99) / 1000.0,
            r.latency.percentile(99.9) / 1000.0, r.latency.max / 1000.0, sum(r.errors.values())))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    tgt = ap.add_mutually_exclusive_group(required=True)
    tgt.add_argument("--target", help="file.py:func, module:func, or builtin:queue")
    tgt.add_argument("--url", help="http(s) endpoint")
    ap.add_argument("--method", default="GET")
    ap.add_argument("--body", default="", help="request body for --url")
    ap.add_argument("--header", action="append", default=[], help="extra header, 'Name: value'")
    load = ap.add_mutually_exclusive_group()
    load.add_argument("--rate", type=float, help="open loop: requests per second")
    load.add_argument("--rates", help="open loop sweep: comma separated rates")
    load.add_argument("--concurrency", type=int, help="closed loop: number of workers")
    ap.add_argument("--duration", type=float, default=10.0, help="measured seconds (default 10)")
    ap.add_argument("--warmup", type=float, default=1.0, help="seconds to run before measuring")
    ap.add_argument("--max-inflight", type=int, default=10000)
    ap.add_argument("--connections", type=int, default=64, help="HTTP connection pool size")
    ap.add_argument("--threads", action="store_true", help="run a sync target in a thread pool")
    ap.add_argument("--json", help="write results as JSON here")
    ap.add_argument("--plot", help="write a PNG chart here (needs matplotlib)")
    args = ap.parse_args(argv)

    async def go():
        if args.url:
            fn = HttpTarget(args.url, args.connections, args.method, args.body.encode(), args.header)
            label = args.url
        else:
            fn = load_target(args.target)
            label = args.target
        results = []
        if args.rates:
            for r in [float(x) for x in args.rates.split(",")]:
                if args.target and args.target.startswith("builtin:"):
                    fn = load_target(args.target)  # fresh queue per step
                res = await run_open(fn, r, args.duration, args.warmup, args.max_inflight, label, args.threads)
                print(res.report(chart=False))
                print()
                results.append(res)
            print(sweep_table(results))
        elif args.rate:
            results.append(await run_open(fn, args.rate, args.duration, args.warmup, args.max_inflight, label, args.threads))
            print(results[0].report())
        else:
            results.append(await run_closed(fn, args.concurrency or 16, args.duration, args.warmup, label, args.threads))
            print(results[0].report())
        return results

    results = asyncio.run(go())
    if args.json:
        with open(args.json, "w") as f:
            json.dump([r.to_dict() for r in results], f, indent=2)
        print("wrote", args.json)
    if args.plot:
        plot(results, args.plot)


if __name__ == "__main__":
    main()
