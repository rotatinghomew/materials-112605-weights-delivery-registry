"""核对交换资料中的实体引用与原始回执顺序。"""
from datetime import datetime
import json
from pathlib import Path
import unittest

from partner import parse_time as time

ROOT = Path(__file__).resolve().parents[1]


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures" / "exchange.json").read_text(encoding="utf-8"))

    def test_record_identity(self):
        for name, collection in self.data.items():
            with self.subTest(collection=name):
                ids = [rec["id"] for rec in collection]
                self.assertTrue(ids)
                self.assertTrue(all(isinstance(i, str) and i for i in ids))
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(set(x) == set(collection[0]) for x in collection))

    def test_receipt_clocks(self):
        for rec in self.data["receipts"]:
            with self.subTest(receipt=rec["id"]):
                self.assertLessEqual(time(rec["happened"]), time(rec["arrived"]))
        with self.assertRaises(ValueError):
            time(datetime(2027, 1, 18).isoformat())

    def test_related_records(self):
        def row(collection, key):
            return next(rec for rec in self.data[collection] if rec["id"] == key)
        from hashlib import sha256
        for rec in self.data["weights"]:
            assert sha256((ROOT / rec["file"]).read_bytes()).hexdigest() == rec["sha256"]
        base = row("weights","W2")["base_id"]
        assert row("weights","W4")["base_id"] == base
        assert row("weights","W6")["base_id"] is None
        assert "W2" in row("weights","W3")["parents"] and "B2" not in row("weights","W3")["data"]
        for rec in self.data["receipts"]:
            assert [row("weights",x)["sha256"] for x in rec["files"]] == rec["digests"]
        result = row("receipts","D2")
        assert time(result["happened"]) < time(row("changes","RL1")["happened"]) < time(result["arrived"])
