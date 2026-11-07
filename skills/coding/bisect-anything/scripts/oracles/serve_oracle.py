#!/usr/bin/env python3
"""Pass/fail oracle for behaviour of a running server.

Starts the server on a free port, waits until it answers, sends one request
(or runs a probe command), checks the answer, and always kills the whole
server process group afterwards, even on errors. A server that never comes
up returns 125, because "did not start" is usually a build problem at that
commit, not the bug you are chasing.

  serve_oracle.py --cmd "python server.py --port {port}" \\
      --url "http://127.0.0.1:{port}/api/total?ids=1,2" --expect '"total": 30'

{port} is replaced in --cmd, --url, --ready-url and --probe, and is also in $PORT.

Exit codes: 0 good, 1 bad, 125 the server did not start.
"""
import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request(url, method, data, headers, timeout):
    req = urllib.request.Request(url, data=data.encode() if data else None, method=method)
    for h in headers:
        k, _, v = h.partition(":")
        req.add_header(k.strip(), v.strip())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cmd", required=True, help="command that starts the server")
    ap.add_argument("--url", help="the request to check")
    ap.add_argument("--ready-url", help="poll this until it answers (default: --url)")
    ap.add_argument("--ready-timeout", type=float, default=60)
    ap.add_argument("--method", default="GET")
    ap.add_argument("--data", help="request body")
    ap.add_argument("--header", action="append", default=[], help="'Name: value' (repeatable)")
    ap.add_argument("--expect-status", type=int, default=200)
    ap.add_argument("--expect", action="append", default=[], help="regex the body must match")
    ap.add_argument("--reject", action="append", default=[], help="regex the body must not match")
    ap.add_argument("--probe", help="instead of --url checks, run this command; its exit code is the verdict")
    ap.add_argument("--timeout", type=float, default=30, help="seconds for the request or probe")
    a = ap.parse_args()
    if not a.url and not a.probe:
        sys.exit("serve_oracle.py: give --url or --probe")

    port = free_port()
    sub = lambda s: s.replace("{port}", str(port)) if s else s  # noqa: E731
    env = dict(os.environ, PORT=str(port))
    logf = tempfile.TemporaryFile()
    server = subprocess.Popen(sub(a.cmd), shell=True, env=env, start_new_session=True,
                              stdout=logf, stderr=subprocess.STDOUT)

    def server_log():
        logf.seek(0)
        return logf.read().decode("utf-8", "replace")[-1500:]

    try:
        ready = sub(a.ready_url or a.url) or None
        deadline = time.time() + a.ready_timeout
        up = False
        while time.time() < deadline:
            if server.poll() is not None:
                print("serve: server exited early (%s)\n%s" % (server.returncode, server_log()), file=sys.stderr)
                sys.exit(125)
            try:
                if ready:
                    request(ready, "GET", None, [], 2)
                else:
                    socket.create_connection(("127.0.0.1", port), 1).close()
                up = True
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        if not up:
            print("serve: not ready after %ss\n%s" % (a.ready_timeout, server_log()), file=sys.stderr)
            sys.exit(125)

        if a.probe:
            try:
                rc = subprocess.run(sub(a.probe), shell=True, env=env, timeout=a.timeout).returncode
            except subprocess.TimeoutExpired:
                rc = 1
            sys.exit(0 if rc == 0 else 125 if rc == 125 else 1)

        try:
            status, body = request(sub(a.url), a.method, a.data, a.header, a.timeout)
        except (OSError, urllib.error.URLError) as e:
            print("serve: request failed: %s" % e, file=sys.stderr)
            sys.exit(1)
        problems = []
        if status != a.expect_status:
            problems.append("status %s, wanted %s" % (status, a.expect_status))
        problems += ["body does not match %r" % p for p in a.expect if not re.search(p, body)]
        problems += ["body matches %r" % p for p in a.reject if re.search(p, body)]
        if problems:
            print("serve: " + "; ".join(problems) + "\nbody: " + body[:500], file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    finally:
        try:
            os.killpg(server.pid, signal.SIGTERM)
            server.wait(5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(server.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    main()
