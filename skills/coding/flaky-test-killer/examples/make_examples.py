#!/usr/bin/env python3
"""Generate two small projects with known flaky tests, for practising and
for testing the flaky-test-killer scripts.

  make_examples.py --out DIR                 # flaky versions
  make_examples.py --out DIR --variant fixed # same projects, root causes fixed

DIR/py-shop    plain unittest (no pytest needed), with run_tests.py that can
               shuffle the order (--seed N), list ids (--list) and run given
               ids in the given order.
  - order dependence: test_users.UserTests.test_enable_eu mutates a global
    settings dict and never restores it; test_billing...test_price_label
    fails whenever it runs after it. Default (alphabetical) order hides it.
  - time dependence: next_billing_date() does month + 1, which crashes on
    the 29th to 31st of some months and in December; the test uses today.
  - timezone dependence: report_date() uses local time, the test compares
    against UTC. Fails whenever the local date differs from the UTC date.
  - race: export runs on a thread with a variable delay; the test sleeps a
    fixed 10 ms instead of waiting for completion.

DIR/node-shop  node:test (Node 18+)
  - unawaited promise: saveOrder() is not awaited; the test sleeps 5 ms
    and hopes the save is done.
  - time dependence: nextMonth() uses setMonth(+1), which rolls Jan 31
    into March; the test uses new Date().
  - timezone dependence: openingHour() uses getHours() on a UTC instant
    and the test expects the UTC hour. Passes in CI (UTC), fails on most
    laptops.
"""
import argparse
import os
import textwrap

PY_FLAKY = {
    "shop/__init__.py": "",
    "shop/config.py": '''
        SETTINGS = {"currency": "USD", "symbol": "$"}
    ''',
    "shop/billing.py": '''
        from shop.config import SETTINGS


        def price_label(cents):
            return "%s%.2f" % (SETTINGS["symbol"], cents / 100)
    ''',
    "shop/users.py": '''
        from shop.config import SETTINGS


        def enable_eu():
            SETTINGS.update(currency="EUR", symbol="\\u20ac")
            return SETTINGS["currency"]
    ''',
    "shop/dates.py": '''
        import datetime


        def next_billing_date(d):
            return d.replace(month=d.month + 1)


        def report_date():
            return datetime.datetime.now().date()
    ''',
    "shop/export.py": '''
        import random
        import threading
        import time


        def start_export(result):
            def work():
                time.sleep(random.uniform(0, 0.02))
                result["done"] = True
            threading.Thread(target=work, daemon=True).start()
    ''',
    "tests/__init__.py": "",
    "tests/test_billing.py": '''
        import unittest
        from shop.billing import price_label


        class BillingTests(unittest.TestCase):
            def test_price_label(self):
                self.assertEqual(price_label(1999), "$19.99")

            def test_zero(self):
                self.assertTrue(price_label(0).endswith("0.00"))

            def test_rounding(self):
                self.assertTrue(price_label(5).endswith("0.05"))
    ''',
    "tests/test_catalog.py": '''
        import unittest


        class CatalogTests(unittest.TestCase):
            def test_a(self): self.assertEqual(1 + 1, 2)
            def test_b(self): self.assertIn("a", "cat")
            def test_c(self): self.assertTrue([1])
            def test_d(self): self.assertEqual(sorted([3, 1]), [1, 3])
            def test_e(self): self.assertEqual("x".upper(), "X")
            def test_f(self): self.assertFalse({})
    ''',
    "tests/test_dates.py": '''
        import datetime
        import unittest
        from shop.dates import next_billing_date, report_date


        class DateTests(unittest.TestCase):
            def test_next_billing_is_next_month(self):
                today = datetime.date.today()
                self.assertEqual(next_billing_date(today).month, today.month % 12 + 1)

            def test_report_date_is_today(self):
                utc_today = datetime.datetime.now(datetime.timezone.utc).date()
                self.assertEqual(report_date(), utc_today)
    ''',
    "tests/test_export.py": '''
        import time
        import unittest
        from shop.export import start_export


        class ExportTests(unittest.TestCase):
            def test_export_finishes(self):
                result = {}
                start_export(result)
                time.sleep(0.01)
                self.assertTrue(result.get("done"), "export not done")
    ''',
    "tests/test_users.py": '''
        import unittest
        from shop.users import enable_eu


        class UserTests(unittest.TestCase):
            def test_enable_eu(self):
                self.assertEqual(enable_eu(), "EUR")

            def test_name(self):
                self.assertEqual("ana".title(), "Ana")
    ''',
    "run_tests.py": '''
        """Run unittest tests in a known order.

        run_tests.py                 all tests, default (alphabetical) order
        run_tests.py --seed 42       all tests, shuffled with seed 42
        run_tests.py --list [--seed] print test ids in run order
        run_tests.py ID [ID ...]     run exactly these ids, in this order
        """
        import random
        import sys
        import unittest


        def all_ids():
            suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
            out = []

            def walk(s):
                for t in s:
                    if isinstance(t, unittest.TestSuite):
                        walk(t)
                    else:
                        out.append(t.id())
            walk(suite)
            return out


        def main(argv):
            seed, listing, ids = None, False, []
            i = 0
            while i < len(argv):
                if argv[i] == "--seed":
                    seed = int(argv[i + 1]); i += 2; continue
                if argv[i] == "--list":
                    listing = True; i += 1; continue
                ids.append(argv[i]); i += 1
            if not ids:
                ids = all_ids()
                if seed is not None:
                    random.Random(seed).shuffle(ids)
            if listing:
                print("\\n".join(ids))
                return 0
            if seed is not None:
                print("seed=%d" % seed)
            suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(t) for t in ids)
            res = unittest.TextTestRunner(verbosity=1).run(suite)
            return 0 if res.wasSuccessful() else 1


        if __name__ == "__main__":
            sys.exit(main(sys.argv[1:]))
    ''',
}

