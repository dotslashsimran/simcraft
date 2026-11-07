#!/usr/bin/env python3
"""A tiny HTTP server that answers in about 1 ms but freezes completely for
--stall-ms every --period seconds, like a stop-the-world GC pause.

    python3 stall_server.py --port 8099 --period 2 --stall-ms 400
"""
import argparse
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

T0 = time.perf_counter()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        phase = (time.perf_counter() - T0) % self.server.period
        if phase < self.server.stall:          # the whole process is "paused"
            time.sleep(self.server.stall - phase)
        time.sleep(0.001)                      # normal work
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--period", type=float, default=2.0)
    ap.add_argument("--stall-ms", type=float, default=400)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.period, srv.stall = a.period, a.stall_ms / 1000.0
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
