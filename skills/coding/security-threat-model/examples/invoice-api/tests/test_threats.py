"""Proofs for the Tinybill threat model, written as tests of the SECURE behaviour.

Against vulnerable/ the proof tests fail: each failure is the demonstration that the threat is real.
Against fixed/ they all pass and stay in the suite as regression tests.
The "control" tests check that normal use still works, so a fix cannot pass by breaking the feature.

Everything runs against a server on 127.0.0.1 with a throwaway data folder. Nothing leaves the machine.

Run from examples/invoice-api:
  TARGET=vulnerable python3 -m unittest discover -s tests -v
  TARGET=fixed      python3 -m unittest discover -s tests -v
"""
import hashlib, hmac, importlib.util, json, os, subprocess, sys, tempfile, threading, time, unittest
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = os.environ.get("TARGET", "fixed")
APP_DIR = HERE.parent / TARGET
SECRETS_SCAN = HERE.parents[2] / "scripts" / "secrets_scan.py"
WEBHOOK_SECRET = "whsec-local-test-only"  # pragma: allowlist secret (test-only value)


def load_app():
    spec = importlib.util.spec_from_file_location(f"app_{TARGET}", APP_DIR / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["PAYMENTS_WEBHOOK_SECRET"] = WEBHOOK_SECRET
        os.environ["PAYMENTS_API_KEY"] = "local-test-placeholder"
        cls.data_dir = tempfile.mkdtemp(prefix="tinybill-test-")
        cls.app = load_app()
        cls.srv = cls.app.make_server(port=0, data_dir=cls.data_dir)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.alice = cls.login("alice")
        cls.bob = cls.login("bob")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    @classmethod
    def call(cls, method, path, token=None, body=None, headers=None):
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(cls.base + path, data=data, method=method, headers=headers or {})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    @classmethod
    def login(cls, name):
        status, body = cls.call("POST", "/api/login", body={"username": name, "password": f"{name}-pass"})
        assert status == 200, body
        return body["token"]

    def status_of(self, invoice_id):
        return self.call("GET", f"/api/invoices/{invoice_id}", self.alice)[1].get("status")


class Controls(Base):
    """Normal use. These must pass before and after the fixes."""

    def test_owner_reads_own_invoice(self):
        status, body = self.call("GET", "/api/invoices/1", self.alice)
        self.assertEqual((status, body["title"]), (200, "Logo design for Juniper Bakery"))

    def test_owner_searches_own_invoices(self):
        status, body = self.call("GET", "/api/search?q=Logo", self.alice)
        self.assertEqual([r["id"] for r in body["results"]], [1])

    def test_owner_downloads_own_file(self):
        status, body = self.call("GET", "/api/files?name=invoice-1.txt", self.alice)
        self.assertEqual(status, 200)
        self.assertIn("Logo design", body["content"])

    def test_no_token_is_rejected(self):
        self.assertEqual(self.call("GET", "/api/invoices/1")[0], 401)


class T1_IDOR(Base):
    def test_user_cannot_read_another_users_invoice(self):
        status, body = self.call("GET", "/api/invoices/3", self.alice)  # invoice 3 belongs to bob
        self.assertEqual(status, 404, f"alice read bob's invoice: {body}")


class T2_SQLInjection(Base):
    def test_search_cannot_escape_owner_filter(self):
        status, body = self.call("GET", "/api/search?q=" + urllib.parse.quote("' OR 1=1 --"), self.alice)
        ids = sorted(r["id"] for r in body.get("results", []))
        self.assertNotIn(3, ids, f"search returned bob's invoice: {ids}")

    def test_search_cannot_read_session_tokens(self):
        q = "' UNION SELECT user_id, token, 0 FROM sessions --"
        status, body = self.call("GET", "/api/search?q=" + urllib.parse.quote(q), self.alice)
        titles = [r["title"] for r in body.get("results", [])]
        self.assertNotIn(self.bob, titles, "search leaked bob's session token")

    def test_wildcards_are_literal(self):
        status, body = self.call("GET", "/api/search?q=%25", self.alice)  # a bare % should match titles containing %
        self.assertEqual(body.get("results"), [])


class T3_PathTraversal(Base):
    def assert_blocked(self, name):
        status, body = self.call("GET", "/api/files?name=" + urllib.parse.quote(name), self.alice)
        self.assertNotEqual(status, 200, f"read outside the user's folder with {name!r}")
        self.assertNotIn("INTERNAL", json.dumps(body))

    def test_dotdot(self):
        self.assert_blocked("../../server_secrets.txt")

    def test_other_users_folder(self):
        self.assert_blocked("../2/invoice-3.txt")

    def test_absolute_path(self):
        self.assert_blocked(os.path.join(self.data_dir, "server_secrets.txt"))


class T4_WebhookForgery(Base):
    def sign(self, body, ts, secret=WEBHOOK_SECRET):
        return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    def test_unsigned_webhook_is_rejected(self):
        status, _ = self.call("POST", "/api/webhooks/payments", body={"invoice_id": 2, "status": "paid"})
        self.assertEqual(status, 401)
        self.assertEqual(self.status_of(2), "unpaid", "anyone on the internet marked invoice 2 paid")

    def test_wrong_secret_is_rejected(self):
        body, ts = json.dumps({"invoice_id": 2, "status": "paid"}).encode(), str(int(time.time()))
        status, _ = self.call("POST", "/api/webhooks/payments", body=body,
                              headers={"X-Timestamp": ts, "X-Signature": self.sign(body, ts, "guess")})
        self.assertEqual(status, 401)

    def test_replayed_old_event_is_rejected(self):
        body, ts = json.dumps({"invoice_id": 2, "status": "paid"}).encode(), str(int(time.time()) - 3600)
        status, _ = self.call("POST", "/api/webhooks/payments", body=body,
                              headers={"X-Timestamp": ts, "X-Signature": self.sign(body, ts)})
        self.assertEqual(status, 401)

    def test_correctly_signed_event_is_accepted(self):  # control
        body, ts = json.dumps({"invoice_id": 1, "status": "paid"}).encode(), str(int(time.time()))
        status, _ = self.call("POST", "/api/webhooks/payments", body=body,
                              headers={"X-Timestamp": ts, "X-Signature": self.sign(body, ts)})
        self.assertEqual(status, 200)
        self.assertEqual(self.status_of(1), "paid")


class T5_HardcodedSecret(Base):
    def test_no_secrets_in_source(self):
        r = subprocess.run([sys.executable, str(SECRETS_SCAN), str(APP_DIR)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


class T6_ErrorLeak(Base):
    def test_crash_returns_generic_error(self):
        body, ts = json.dumps({"invoice_id": {"x": 1}, "status": "paid"}).encode(), str(int(time.time()))
        sig = T4_WebhookForgery.sign(None, body, ts)
        status, resp = self.call("POST", "/api/webhooks/payments", body=body, headers={"X-Timestamp": ts, "X-Signature": sig})
        self.assertEqual((status, resp), (500, {"error": "internal error"}))

    def test_sql_error_text_is_not_returned(self):
        status, body = self.call("GET", "/api/search?q=" + urllib.parse.quote("'"), self.alice)
        text = json.dumps(body).lower()
        for leak in ("unrecognized", "syntax", "sqlite", "near \\\"", "traceback"):
            self.assertNotIn(leak, text, f"database error text sent to the client: {body}")


if __name__ == "__main__":
    unittest.main()