PY_FIXED = dict(PY_FLAKY)
PY_FIXED.update({
    # dates: clamp the day, wrap the year; report in UTC on purpose
    "shop/dates.py": '''
        import calendar
        import datetime


        def next_billing_date(d):
            year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
            day = min(d.day, calendar.monthrange(year, month)[1])
            return d.replace(year=year, month=month, day=day)


        def report_date(now=None):
            now = now or datetime.datetime.now(datetime.timezone.utc)
            return now.astimezone(datetime.timezone.utc).date()
    ''',
    # export: hand back an Event so callers can wait for the real condition
    "shop/export.py": '''
        import random
        import threading
        import time


        def start_export(result):
            done = threading.Event()

            def work():
                time.sleep(random.uniform(0, 0.02))
                result["done"] = True
                done.set()
            threading.Thread(target=work, daemon=True).start()
            return done
    ''',
    "tests/test_dates.py": '''
        import datetime
        import unittest
        from shop.dates import next_billing_date, report_date

        D = datetime.date
        UTC = datetime.timezone.utc


        class DateTests(unittest.TestCase):
            def test_next_billing_edges(self):
                cases = {
                    D(2026, 1, 15): D(2026, 2, 15),
                    D(2026, 1, 31): D(2026, 2, 28),
                    D(2028, 1, 31): D(2028, 2, 29),
                    D(2026, 3, 31): D(2026, 4, 30),
                    D(2026, 12, 31): D(2027, 1, 31),
                }
                for given, want in cases.items():
                    with self.subTest(given=given):
                        self.assertEqual(next_billing_date(given), want)

            def test_report_date_uses_utc(self):
                late_in_kiritimati = datetime.datetime(2026, 5, 2, 1, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=14)))
                self.assertEqual(report_date(late_in_kiritimati), D(2026, 5, 1))
    ''',
    "tests/test_export.py": '''
        import unittest
        from shop.export import start_export


        class ExportTests(unittest.TestCase):
            def test_export_finishes(self):
                result = {}
                done = start_export(result)
                self.assertTrue(done.wait(timeout=5), "export did not finish in 5s")
                self.assertTrue(result["done"])
    ''',
    # polluter now restores global state; victim no longer trusts the default
    "tests/test_users.py": '''
        import unittest
        from unittest import mock
        from shop import config
        from shop.users import enable_eu


        class UserTests(unittest.TestCase):
            def test_enable_eu(self):
                with mock.patch.dict(config.SETTINGS):
                    self.assertEqual(enable_eu(), "EUR")

            def test_name(self):
                self.assertEqual("ana".title(), "Ana")
    ''',
    "tests/test_billing.py": '''
        import unittest
        from unittest import mock
        from shop import config
        from shop.billing import price_label


        class BillingTests(unittest.TestCase):
            def setUp(self):
                p = mock.patch.dict(config.SETTINGS, {"currency": "USD", "symbol": "$"})
                p.start()
                self.addCleanup(p.stop)

            def test_price_label(self):
                self.assertEqual(price_label(1999), "$19.99")

            def test_zero(self):
                self.assertTrue(price_label(0).endswith("0.00"))

            def test_rounding(self):
                self.assertTrue(price_label(5).endswith("0.05"))
    ''',
})

