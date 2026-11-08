import json
import unittest
from pathlib import Path

from spec_enricher.domain import (
    CONTRADICTED,
    NOT_ADDRESSED,
    NOT_ASSESSED,
    SATISFIED,
    UNKNOWN_ASSESSMENT,
    Evaluation,
    ExpressionError,
    clauses,
    condition,
)


class DomainTests(unittest.TestCase):
    def test_three_valued_logic(self):
        expr = {"all": [{"predicate": "a"}, {"not": {"predicate": "b"}}]}
        self.assertIsNone(condition(expr, {"a": True}))
        self.assertFalse(condition(expr, {"a": False}))
        self.assertTrue(condition(expr, {"a": True, "b": False}))
        self.assertTrue(condition({"any": [{"predicate": "a"}, {"predicate": "b"}]}, {"a": True}))
        with self.assertRaises(ExpressionError):
            condition({"exec": "anything"}, {})

    def test_aggregation_and_incomplete_flag(self):
        n = {x: {"force": "required"} for x in "abc"}
        tree = {"all": [{"clause": "a"}, {"clause": "b"}]}
        self.assertEqual(
            clauses(tree, {}, {"a": CONTRADICTED, "b": UNKNOWN_ASSESSMENT}, n),
            Evaluation(CONTRADICTED, None, True),
        )
        tree = {"any": [{"clause": "a"}, {"clause": "b"}]}
        self.assertEqual(
            clauses(tree, {}, {"a": SATISFIED, "b": CONTRADICTED}, n).outcome, SATISFIED
        )

    def test_context_guard_and_force(self):
        n = {"a": {"force": "not_a_requirement"}, "b": {"force": "required"}}
        self.assertEqual(
            clauses({"clause": "a"}, {}, {}, n), Evaluation(NOT_ASSESSED, "no_active_clauses")
        )
        context_guard = {
            "when": {"predicate": "p"},
            "then": {"clause": "a"},
            "else": "not_applicable_branch",
        }
        self.assertEqual(
            clauses(context_guard, {}, {}, n), Evaluation(NOT_ASSESSED, "no_active_clauses")
        )
        tree = {
            "when": {"predicate": "p"},
            "then": {"clause": "b"},
            "else": "not_applicable_branch",
        }
        self.assertEqual(clauses(tree, {"p": False}, {}, n).outcome, NOT_ASSESSED)
        self.assertEqual(clauses(tree, {}, {}, n).outcome, UNKNOWN_ASSESSMENT)
        self.assertEqual(clauses(tree, {"p": True}, {"b": NOT_ADDRESSED}, n).outcome, NOT_ADDRESSED)
        n["a"]["force"] = "recommended"
        self.assertEqual(
            clauses(
                {"any": [{"clause": "a"}, {"clause": "b"}]}, {}, {}, n, enforcement=True
            ).reason,
            "mixed_force_alternatives",
        )
        guarded = {"any": [context_guard, {"clause": "b"}]}
        self.assertEqual(
            clauses(guarded, {"p": False}, {"b": SATISFIED}, n, enforcement=True).outcome, SATISFIED
        )

    def test_actual_agentic_record_preserves_force_and_guard(self):
        catalog = json.loads(Path("catalog/requirements.full.json").read_text())
        record = next(
            x for x in catalog["requirements"] if x["record_key"] == "agentic.threat-model"
        )
        self.assertEqual(record["source_kind"], "review_criterion")
        self.assertEqual(
            [record["clause_normativity"][x]["force"] for x in ("c1", "c2", "c3")], ["required"] * 3
        )
        n = record["clause_normativity"]
        actual = clauses(
            record["clause_logic"],
            {"p2": False},
            {"c1": SATISFIED, "c2": CONTRADICTED, "c3": SATISFIED},
            n,
            enforcement=True,
        )
        self.assertEqual(actual.outcome, SATISFIED)


if __name__ == "__main__":
    unittest.main()
