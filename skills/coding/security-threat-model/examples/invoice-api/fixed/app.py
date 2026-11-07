"""Tinybill invoice API: a small JSON API for freelancers to keep invoices and invoice files.

FIXED version. Each fix is marked with the threat id from ../THREAT_MODEL.md (T1 to T6), and each one has a
regression test in ../tests/test_threats.py. Needs PAYMENTS_WEBHOOK_SECRET (and PAYMENTS_API_KEY to charge) in
the environment.

Run: python3 app.py [port]. Seeded users are alice and bob; each password is the name followed by "-pass".
"""
import hashlib, hmac, json, os, re, secrets, sqlite3, sys, tempfile, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

WEBHOOK_TOLERANCE_SECONDS = 300
SAFE_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn))
        return fn
    return deco


class HttpError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 100_000).hex()


def init_data(data_dir):
    data_dir = Path(data_dir)
    db = sqlite3.connect(data_dir / "app.db")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT UNIQUE, salt BLOB, pw_hash TEXT);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER);
        CREATE TABLE IF NOT EXISTS invoices (id INTEGER PRIMARY KEY, owner_id INTEGER, title TEXT, amount_cents INTEGER, status TEXT);
    """)
    if not db.execute("SELECT 1 FROM users").fetchone():
        for uid, name in ((1, "alice"), (2, "bob")):
            salt = os.urandom(16)
            db.execute("INSERT INTO users VALUES (?,?,?,?)", (uid, name, salt, hash_pw(f"{name}-pass", salt)))
        db.executemany("INSERT INTO invoices VALUES (?,?,?,?,?)", [
            (1, 1, "Logo design for Juniper Bakery", 48500, "unpaid"),
            (2, 1, "Website copy, March", 120000, "unpaid"),
            (3, 2, "Retainer for Halden Legal (confidential)", 950000, "unpaid"),
        ])
        db.commit()
    for uid in (1, 2):
        (data_dir / "files" / str(uid)).mkdir(parents=True, exist_ok=True)
    (data_dir / "files" / "1" / "invoice-1.txt").write_text("Invoice 1: Logo design, $485.00\n")
    (data_dir / "files" / "2" / "invoice-3.txt").write_text("Invoice 3: Retainer, $9,500.00\n")
    (data_dir / "server_secrets.txt").write_text("INTERNAL ONLY: db backups key = not-real-backup-key\n")
    db.close()


class Ctx:
    def __init__(self, handler, data_dir):
        self.h, self.data_dir = handler, Path(data_dir)
        u = urlparse(handler.path)
        self.path = u.path
        self.query = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(handler.headers.get("Content-Length") or 0)
        self.raw_body = handler.rfile.read(n) if n else b""
        self.db = sqlite3.connect(self.data_dir / "app.db")

    def json(self):
        try:
            return json.loads(self.raw_body or b"{}")
        except ValueError:
            raise HttpError(400, "bad json")

    def require_user(self):
        auth = self.h.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        row = self.db.execute("SELECT user_id FROM sessions WHERE token = ?", (token,)).fetchone()
        if not row:
            raise HttpError(401, "login required")
        return row[0]


@route("POST", r"/api/login")
def login(ctx):
    body = ctx.json()
    row = ctx.db.execute("SELECT id, salt, pw_hash FROM users WHERE name = ?", (body.get("username", ""),)).fetchone()
    if not row or not secrets.compare_digest(hash_pw(body.get("password", ""), row[1]), row[2]):
        raise HttpError(401, "bad credentials")
    token = secrets.token_urlsafe(32)
    ctx.db.execute("INSERT INTO sessions VALUES (?, ?)", (token, row[0]))
    ctx.db.commit()
    return {"token": token}


@route("GET", r"/api/invoices")
def list_invoices(ctx):
    user_id = ctx.require_user()
    rows = ctx.db.execute("SELECT id, title, amount_cents, status FROM invoices WHERE owner_id = ?", (user_id,)).fetchall()
    return {"invoices": [dict(zip(("id", "title", "amount_cents", "status"), r)) for r in rows]}


@route("GET", r"/api/invoices/(\d+)")
def get_invoice(ctx, invoice_id):
    user_id = ctx.require_user()
    # T1: the owner check is part of the query, so there is no code path that loads a row and forgets to check it.
    # Someone else's invoice gets the same 404 as a missing one, so ids cannot be probed.
    row = ctx.db.execute("SELECT id, title, amount_cents, status FROM invoices WHERE id = ? AND owner_id = ?",
                         (invoice_id, user_id)).fetchone()
    if not row:
        raise HttpError(404, "not found")
    return dict(zip(("id", "title", "amount_cents", "status"), row))


@route("GET", r"/api/search")
def search(ctx):
    user_id = ctx.require_user()
    q = ctx.query.get("q", "")[:100]
    # T2: values go in as parameters, never into the SQL text. LIKE wildcards in the input are escaped so % and _
    # are searched for literally.
    pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = ctx.db.execute("SELECT id, title, amount_cents FROM invoices WHERE owner_id = ? AND title LIKE ? ESCAPE '\\'",
                          (user_id, pattern)).fetchall()
    return {"results": [dict(zip(("id", "title", "amount_cents"), r)) for r in rows]}


@route("GET", r"/api/files")
def download(ctx):
    user_id = ctx.require_user()
    name = ctx.query.get("name", "")
    # T3: two independent checks. The name must be a plain file name (no slashes, no leading dot), and the resolved
    # path must still be inside this user's folder after symlinks and .. are resolved.
    base = (ctx.data_dir / "files" / str(user_id)).resolve()
    target = (base / name).resolve()
    if not SAFE_FILENAME.fullmatch(name) or not target.is_relative_to(base) or not target.is_file():
        raise HttpError(404, "not found")
    return {"name": name, "content": target.read_text()}


@route("POST", r"/api/webhooks/payments")
def payment_webhook(ctx):
    # T4: the provider signs "<timestamp>.<raw body>" with a shared secret. Verify against the raw bytes before
    # parsing, compare in constant time, reject old timestamps so a captured event cannot be replayed, and fail
    # closed if the secret is not configured.
    secret = os.environ.get("PAYMENTS_WEBHOOK_SECRET", "")
    ts = ctx.h.headers.get("X-Timestamp", "")
    sig = ctx.h.headers.get("X-Signature", "")
    if not secret or not ts.isdigit() or abs(time.time() - int(ts)) > WEBHOOK_TOLERANCE_SECONDS:
        raise HttpError(401, "bad signature")
    expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + ctx.raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HttpError(401, "bad signature")
    event = ctx.json()
    if event.get("status") not in {"paid", "refunded"}:
        raise HttpError(400, "bad status")
    ctx.db.execute("UPDATE invoices SET status = ? WHERE id = ?", (event.get("status", "paid"), event.get("invoice_id")))
    ctx.db.commit()
    return {"ok": True}


def charge_headers():
    # T5: the key lives in the environment (or a secret manager), never in the source or the repo history.
    return {"Authorization": f"Bearer {os.environ['PAYMENTS_API_KEY']}"}


def make_server(port=8765, data_dir=None, host="127.0.0.1"):
    data_dir = data_dir or tempfile.mkdtemp(prefix="tinybill-")
    init_data(data_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def log_error(self, fmt, *args):
            sys.stderr.write("tinybill: " + (fmt % args) + "\n")

        def handle_any(self, method):
            ctx = None
            try:
                ctx = Ctx(self, data_dir)
                for m, rx, fn in ROUTES:
                    match = rx.fullmatch(ctx.path)
                    if m == method and match:
                        status, body = 200, fn(ctx, *match.groups())
                        break
                else:
                    status, body = 404, {"error": "no route"}
            except HttpError as e:
                status, body = e.status, {"error": e.msg}
            except Exception:  # noqa: BLE001
                # T6: log the details on the server, send the client nothing it can learn from.
                self.log_error("unhandled error on %s %s\n%s", method, ctx.path if ctx else "?", traceback.format_exc())
                status, body = 500, {"error": "internal error"}
            finally:
                if ctx:
                    ctx.db.close()
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    srv = make_server(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"Tinybill (fixed) on http://127.0.0.1:{srv.server_address[1]}")
    srv.serve_forever()
