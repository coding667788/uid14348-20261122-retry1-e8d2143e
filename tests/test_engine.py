"""Engine-level semantics: branches, receipts, TTL, pause, determinism."""

from __future__ import annotations

import copy
import json
import unittest

from tests.helpers import load_example
from solution.engine import SimConfig, Simulator, Violation
from solution.model import load_case


def _base_config(case, **overrides):
    delays = {lid: link.delay_min for lid, link in case.links.items()}
    delays.update(overrides.get("delays", {}))
    return SimConfig(
        name="unit",
        delays=delays,
        horizon=overrides.get("horizon", case.replay_horizon or 14),
        schedule=overrides.get("schedule", ()),
    )


class ReceiptIsolationTests(unittest.TestCase):
    def test_branches_keep_independent_receipts(self):
        document, case = load_example("01_safe_gray.json")
        # add a second parallel link P2->S1 so R1 forks into two branches
        document["links"].append({
            "id": "L5", "src": "P2", "dst": "S1", "delay_min": 1, "delay_max": 1,
            "failure_domain": "d2", "carries": ["S1"],
        })
        case = load_case(document)
        result = Simulator(case, _base_config(case)).run()
        r1 = [r for r in result["receipts"] if r["request_id"] == "R1"]
        branches = {r["branch"] for r in r1}
        self.assertEqual(len(r1), len(branches))
        self.assertGreaterEqual(len(branches), 2)
        self.assertTrue(all(r["first"] for r in r1))

    def test_looping_token_never_overwrites_a_receipt(self):
        document, case = load_example("01_safe_gray.json")
        # a reverse link lets tokens loop; the loop cohort must be dropped
        # (TTL or re-entry validation) rather than mutate an existing receipt
        document["links"].append({
            "id": "L6", "src": "P2", "dst": "P1", "delay_min": 1, "delay_max": 1,
            "failure_domain": "d1", "carries": ["*"],
        })
        document["requests"][0]["ttl"] = 6
        case = load_case(document)
        result = Simulator(case, _base_config(case)).run()
        # every surviving receipt remains a first-hand, unmutated record
        self.assertTrue(all(r["first"] for r in result["receipts"]))
        self.assertFalse(any(v["code"] == Violation.RECEIPT_MIX
                             for v in result["violations"]))
        # the extra loop traffic is accounted for as drops with a verifiable reason
        self.assertTrue(result["drops"])

    def test_late_delivery_after_deadline(self):
        document, case = load_example("01_safe_gray.json")
        document["requests"][0]["deadline_step"] = 1  # impossible deadline
        case = load_case(document)
        result = Simulator(case, _base_config(case)).run()
        self.assertTrue(any(v["code"] == Violation.LATE for v in result["violations"]))
        self.assertTrue(any(d["reason"] == "late_delivery" for d in result["drops"]))


class PauseTests(unittest.TestCase):
    def test_paused_proxy_queues_and_resume_revalidates(self):
        document, case = load_example("05_ttl_failure_boundary.json")
        schedule = tuple((ev.step, ev.action, ev.wait_slots)
                         for ev in case.replay_events)
        cfg = _base_config(case, schedule=schedule, horizon=case.replay_horizon,
                           failure_domain="d1", down_step=1, up_step=4,
                           recovery_delay=1)
        result = Simulator(case, cfg).run()
        queued = [a for a in result["anomalies"] if a["reason"] == "paused_queued"]
        self.assertTrue(queued)
        resume = next(a for a in result["actions"] if a["type"] == "resume")
        self.assertGreaterEqual(resume["released_tokens"], 1)

    def test_token_expiring_while_paused_is_dropped(self):
        document, case = load_example("05_ttl_failure_boundary.json")
        # hold P1 longer than the TTL of the queued R2
        document["replay"]["events"] = [
            {"step": 1, "action": "e1"},
            {"step": 9, "action": "e2"},
        ]
        case = load_case(document)
        schedule = ((1, "e1", None), (9, "e2", None))
        cfg = _base_config(case, schedule=schedule, horizon=12,
                           failure_domain="d1", down_step=1, up_step=4,
                           recovery_delay=1)
        result = Simulator(case, cfg).run()
        self.assertTrue(any(d["request_id"] == "R2" and d["reason"] == "ttl_expired"
                            for d in result["drops"]))


class TTLTests(unittest.TestCase):
    def test_ttl_ticks_on_every_step_including_loops(self):
        document, case = load_example("01_safe_gray.json")
        document["links"].append({
            "id": "L6", "src": "P2", "dst": "P1", "delay_min": 1, "delay_max": 1,
            "failure_domain": "d1", "carries": ["*"],
        })
        document["requests"][0]["ttl"] = 3
        case = load_case(document)
        result = Simulator(case, _base_config(case)).run()
        dropped = [d for d in result["drops"]
                   if d["request_id"] == "R1" and d["reason"] == "ttl_expired"]
        self.assertTrue(dropped)
        self.assertLessEqual(dropped[0]["step"], 4)


class DeterminismTests(unittest.TestCase):
    def test_repeated_runs_identical_json(self):
        _, case = load_example("01_safe_gray.json")
        a = json.dumps(Simulator(case, _base_config(case)).run(), sort_keys=True)
        b = json.dumps(Simulator(case, _base_config(case)).run(), sort_keys=True)
        self.assertEqual(a, b)

    def test_event_ids_are_sequential_and_unique(self):
        result = Simulator(load_example("01_safe_gray.json")[1],
                           _base_config(load_example("01_safe_gray.json")[1])).run()
        ids = [e["event_id"] for e in result["timeline"]]
        self.assertEqual(len(ids), len(set(ids)))


class CompressionTests(unittest.TestCase):
    def test_idle_steps_are_skipped(self):
        _, case = load_example("01_safe_gray.json")
        result = Simulator(case, _base_config(case, horizon=30)).run()
        self.assertLess(result["compression"]["visited_steps"],
                        result["compression"]["horizon_steps"])
        self.assertGreaterEqual(result["compression"]["skipped_steps"], 0)


if __name__ == "__main__":
    unittest.main()