NODE_FLAKY = {
    "package.json": '''
        {"name": "node-shop", "private": true, "type": "module", "scripts": {"test": "node --test"}}
    ''',
    "src/orders.js": '''
        const delay = (ms) => new Promise((r) => setTimeout(r, ms));

        export async function saveOrder(db, order) {
          await delay(Math.random() * 10);
          db.set(order.id, order);
        }
    ''',
    "src/time.js": '''
        export function nextMonth(d) {
          const n = new Date(d);
          n.setMonth(n.getMonth() + 1);
          return n;
        }

        export function openingHour(iso) {
          return new Date(iso).getHours();
        }
    ''',
    "test/orders.test.js": '''
        import { test } from 'node:test';
        import assert from 'node:assert/strict';
        import { saveOrder } from '../src/orders.js';

        const delay = (ms) => new Promise((r) => setTimeout(r, ms));

        test('order is saved', async () => {
          const db = new Map();
          saveOrder(db, { id: 1, total: 20 });
          await delay(5);
          assert.ok(db.has(1), 'order 1 missing');
        });
    ''',
    "test/time.test.js": '''
        import { test } from 'node:test';
        import assert from 'node:assert/strict';
        import { nextMonth, openingHour } from '../src/time.js';

        test('nextMonth moves one month ahead', () => {
          const now = new Date();
          assert.equal(nextMonth(now).getMonth(), (now.getMonth() + 1) % 12);
        });

        test('store opens at 09:00 UTC', () => {
          assert.equal(openingHour('2026-01-01T09:00:00Z'), 9);
        });
    ''',
}

NODE_FIXED = dict(NODE_FLAKY)
NODE_FIXED.update({
    "src/time.js": '''
        export function nextMonth(d) {
          const n = new Date(d);
          const day = n.getDate();
          n.setDate(1);
          n.setMonth(n.getMonth() + 1);
          const last = new Date(n.getFullYear(), n.getMonth() + 1, 0).getDate();
          n.setDate(Math.min(day, last));
          return n;
        }

        export function openingHour(iso) {
          return new Date(iso).getUTCHours();
        }
    ''',
    "test/orders.test.js": '''
        import { test } from 'node:test';
        import assert from 'node:assert/strict';
        import { saveOrder } from '../src/orders.js';

        test('order is saved', async () => {
          const db = new Map();
          await saveOrder(db, { id: 1, total: 20 });
          assert.ok(db.has(1), 'order 1 missing');
        });
    ''',
    "test/time.test.js": '''
        import { test } from 'node:test';
        import assert from 'node:assert/strict';
        import { nextMonth, openingHour } from '../src/time.js';

        test('nextMonth clamps to the last day', () => {
          const cases = [
            [new Date(2026, 0, 15), 1, 15],
            [new Date(2026, 0, 31), 1, 28],
            [new Date(2028, 0, 31), 1, 29],
            [new Date(2026, 11, 31), 0, 31],
          ];
          for (const [given, month, day] of cases) {
            const got = nextMonth(given);
            assert.deepEqual([got.getMonth(), got.getDate()], [month, day], given.toString());
          }
        });

        test('store opens at 09:00 UTC', () => {
          assert.equal(openingHour('2026-01-01T09:00:00Z'), 9);
        });
    ''',
})


def write(root, files):
    for rel, body in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(body).lstrip("\n"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--variant", choices=["flaky", "fixed"], default="flaky")
    a = ap.parse_args()
    fixed = a.variant == "fixed"
    write(os.path.join(a.out, "py-shop"), PY_FIXED if fixed else PY_FLAKY)
    write(os.path.join(a.out, "node-shop"), NODE_FIXED if fixed else NODE_FLAKY)
    print("wrote %s examples to %s/py-shop and %s/node-shop" % (a.variant, a.out, a.out))


if __name__ == "__main__":
    main()
