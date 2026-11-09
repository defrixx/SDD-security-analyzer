"""General exclusion authority tests: no topic keywords and no model requests."""
import copy
import unittest

from spec_enricher.review_scope import exclusion_guard


def record(expression=None, logic=None):
    return {
        "applicability": {"expression": expression or {"predicate": "feature"}},
        "clause_logic": logic or {"clause": "control"},
        "clause_normativity": {"control": {"force": "required"}},
    }


class ReviewScopeTests(unittest.TestCase):
    def test_feature_present_missing_control_and_feature_absence_both_require_confirmation(self):
        # The validator cannot distinguish these natural-language meanings from
        # the same false model claim. Neither can silently remove an obligation.
        for scenario in ("feature-present-control-absent", "truly-irrelevant", "explicit-negative-context"):
            with self.subTest(scenario=scenario):
                facts = {"feature": False}
                original = copy.deepcopy(facts)
                guard = exclusion_guard(record(), facts, {})
                self.assertTrue(guard["requires_confirmation"])
                self.assertIsNone(guard["effective_applicability"])
                self.assertEqual(facts, original)

    def test_negation_exclusion_from_true_is_also_held(self):
        guard = exclusion_guard(record({"not": {"predicate": "feature"}}), {"feature": True}, {})
        self.assertTrue(guard["requires_confirmation"])

    def test_any_all_exclusion_requires_sufficient_explicit_decisions(self):
        for operator in ("any", "all"):
            r = record({operator: [{"predicate": "a"}, {"predicate": "b"}]})
            self.assertTrue(exclusion_guard(r, {"a": False, "b": False}, {})["requires_confirmation"])
            self.assertFalse(exclusion_guard(r, {"a": False, "b": False}, {"a": False, "b": False})["requires_confirmation"])
        r = record({"any": [{"predicate": "a"}, {"predicate": "b"}]})
        self.assertTrue(exclusion_guard(r, {"a": False, "b": False}, {"a": False})["requires_confirmation"])

    def test_conditional_clause_excluded_by_either_polarity_is_held(self):
        for conditional, fact in (({"predicate": "branch"}, False), ({"not": {"predicate": "branch"}}, True)):
            r = record(logic={"when": conditional, "then": {"clause": "control"}, "else": "not_applicable_branch"})
            guard = exclusion_guard(r, {"feature": True, "branch": fact}, {})
            self.assertTrue(guard["requires_confirmation"])
            self.assertEqual(guard["excluded_clause_ids"], ["control"])
            self.assertTrue(guard["effective_applicability"])
            self.assertFalse(exclusion_guard(r, {"feature": True, "branch": fact}, {"branch": fact})["requires_confirmation"])

    def test_explicit_overrides_unknown_and_context_only(self):
        self.assertFalse(exclusion_guard(record(), {"feature": False}, {"feature": False})["requires_confirmation"])
        self.assertTrue(exclusion_guard(record(), {"feature": True}, {"feature": True})["effective_applicability"])
        unknown = exclusion_guard(record(), {"feature": None}, {})
        self.assertIsNone(unknown["effective_applicability"])
        self.assertFalse(unknown["requires_confirmation"])
        r = record()
        r["clause_normativity"]["control"]["force"] = "not_a_requirement"
        self.assertFalse(exclusion_guard(r, {"feature": False}, {})["requires_confirmation"])
