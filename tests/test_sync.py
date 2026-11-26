"""回执查询、合并与对接方接收端。"""
from __future__ import annotations

import json
from pathlib import Path
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

from support import DATA, EARLY, snapshot
from partner import handler_for
from exchange import UnavailableError, ValidationError, fetch, merge
from exchange.sync import receipt_from_exchange
from exchange.transport import dispatch
from http.server import ThreadingHTTPServer


class PartnerFixtureTests(unittest.TestCase):
    def setUp(self):
        self.data = snapshot()
        self.receipts = json.loads(DATA.read_text(encoding="utf-8"))["receipts"]

    def test_receipt_business_order_differs_from_listing(self):
        local = [receipt_from_exchange(row, self.data) for row in self.receipts]
        # D2 于 10:55 发生、11:20 才可见；不能让 11:40 发生的 D4 排到它前面。
        merged = merge(local, [], "2027-01-18T12:00:00+08:00")
        order = [row["id"] for row in merged.receipts]
        self.assertLess(order.index("D2"), order.index("D4"))

    def test_merge_filters_by_arrival(self):
        local = [receipt_from_exchange(row, self.data) for row in self.receipts]
        merged = merge(local, [], "2027-01-18T11:10:00+08:00")
        self.assertEqual([row["id"] for row in merged.receipts], ["D1", "D3"])

    def test_duplicate_receipt_must_agree(self):
        local = [receipt_from_exchange(self.receipts[0], self.data)]
        altered = dict(self.receipts[0], state="enabled")
        remote = [receipt_from_exchange(altered, self.data)]
        with self.assertRaises(ValidationError):
            merge(local, remote, "2027-01-18T12:00:00+08:00")

    def test_digest_must_match_registered_bytes(self):
        altered = dict(self.receipts[0], digests=["0" * 64, self.receipts[0]["digests"][1]])
        with self.assertRaises(ValidationError):
            receipt_from_exchange(altered, self.data)


class PartnerServerTests(unittest.TestCase):
    def serve(self, receipts, **kwargs):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(receipts, **kwargs))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_port}"

    def test_fetch_respects_business_visibility(self):
        receipts = json.loads(DATA.read_text(encoding="utf-8"))["receipts"]
        url = self.serve(receipts, unavailable=False, journal=None, drop_reply=False)
        rows = fetch(url, "2027-01-18T11:10:00+08:00", snapshot=snapshot())
        self.assertEqual([row.id for row in rows], ["D1", "D3"])

    def test_unavailable_server_is_reported(self):
        url = self.serve([], unavailable=True, journal=None, drop_reply=False)
        with self.assertRaises(UnavailableError):
            fetch(url, EARLY)

    def test_dispatch_is_accepted_without_receipt(self):
        with tempfile_journal() as journal:
            url = self.serve([], unavailable=False, journal=journal, drop_reply=False)
            data = snapshot()
            from exchange import Ledger
            ledger = Ledger(data)
            delivery = ledger.issue("O1", "C-1", ("W1", "W2"), EARLY)
            result = dispatch(url, {"credential": delivery.id, "order": "O1"})
            self.assertEqual(result.outcome, "accepted")
            with __import__("sqlite3").connect(journal) as db:
                rows = db.execute("SELECT requested FROM ingress").fetchall()
            self.assertEqual(len(rows), 1)
            # 接收端不会生成履约回执。
            with __import__("sqlite3").connect(journal) as db:
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"ingress"})

    def test_dropped_reply_is_unknown(self):
        with tempfile_journal() as journal:
            url = self.serve([], unavailable=False, journal=journal, drop_reply=True)
            result = dispatch(url, {"credential": "x", "order": "O1"})
            self.assertEqual(result.outcome, "unknown")


class tempfile_journal:
    def __enter__(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "journal.db"
        return self.path

    def __exit__(self, *exc):
        self._dir.cleanup()
        return False


if __name__ == "__main__":
    unittest.main()
