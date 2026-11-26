"""客户登记库：接收、确认可取、撤销处置与双方核对。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import AFTER_RL1, EARLY, snapshot
from exchange import ConflictError, Ledger, PolicyError


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.data = snapshot()
        self.ledger = Ledger(self.data)
        self.at = EARLY

    def test_delivery_request_unknown_keeps_foreign_control_possibility(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), self.at)
        self.ledger.mark_unknown(delivery.id, self.at)
        risk = self.ledger.freight_control_risk()
        self.assertEqual(len(risk), 1)
        self.assertTrue(risk[0]["foreign_control_possible"])

    def test_stage_before_enable(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), self.at)
        self.ledger.confirm_dispatch(delivery.id, self.at)
        staged = self.ledger.stage(delivery.id, self.at)
        self.assertEqual(staged.state, "staged")
        self.assertFalse(staged.to_dict()["user_can_fetch"])
        enabled = self.ledger.enable(delivery.id, self.at)
        self.assertEqual(enabled.state, "enabled")
        self.assertTrue(enabled.to_dict()["user_can_fetch"])

    def test_registry_cannot_stage_unconfirmed_request(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), self.at)
        with self.assertRaises(ConflictError):
            self.ledger.stage(delivery.id, self.at)

    def test_cannot_fetch_before_enabled(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), self.at)
        self.ledger.mark_unknown(delivery.id, self.at)
        self.ledger.stage(delivery.id, self.at)
        with self.assertRaises(ConflictError):
            self.ledger.obtain(delivery.id, self.at)

    def test_enable_checks_approved_zone(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), self.at)
        self.ledger.confirm_dispatch(delivery.id, self.at)
        self.ledger.stage(delivery.id, self.at)
        # 对 O1 来说目前是 EU。人为把辖区改掉再确认会被拒绝。
        altered = self.ledger.get(delivery.id).__class__(**{
            **self.ledger.get(delivery.id).__dict__, "zone": "CN"})
        self.ledger._replace(altered)
        with self.assertRaises(PolicyError):
            self.ledger.enable(delivery.id, self.at)

    def test_issue_rejects_unlicensed_package(self):
        with self.assertRaises(PolicyError):
            self.ledger.issue("O1", "C-1", ("W3",), self.at)

    def test_role_visibility_is_scoped(self):
        self.ledger.issue("O1", "C-1", ("W1", "W2"), self.at)
        partner = self.ledger.partner_view("N-K")
        self.assertEqual(partner["batches"], ["B2"])
        self.assertEqual(len(partner["deliveries"]), 1)
        self.assertEqual(partner["deliveries"][0]["weights"], ["W2"])
        customer = self.ledger.customer_view("O1")
        self.assertEqual(customer["zone"], "EU")
        with self.assertRaises(Exception):
            self.ledger.partner_view("NOPE")

    def test_reconcile_matches_by_digest_and_state(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), self.at)
        digests = list(delivery.digests)
        self.assertTrue(self.ledger.reconcile(delivery.id, digests)["transferable"])
        self.assertFalse(self.ledger.reconcile(delivery.id, digests[:-1])["same_package"])

    def test_two_exclusive_versions_cannot_both_be_delivered(self):
        self.ledger.issue("O1", "C-1", ("W1", "W2"), self.at)
        with self.assertRaises(ConflictError):
            self.ledger.issue("O1", "C-2", ("W1", "W2"), self.at)


class RevocationTests(unittest.TestCase):
    def setUp(self):
        self.data = snapshot()
        self.ledger = Ledger(self.data)

    def deliver(self, order, claim, weights, stage=True, obtain=False):
        delivery = self.ledger.issue(order, claim, weights, EARLY)
        self.ledger.confirm_dispatch(delivery.id, EARLY)
        if stage:
            self.ledger.stage(delivery.id, EARLY)
        if obtain:
            self.ledger.enable(delivery.id, EARLY)
            self.ledger.obtain(delivery.id, EARLY)
        return delivery

    def test_obtained_copy_is_recalled_and_stopped(self):
        delivery = self.deliver("O1", "C-1", ("W1", "W2"), obtain=True)
        outcome = self.ledger.revoke(delivery.id, AFTER_RL1)
        self.assertEqual(outcome["outcome"], "recalled")
        self.assertEqual(self.ledger.get(delivery.id).state, "recalled")
        self.assertFalse(self.ledger.get(delivery.id).to_dict()["transferable"])
        with self.assertRaises(ConflictError):
            self.ledger.obtain(delivery.id, AFTER_RL1)

    def test_unknown_dispatch_goes_to_pending_verification(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), EARLY)
        self.ledger.mark_unknown(delivery.id, EARLY)
        outcome = self.ledger.revoke(delivery.id, AFTER_RL1)
        self.assertEqual(outcome["outcome"], "pending_verification")
        self.assertEqual(self.ledger.get(delivery.id).state, "pending_verification")
        self.assertEqual(len(self.ledger.pending_verification()), 1)

    def test_unstaged_dispatched_copy_is_withdrawn(self):
        delivery = self.deliver("O1", "C-1", ("W1", "W2"), stage=False)
        outcome = self.ledger.revoke(delivery.id, AFTER_RL1)
        self.assertEqual(outcome["outcome"], "withdrawn")

    def test_change_only_affects_inheriting_deliveries(self):
        property_pkg = self.deliver("O1", "C-P", ("W1", "W4"), obtain=True)  # 不含 B2
        resin_pkg = self.deliver("O1", "C-R", ("W1", "W2"), obtain=True)     # 继承 B2
        result = self.ledger.apply_change("RL1", AFTER_RL1)
        touched = {item["credential"] for item in result["outcomes"]}
        self.assertIn(resin_pkg.id, touched)
        self.assertNotIn(property_pkg.id, touched)
        self.assertEqual(self.ledger.get(property_pkg.id).state, "obtained")
        self.assertEqual(self.ledger.get(resin_pkg.id).state, "recalled")

    def test_change_does_not_touch_other_zone(self):
        cn = self.deliver("O2", "C-CN", ("W1", "W2"), obtain=True)
        result = self.ledger.apply_change("RL1", AFTER_RL1)
        self.assertNotIn(cn.id, {item["credential"] for item in result["outcomes"]})
        self.assertEqual(self.ledger.get(cn.id).state, "obtained")

    def test_ledger_round_trips(self):
        delivery = self.deliver("O1", "C-1", ("W1", "W2"), obtain=True)
        self.ledger.revoke(delivery.id, AFTER_RL1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            self.ledger.save(path)
            restored = Ledger.load(self.data, path)
        self.assertEqual(restored.get(delivery.id).state, "recalled")
        self.assertEqual(len(restored.events), len(self.ledger.events))


if __name__ == "__main__":
    unittest.main()


class UnknownResponseTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(snapshot())

    def test_unknown_dispatch_can_stage_but_stays_risky(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), EARLY)
        self.ledger.mark_unknown(delivery.id, EARLY)
        staged = self.ledger.stage(delivery.id, EARLY)
        self.assertEqual(staged.state, "staged_unconfirmed")
        self.assertFalse(staged.to_dict()["user_can_fetch"])
        self.assertTrue(any(row["credential"] == delivery.id
                            for row in self.ledger.freight_control_risk()))

    def test_unconfirmed_package_needs_verification_after_revocation(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), EARLY)
        self.ledger.mark_unknown(delivery.id, EARLY)
        self.ledger.stage(delivery.id, EARLY)
        outcome = self.ledger.revoke(delivery.id, AFTER_RL1)
        self.assertEqual(outcome["outcome"], "pending_verification")

    def test_confirmed_package_can_still_be_enabled(self):
        delivery = self.ledger.issue("O1", "C-1", ("W1", "W2"), EARLY)
        self.ledger.mark_unknown(delivery.id, EARLY)
        self.ledger.stage(delivery.id, EARLY)
        enabled = self.ledger.enable(delivery.id, EARLY)
        self.assertEqual(enabled.state, "enabled")
