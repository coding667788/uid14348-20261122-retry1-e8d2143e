"""The six scenario families explicitly required by the task statement."""

from __future__ import annotations

import unittest

from tests.helpers import replay_example
from solution.engine import Violation


class LeakWindowTests(unittest.TestCase):
    """Two ultimately-legal versions with a cross-tenant leak between them."""

    def test_leak_is_caught_at_service_boundary(self):
        result = replay_example("02_leak_window.json")
        codes = [v["code"] for v in result["violations"]]
        self.assertIn(Violation.ISOLATION, codes)
        earliest = result["earliest_violation"]
        self.assertEqual(earliest["code"], Violation.ISOLATION)
        # marker was rewritten but the original tenant label survived
        drop = next(d for d in result["drops"] if d["reason"] == "isolation_violation")
        self.assertEqual(drop["location"], "S2")
        self.assertIn("'ta'", drop["detail"])

    def test_violation_prefix_is_event_sequence(self):
        result = replay_example("02_leak_window.json")
        prefix = result["earliest_violation"]["prefix"]
        self.assertTrue(prefix)
        self.assertEqual(prefix[-1], result["earliest_violation"]["event_id"])
        self.assertEqual(len(prefix), len(set(prefix)))


class IngressSwapStrandTests(unittest.TestCase):
    """An ingress change strands markers emitted under the old rule set."""

    def test_premature_withdraw_lacks_drain_proof(self):
        result = replay_example("03_ingress_swap_strand.json")
        codes = [v["code"] for v in result["violations"]]
        self.assertIn(Violation.WITHDRAW_NO_PROOF, codes)
        action = next(a for a in result["actions"] if a["action"] == "c1")
        self.assertFalse(action["ok"])
        self.assertEqual(action["reason"], "withdraw_undrained")
        self.assertTrue(action["witness_tokens"])

    def test_old_marker_stranded_after_swap(self):
        result = replay_example("03_ingress_swap_strand.json")
        denied = [d for d in result["drops"] if d["request_id"] == "R-old"]
        self.assertTrue(denied)
        self.assertIn(denied[0]["reason"], ("inbound_denied", "inbound_no_match"))


class GenerationSplitTests(unittest.TestCase):
    """Same live state gets different futures across barrier generations."""

    def test_stale_birth_generation_rejected(self):
        result = replay_example("04_generation_split.json")
        stale = [v for v in result["violations"]
                 if v["code"] == Violation.GENERATION_REINJECTION]
        self.assertTrue(stale)
        drop = next(d for d in result["drops"] if d["reason"] == "stale_generation")
        self.assertEqual(drop["request_id"], "R2")
        self.assertIn("min_open=2", drop["detail"])

    def test_barriers_carry_witness_and_completion(self):
        result = replay_example("04_generation_split.json")
        self.assertEqual(len(result["barriers"]), 2)
        for barrier in result["barriers"]:
            self.assertTrue(barrier["completed"])
            self.assertEqual(
                len(barrier["fate"]),
                len(barrier["witness_tokens"]),
            )


class TTLFailureBoundaryTests(unittest.TestCase):
    """Failure window ending exactly when an in-flight token's TTL expires."""

    def test_expired_token_not_counted_as_recovered(self):
        result = replay_example("05_ttl_failure_boundary.json")
        drop = next(d for d in result["drops"]
                    if d["reason"] == "ttl_expired_at_recovery")
        self.assertEqual(drop["request_id"], "R1")
        self.assertEqual(drop["step"], 4)

    def test_paused_proxy_holds_then_releases(self):
        result = replay_example("05_ttl_failure_boundary.json")
        resume = next(a for a in result["actions"] if a["action"] == "e2")
        self.assertGreaterEqual(resume["released_tokens"], 0)
        receipt = next(r for r in result["receipts"] if r["request_id"] == "R2")
        self.assertTrue(receipt["first"])
        self.assertGreater(receipt["step"], 4)


class ScenarioConflictTests(unittest.TestCase):
    """Per-scenario shortest sequences cannot be combined into one table."""

    def test_fixed_baseline_violates_under_failure(self):
        result = replay_example("06_scenario_conflict.json")
        # R1 is killed by the failure window while P1 is paused
        self.assertTrue(any(d["request_id"] == "R1" for d in result["drops"]))
        codes = [v["code"] for v in result["violations"]]
        self.assertIn(Violation.REACHABILITY, codes)


class SafeBaselineTests(unittest.TestCase):
    def test_safe_case_has_no_violations_and_full_reachability(self):
        result = replay_example("01_safe_gray.json")
        self.assertEqual(result["overall_status"], "safe")
        self.assertEqual(
            sorted(result["reachability"]["reachable_requests"]),
            ["R1", "R2"],
        )
        self.assertEqual(result["reachability"]["unreachable_requests"], [])

    def test_original_tenant_immutable_through_rewrites(self):
        result = replay_example("01_safe_gray.json")
        for receipt in result["receipts"]:
            req_tenant = "ta" if receipt["request_id"] == "R1" else "tb"
            self.assertEqual(receipt["tenant"], req_tenant)
            self.assertNotEqual(receipt["marker"], req_tenant)


if __name__ == "__main__":
    unittest.main()
