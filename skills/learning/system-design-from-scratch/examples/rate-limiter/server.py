"""Tiny HTTP front end for a limiter: 200 if admitted, 429 if not.

    python3 server.py --algo token_bucket --limit 1000 --port 8080
    python3 ../../scripts/loadgen.py --url http://127.0.0.1:8080/?key=a --rate 3000 --duration 5

The key comes from ?key=..., so you can test per client limits.
Standard library asyncio, HTTP/1.1 keep-alive, nothing else.
"""

import argparse
import asyncio
from urllib.parse import parse_qs, urlsplit

from limiters import ALL


async def handle(reader, writer, limiter):
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            parts = line.decode("latin-1").split()
            length = 0
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.decode("latin-1").partition(":")
                if k.strip().lower() == "content-length":
                    length = int(v)
            if length:
                await reader.readexactly(length)
            query = parse_qs(urlsplit(parts[1] if len(parts) > 1 else "/").query)
            ok = limiter.allow(query.get("key", ["default"])[0])
            body = b"ok" if ok else b"slow down"
            status = b"200 OK" if ok else b"429 Too Many Requests"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def serve(algo, limit, window, port):
    limiter = ALL[algo](limit, window, __import__("time").monotonic)
    server = await asyncio.start_server(lambda r, w: handle(r, w, limiter), "127.0.0.1", port)
    print("listening on 127.0.0.1:%d (%s, %d per %gs)" % (port, algo, limit, window), flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--algo", default="token_bucket", choices=sorted(ALL))
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--window", type=float, default=1.0)
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    try:
        asyncio.run(serve(a.algo, a.limit, a.window, a.port))
    except KeyboardInterrupt:
        pass
