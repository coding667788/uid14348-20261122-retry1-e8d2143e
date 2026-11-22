"""Planner: objective minimisation, infeasibility reporting, exhaustive cross-check."""

from __future__ import annotations

import unittest

from tests.helpers import load_example
from solution.model import load_case
from solution.planner import (
    ScheduleBuilder, build_scenarios, evaluate_factory, exhaustive_crosscheck,
    run_plan,
)


class PlannerFeasibleTests(unittest.TestCase):
    def test_safe_case_yields_minimal_objective(self):
        document, case = load_example("01_safe_gray.json")
        result = run_plan(case, document.get("plan"))
        self.assertTrue(result["feasible"])
        obj = result["objective"]
        self.assertEqual(obj["total_paused_proxy_slots"], 0)
        # precedence a1<a2<a3<a4 is respected in the emitted sequence
        seq = obj["action_id_sequence"]
        self.assertEqual(seq, ["a1", "a2", "a3", "a4"])
        slots = [s["slot"] for s in result["schedule"]]
        self.assertEqual(slots, sorted(slots))

    def test_schedule_is_safe_in_every_envelope_scenario(self):
        document, case = load_example("01_safe_gray.json")
        result = run_plan(case, document.get("plan"))
        configs = build_scenarios(case, list(range(case.plan_slots)), {})
        evaluate = evaluate_factory(case, configs)
        from solution.planner import Placement
        placements = tuple(
            Placement(s["slot"], s["action"], s["wait_slots"])
            for s in result["schedule"]
        )
        verdict = evaluate(placements)
        self.assertIsNotNone(verdict)
        self.assertGreater(result["scenarios_evaluated"], 1)


class PlannerInfeasibleTests(unittest.TestCase):
    def test_leak_cannot_be_planned_away(self):
        document, case = load_example("02_leak_window.json")
        result = run_plan(case, document.get("plan"))
        self.assertFalse(result["feasible"])
        report = result["baseline_report"]
        self.assertEqual(report["kind"], "simulation_failure")
        self.assertEqual(report["code"], "isolation_violation")
        self.assertTrue(report["prefix"])

    def test_undrained_withdraw_shortest_prefix_reported(self):
        document, case = load_example("03_ingress_swap_strand.json")
        result = run_plan(case, document.get("plan"))
        self.assertFalse(result["feasible"])
        self.assertEqual(
            result["baseline_report"]["code"],
            "withdraw_without_drain_proof",
        )

    def test_baseline_scheduling_conflict_detected(self):
        document, case = load_example("01_safe_gray.json")
        # only 3 slots for 4 strictly-ordered actions: no legal table exists,
        # and the baseline itself also violates the precedence ordering
        document["plan"]["slots"] = 3
        document["baseline"] = [
            {"slot": 0, "action": "a1"},
            {"slot": 1, "action": "a2", "wait_slots": 1},
            {"slot": 1, "action": "a4"},
            {"slot": 2, "action": "a3"},
        ]
        # the reference parser rejects same-slot duplicates, so move a4 to a
        # distinct slot that still precedes its required predecessor a3
        document["baseline"] = [
            {"slot": 0, "action": "a1"},
            {"slot": 1, "action": "a4"},
            {"slot": 2, "action": "a2", "wait_slots": 1},
        ]
        case = load_case(document)
        result = run_plan(case, document.get("plan"))
        self.assertFalse(result["feasible"])
        self.assertEqual(result["reason"], "baseline_scheduling_conflict")
        self.assertIn(result["earliest_conflict"]["kind"],
                      ("precedence_unsatisfied", "precedence_order_violation"))


class ExhaustiveCrosscheckTests(unittest.TestCase):
    def test_dfs_optimum_matches_brute_force_on_small_topology(self):
        document, case = load_example("01_safe_gray.json")
        # shrink the search space so full enumeration stays tiny
        document["plan"]["slots"] = 5
        document["plan"]["max_wait_slots"] = 1
        case = load_case(document)
        configs = build_scenarios(case, list(range(case.plan_slots)), {})
        cross = exhaustive_crosscheck(
            case, configs, case.plan_slots, case.max_wait_slots
        )
        self.assertIsNotNone(cross["optimum"])
        self.assertGreater(cross["feasible_schedules"], 0)

        result = run_plan(case, document.get("plan"))
        self.assertTrue(result["feasible"])
        self.assertEqual(
            tuple(result["objective"]["action_id_sequence"]),
            cross["optimum"]["objective"][2],
        )
        self.assertEqual(
            result["objective"]["release_slots_used"],
            cross["optimum"]["objective"][1],
        )
        self.assertEqual(
            result["objective"]["total_paused_proxy_slots"],
            cross["optimum"]["objective"][0],
        )

    def test_objective_ordering_prefers_less_pause_time(self):
        _, case = load_example("01_safe_gray.json")
        builder = ScheduleBuilder(case, case.plan_slots, case.max_wait_slots)
        from solution.planner import Placement
        a = (Placement(0, "a1", None), Placement(1, "a2", 1),
             Placement(3, "a3", None), Placement(4, "a4", None))
        b = (Placement(1, "a1", None), Placement(2, "a2", 1),
             Placement(4, "a3", None), Placement(6, "a4", None))
        self.assertLess(builder._objective(a), builder._objective(b))


if __name__ == "__main__":
    unittest.main()
