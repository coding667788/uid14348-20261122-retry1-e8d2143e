"""Structural validation: field-path error codes and hard limit enforcement."""

from __future__ import annotations

import copy
import unittest

from tests.helpers import load_example
from solution.errors import ErrorCode
from solution.model import MAX_ACTIONS, MAX_PROXIES, MAX_SLOTS, MAX_TTL, load_case


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.document, _ = load_example("01_safe_gray.json")

    def _expect(self, mutate, code, path_fragment):
        doc = copy.deepcopy(self.document)
        mutate(doc)
        with self.assertRaises(Exception) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, code)
        self.assertIn(path_fragment, ctx.exception.path)

    def test_top_level_must_be_object(self):
        with self.assertRaises(Exception) as ctx:
            load_case([1, 2, 3])
        self.assertEqual(ctx.exception.code, ErrorCode.NOT_AN_OBJECT)

    def test_missing_field_path(self):
        def mutate(doc):
            del doc["nodes"]
        self._expect(mutate, ErrorCode.MISSING_FIELD, "$")

    def test_wrong_type_path(self):
        def mutate(doc):
            doc["nodes"] = "nope"
        self._expect(mutate, ErrorCode.WRONG_TYPE, "nodes")

    def test_duplicate_node_id(self):
        def mutate(doc):
            doc["nodes"].append(dict(doc["nodes"][0]))
        self._expect(mutate, ErrorCode.DUPLICATE_ID, "nodes")

    def test_unknown_reference(self):
        def mutate(doc):
            doc["links"][0]["dst"] = "GHOST"
        self._expect(mutate, ErrorCode.UNKNOWN_REFERENCE, "links")

    def test_ttl_limit(self):
        def mutate(doc):
            doc["requests"][0]["ttl"] = MAX_TTL + 1
        self._expect(mutate, ErrorCode.ILLEGAL_VALUE, "ttl")

    def test_delay_order(self):
        def mutate(doc):
            doc["links"][0]["delay_min"], doc["links"][0]["delay_max"] = 3, 1
        self._expect(mutate, ErrorCode.ILLEGAL_VALUE, "delay_min")

    def test_precedence_cycle(self):
        def mutate(doc):
            doc["precedence"].append(["a4", "a1"])
        self._expect(mutate, ErrorCode.SEMANTIC_CONFLICT, "precedence")

    def test_action_cardinality_limit(self):
        doc = copy.deepcopy(self.document)
        doc["actions"] = [
            {"id": f"x{i}", "type": "pause", "proxy": "P1"} for i in range(MAX_ACTIONS + 1)
        ]
        doc["precedence"] = []
        with self.assertRaises(Exception) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, ErrorCode.CARDINALITY_VIOLATION)

    def test_proxy_limit(self):
        doc = copy.deepcopy(self.document)
        proxies = [
            {"id": f"PX{i}", "kind": "proxy", "region": "r"}
            for i in range(MAX_PROXIES + 1)
        ]
        doc["nodes"] = [n for n in doc["nodes"] if n["kind"] != "proxy"] + proxies
        with self.assertRaises(Exception) as ctx:
            load_case(doc)
        self.assertEqual(ctx.exception.code, ErrorCode.CARDINALITY_VIOLATION)

    def test_slot_limit(self):
        def mutate(doc):
            doc["plan"]["slots"] = MAX_SLOTS + 1
        self._expect(mutate, ErrorCode.ILLEGAL_VALUE, "slots")

    def test_unknown_policy_reference(self):
        def mutate(doc):
            doc["initial_state"]["outbound"][0]["active_version"] = 99
        self._expect(mutate, ErrorCode.UNKNOWN_REFERENCE, "active_version")


if __name__ == "__main__":
    unittest.main()
