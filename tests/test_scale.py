"""Performance and bounded-search guarantees at the stated limits."""

from __future__ import annotations

import time
import unittest

from solution.model import load_case
from solution.planner import (
    ScheduleBuilder, build_scenarios, run_plan, static_reachability_bounds,
)
from examples.scale_build import build


class ScaleLimitTests(unittest.TestCase):
    def test_structural_precheck_rejects_long_chain_instantly(self):
        case = load_case(build(feasible=False))
        start = time.perf_counter()
        result = run_plan(case, build(feasible=False)["plan"])
        elapsed = time.perf_counter() - start
        self.assertFalse(result["feasible"])
        self.assertEqual(result["reason"], "structurally_unreachable")
        self.assertEqual(result["schedules_evaluated"], 0)
        self.assertLess(elapsed, 5.0)
        dead = next(r for r in result["static_reachability"]
                    if r["structurally_unreachable"])
        self.assertGreater(dead["minimum_path_steps"], dead["ttl"])

    def test_feasible_max_scale_terminates_within_budget(self):
        doc = build(feasible=True)
        case = load_case(doc)
        start = time.perf_counter()
        result = run_plan(case, doc["plan"])
        elapsed = time.perf_counter() - start
        self.assertEqual(
            sum(1 for n in case.nodes.values() if n.kind == "proxy"), 14
        )
        self.assertEqual(len(case.actions), 16)
        self.assertEqual(case.plan_slots, 10)
        self.assertLess(elapsed, 30.0)
        self.assertTrue(result["feasible"])
        self.assertIn("search_truncated", result)

    def test_static_bounds_match_topology(self):
        case = load_case(build(feasible=True))
        report = static_reachability_bounds(case)
        row = report[0]
        # IG1 -> P1 -> S1 with unit delays
        self.assertEqual(row["minimum_path_steps"], 2)
        self.assertFalse(row["structurally_unreachable"])

    def test_envelope_deduplicates_corner_scenarios(self):
        case = load_case(build(feasible=True))
        configs = build_scenarios(case, list(range(case.plan_slots)), {})
        signatures = {
            (tuple(sorted(c.delays.items())), c.failure_domain, c.down_step)
            for c in configs
        }
        self.assertEqual(len(signatures), len(configs))


if __name__ == "__main__":
    unittest.main()
