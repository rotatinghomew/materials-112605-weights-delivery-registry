"""演进与实习：谱系、继承义务和许可边界。"""
from __future__ import annotations

import unittest

from support import AFTER_RL1, EARLY, snapshot
from exchange import (
    affected_weights,
    ancestors,
    boundary,
    entitlement,
    inherits_license,
    obligations,
    trainable,
)


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.data = snapshot()

    def test_ancestors_follow_parents_and_base(self):
        self.assertEqual([w.id for w in ancestors(self.data, "W3")], ["W3", "W1", "W2"])
        self.assertEqual([w.id for w in ancestors(self.data, "W6")], ["W6"])

    def test_inherited_batches_include_upstream_even_if_not_direct(self):
        # W3 直接只读 B3，但上游 W2 的 B2 义务仍然随包交付。
        self.assertEqual(obligations(self.data, "W3"), ("B1", "B2", "B3"))
        self.assertEqual(obligations(self.data, "W2"), ("B1", "B2"))

    def test_base_counts_even_without_parent_listing(self):
        # 只声明 base_id、不列 parents 也不能摆脱基座的数据义务。
        weight = next(w for w in self.data.weights if w.id == "W4")
        self.assertEqual(weight.base_id, "W1")
        self.assertIn("B1", obligations(self.data, "W4"))

    def test_removing_metadata_reference_does_not_clear_obligation(self):
        # W2 的 data 仍列 B2；即使有人删掉这条引用，基座 W1 的 B1 也还在。
        self.assertIn("B1", obligations(self.data, "W2"))
        self.assertTrue(inherits_license(self.data, "W2", "L1"))

    def test_retracted_transfer_only_hits_inheriting_packages(self):
        affected = set(affected_weights(self.data, "RL1"))
        self.assertEqual(affected, {"W2", "W3", "W6"})
        self.assertNotIn("W4", affected)
        self.assertNotIn("W5", affected)

    def test_entitlement_reports_revoked_scope(self):
        before = entitlement(self.data, "W2", "review", "EU", "2027-01-18T10:59:00+08:00")
        after = entitlement(self.data, "W2", "review", "EU", AFTER_RL1)
        self.assertTrue(before.allowed)
        self.assertFalse(after.allowed)
        denial = next(d for d in after.denials if d.batch == "B2")
        self.assertEqual(denial.reason, "revoked")
        self.assertEqual(denial.change, "RL1")

    def test_revocation_is_scoped_to_use_and_zone(self):
        # 撤销的是 EU/review，CN 与 internal 不受影响。
        self.assertTrue(entitlement(self.data, "W2", "review", "CN", AFTER_RL1).allowed)
        self.assertTrue(entitlement(self.data, "W2", "internal", "EU", AFTER_RL1).allowed)

    def test_internal_scope_survives_for_next_training_round(self):
        result = trainable(self.data, "W5", "EU", AFTER_RL1)
        # L5 只签了 EU，但撤销的是 review，不是 internal。
        self.assertTrue(result.allowed)
        self.assertEqual(result.grants[0].use, "internal")

    def test_internal_requires_internal_grant(self):
        result = entitlement(self.data, "W5", "internal", "CN", AFTER_RL1)
        self.assertFalse(result.allowed)
        self.assertEqual(result.denials[0].reason, "not_in_scope")

    def test_boundary_annotates_coverage_and_denials(self):
        result = boundary(self.data, "W3", AFTER_RL1)
        self.assertEqual(result["coverage"], ["封装"])
        self.assertEqual(result["obligations"], ["B1", "B2", "B3"])
        self.assertFalse(result["uses"]["review"]["EU"]["allowed"])
        self.assertTrue(result["uses"]["internal"]["EU"]["allowed"])
        reasons = {d["reason"] for d in result["uses"]["review"]["EU"]["denials"]}
        self.assertIn("revoked", reasons)

    def test_unknown_use_is_never_authorized(self):
        result = entitlement(self.data, "W1", "publish", "EU", EARLY)
        self.assertFalse(result.allowed)
        self.assertEqual(result.denials[0].reason, "not_in_scope")

    def test_expiry_closes_delivery(self):
        result = entitlement(self.data, "W1", "review", "EU", "2027-01-18T19:00:00+08:00")
        self.assertFalse(result.allowed)
        self.assertIn("expired", {d.reason for d in result.denials})

    def test_later_signed_license_cannot_backfill(self):
        result = entitlement(self.data, "W1", "review", "EU", "2027-01-18T07:00:00+08:00")
        self.assertFalse(result.allowed)
        self.assertIn("not_yet_effective", {d.reason for d in result.denials})


if __name__ == "__main__":
    unittest.main()
