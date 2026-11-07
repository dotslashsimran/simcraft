"""Tinybill invoice API: a small JSON API for freelancers to keep invoices and invoice files.

DELIBERATELY VULNERABLE. This is the "before" version used to practise the threat-model skill. It has an IDOR,
a SQL injection, a path traversal, an unverified payment webhook and a hardcoded (fake) key. Run it only on
127.0.0.1. The fixed version is in ../fixed/app.py.

Run: python3 app.py [port]. Seeded users are alice and bob; each password is the name followed by "-pass".
"""
import hashlib, json, os, re, secrets, sqlite3, sys, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# FAKE key for this example. It is not a real provider key and never was.
PAYMENTS_API_KEY = "sk_test_FAKE_tinybill_example_not_a_real_key"

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
    ctx.require_user()
    row = ctx.db.execute("SELECT id, title, amount_cents, status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if not row:
        raise HttpError(404, "not found")
    return dict(zip(("id", "title", "amount_cents", "status"), row))


@route("GET", r"/api/search")
def search(ctx):
    user_id = ctx.require_user()
    q = ctx.query.get("q", "")
    rows = ctx.db.execute(f"SELECT id, title, amount_cents FROM invoices WHERE owner_id = {user_id} AND title LIKE '%{q}%'").fetchall()
    return {"results": [dict(zip(("id", "title", "amount_cents"), r)) for r in rows]}


@route("GET", r"/api/files")
def download(ctx):
    user_id = ctx.require_user()
    name = ctx.query.get("name", "")
    target = os.path.join(ctx.data_dir, "files", str(user_id), name)
    if not os.path.isfile(target):
        raise HttpError(404, "not found")
    with open(target) as f:
        return {"name": name, "content": f.read()}


@route("POST", r"/api/webhooks/payments")
def payment_webhook(ctx):
    event = ctx.json()
    ctx.db.execute("UPDATE invoices SET status = ? WHERE id = ?", (event.get("status", "paid"), event.get("invoice_id")))
    ctx.db.commit()
    return {"ok": True}


def charge_headers():
    return {"Authorization": f"Bearer {PAYMENTS_API_KEY}"}


def make_server(port=8765, data_dir=None, host="127.0.0.1"):
    data_dir = data_dir or tempfile.mkdtemp(prefix="tinybill-")
    init_data(data_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

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
            except Exception as e:  # noqa: BLE001
                status, body = 500, {"error": str(e)}
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
    print(f"Tinybill (VULNERABLE) on http://127.0.0.1:{srv.server_address[1]}")
    srv.serve_forever()
