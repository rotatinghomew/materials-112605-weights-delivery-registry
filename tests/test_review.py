"""发布审查、生效批准与订单兑现。"""
from __future__ import annotations

import unittest

from support import AFTER_RL1, AFTER_S4, BEFORE_RL1, EARLY, snapshot
from exchange import all_audits, approvals, check_switch, evaluate, plan, plans, versions


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.data = snapshot()

    def audit(self, key):
        return evaluate(self.data, self.data.by_id("audits", key))

    def test_approval_pins_concrete_digests(self):
        result = self.audit("S1")
        self.assertTrue(result.approved)
        expected = [self.data.by_id("weights", x).sha256 for x in ("W1", "W2")]
        self.assertEqual(list(result.approval["digests"]), expected)
        self.assertEqual(result.approval["coverage"], ["聚合物", "薄膜"])

    def test_audit_requires_full_family_coverage(self):
        result = self.audit("S3")
        # O3 要求三相材料族，W1+W2 缺屏蔽，但候选里带了 W5。
        self.assertTrue(result.approved)
        self.assertEqual(result.approval["coverage"], ["屏蔽", "聚合物", "薄膜"])

    def test_approval_does_not_use_later_signed_license(self):
        # 批准只能依据审查当时已生效的授权，不能拿后签的许可顶替。
        from exchange.clock import parse_time
        result = self.audit("S1")
        self.assertTrue(result.approved)
        for grant in result.approval["grants"]:
            license = self.data.by_id("licenses", grant["license"])
            self.assertLessEqual(parse_time(license.begins), parse_time("2027-01-18T10:00:00+08:00"))
            self.assertGreater(parse_time(license.expires), parse_time("2027-01-18T10:00:00+08:00"))

    def test_approval_dies_when_its_scope_is_revoked(self):
        # S1 钉的 W2 继承 L2；撤销 EU/review 后 S1 不再生效。
        from exchange import effective_approval
        self.assertIsNotNone(effective_approval(self.data, "O1", BEFORE_RL1))
        self.assertIsNone(effective_approval(self.data, "O1", AFTER_RL1))

    def test_unrelated_change_does_not_void_inflight_approval(self):
        from exchange import effective_approval
        # RL1 只撤销 EU/review 的 L2，CN 的订单 O2 不受影响。
        self.assertIsNotNone(effective_approval(self.data, "O2", AFTER_RL1))
        # RL2 是 L5，与 O1/O2 无关。
        self.assertIsNotNone(effective_approval(self.data, "O2", AFTER_S4))

    def test_later_audit_supersedes_with_new_composition(self):
        from exchange import effective_approval
        later = effective_approval(self.data, "O1", "2027-01-18T12:00:00+08:00")
        self.assertIsNotNone(later)
        self.assertEqual(later.audit.id, "S4")
        self.assertEqual(list(later.approval["weights"]), ["W1", "W4"])

    def test_approval_not_yet_issued_is_not_effective(self):
        from exchange import effective_approval
        # S4 在 11:45 才审查；11:30 不能拿它当授权。
        self.assertIsNone(effective_approval(self.data, "O1", "2027-01-18T11:30:00+08:00"))

    def test_plan_uses_approval_when_still_valid(self):
        result = plan(self.data, "O1", EARLY)
        self.assertFalse(result.substituted)
        self.assertEqual(result.delivery.ids, ("W1", "W2"))

    def test_plan_substitutes_after_revocation(self):
        result = plan(self.data, "O1", AFTER_RL1)
        self.assertTrue(result.substituted)
        self.assertEqual(result.delivery.ids, ("W1", "W4"))
        reasons = {item["reason"] for item in result.reasons}
        self.assertIn("substituted_alternative", reasons)

    def test_replacement_needs_order_permission(self):
        from exchange import alternatives
        from exchange.fulfilment import check_switch
        # O2 的批准在撤销后仍然有效，因此不需要替换。
        from exchange import effective_approval
        self.assertIsNotNone(effective_approval(self.data, "O2", AFTER_RL1))
        # 若真的改成 W1+W4，虽然覆盖与基座都符合，但 O2 不允许替换。
        result = check_switch(self.data, "O2", ("W1", "W4"), AFTER_RL1)
        self.assertFalse(result["checks"]["replace_allowed"])
        self.assertFalse(result["checks"]["acceptable"])

    def test_switch_checks_three_conditions(self):
        accepted = check_switch(self.data, "O1", ("W1", "W4"), AFTER_RL1)
        self.assertTrue(accepted["checks"]["acceptable"])
        self.assertTrue(accepted["checks"]["base_compatible"])

        missing_family = check_switch(self.data, "O1", ("W1",), AFTER_RL1)
        self.assertFalse(missing_family["checks"]["coverage"])
        self.assertIn("聚合物", missing_family["checks"]["coverage_missing"])
        self.assertFalse(missing_family["checks"]["acceptable"])

        incompatible = check_switch(self.data, "O3", ("W6", "W5"), AFTER_RL1)
        self.assertFalse(incompatible["checks"]["base_compatible"])

    def test_customer_can_see_versions_and_zones(self):
        view = versions(self.data, "O3", AFTER_RL1)
        self.assertTrue(view["alternatives"])
        self.assertEqual(view["allowed_zones"], ["EU"])
        first = view["alternatives"][0]
        self.assertEqual(first["coverage"], ["屏蔽", "聚合物", "薄膜"])
        self.assertIn("W5", first["weights"])

    def test_plans_never_deliver_uncovered_sets(self):
        for item in plans(self.data, AFTER_RL1):
            if item["delivery"]:
                self.assertTrue(set(item["families"]) <= set(item["delivery"]["coverage"]))


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.data = snapshot()

    def test_composition_comes_from_registration(self):
        result = plan(self.data, "O2", BEFORE_RL1)
        self.assertEqual(result.delivery.ids, ("W1", "W2"))
        composition = result.to_dict(self.data)["delivery"]["composition"]
        self.assertEqual(composition["bases"], ["W1"])
        self.assertEqual(composition["adapters"], ["W2"])
        for item in composition["files"]:
            self.assertEqual(item["file"], f"artifacts/{item['weight']}.pack")
            self.assertEqual(item["sha256"], self.data.by_id("weights", item["weight"]).sha256)

    def test_whole_set_delivery_is_marked(self):
        # O3 需要三个材料族，没有单包恰好覆盖，因此以多包组合交付。
        result = plan(self.data, "O3", EARLY)
        self.assertGreater(len(result.delivery.ids), 1)
        self.assertFalse(result.to_dict(self.data)["delivery"]["whole_set"])


if __name__ == "__main__":
    unittest.main()
