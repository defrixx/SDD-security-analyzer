import unittest

from spec_enricher.assessment import Assessment, effect, record_effect
from spec_enricher.evaluation import matrix_row
from spec_enricher.pipeline import implementation_claim


class AssessmentTests(unittest.TestCase):
    def test_new_path_gap_with_unchanged_baseline_is_introduced_not_regressed(self):
        record = {
            "applicability": {"expression": {"predicate": "scope"}},
            "clause_logic": {"all": [{"clause": "access"}, {"clause": "review"}]},
            "clause_normativity": {
                cid: {"force": "required", "nature": "technical_control"}
                for cid in ("access", "review")
            },
        }
        facts = {"scope": True}
        before = matrix_row(
            record,
            facts,
            {
                "access": {"assessment": "satisfied_by_spec"},
                "review": {"assessment": "not_addressed"},
            },
        )
        after = matrix_row(
            record,
            facts,
            {"access": {"assessment": "not_addressed"}, "review": {"assessment": "not_addressed"}},
        )
        self.assertEqual(before["assessment"], after["assessment"])
        self.assertEqual(record_effect(record, before, after, facts, facts)[0], "regressed")
        change, branches = record_effect(record, before, after, facts, facts, additions_only=True)
        self.assertEqual(change, "introduced")
        self.assertEqual(branches[0]["change_effect"], "introduced")
        self.assertEqual(branches[0]["basis"], "resolved_additions_preserve_existing_baseline")
        self.assertEqual(before["clause_assessments"]["access"]["assessment"], "satisfied_by_spec")

    def test_removal_proof_preserves_regression_without_promoting_broad_baseline(self):
        record = {
            "applicability": {"expression": {"predicate": "scope"}},
            "clause_logic": {"clause": "wide"},
            "clause_normativity": {"wide": {"force": "required", "nature": "technical_control"}},
        }
        facts = {"scope": True}
        proof = {
            "baseline_evidence": [{"block_id": "before", "quote": "Secrets excluded from logs."}],
            "delta_evidence": [{"block_id": "delta", "quote": "Log the access token."}],
            "explanation": "A described safeguard is removed while other baseline logging duties were unspecified.",
        }
        before = matrix_row(record, facts, {"wide": {"assessment": "not_addressed"}})
        after = matrix_row(
            record,
            facts,
            {"wide": {"assessment": "contradicted_by_spec", "control_removal": proof}},
        )
        change, branches = record_effect(record, before, after, facts, facts)
        self.assertEqual(change, "regressed")
        self.assertEqual(before["assessment"], "not_addressed")
        self.assertEqual(branches[0]["control_removals"], {"wide": proof})
        after["clause_assessments"]["wide"].pop("control_removal")
        self.assertEqual(record_effect(record, before, after, facts, facts)[0], "unresolved")
        before = matrix_row(record, facts, {"wide": {"assessment": "contradicted_by_spec"}})
        self.assertEqual(record_effect(record, before, after, facts, facts)[0], "unchanged")
        after["clause_assessments"]["wide"]["control_removal"] = proof
        conflicted = matrix_row(record, facts, after["clause_assessments"], source_conflict=True)
        self.assertEqual(
            record_effect(record, before, conflicted, facts, facts), ("unresolved", [])
        )

    def test_removal_proof_never_breaks_lawful_any_or_newly_active_guard(self):
        norms = {cid: {"force": "required", "nature": "technical_control"} for cid in ("a", "b")}
        record = {
            "applicability": {"expression": {"predicate": "scope"}},
            "clause_logic": {"any": [{"clause": "a"}, {"clause": "b"}]},
            "clause_normativity": norms,
        }
        facts = {"scope": True}
        proof = {
            "baseline_evidence": [{"block_id": "before", "quote": "Guard A."}],
            "delta_evidence": [{"block_id": "delta", "quote": "Replace A with B."}],
            "explanation": "A is removed, but the legal alternative B remains sufficient.",
        }
        before = matrix_row(
            record,
            facts,
            {"a": {"assessment": "satisfied_by_spec"}, "b": {"assessment": "not_addressed"}},
        )
        after = matrix_row(
            record,
            facts,
            {
                "a": {"assessment": "contradicted_by_spec", "control_removal": proof},
                "b": {"assessment": "satisfied_by_spec"},
            },
        )
        self.assertEqual(record_effect(record, before, after, facts, facts)[0], "unchanged")
        record["clause_logic"] = {
            "when": {"predicate": "new"},
            "then": {"clause": "a"},
            "else": "not_applicable_branch",
        }
        before_facts, after_facts = facts | {"new": False}, facts | {"new": True}
        before = matrix_row(record, before_facts, {})
        after = matrix_row(record, after_facts, after["clause_assessments"])
        self.assertEqual(
            record_effect(record, before, after, before_facts, after_facts)[0], "introduced"
        )

    def test_unknown_scope_with_no_active_clauses_does_not_create_an_assessment(self):
        inactive = Assessment("unknown", "not_assessed", reason="no_active_clauses")
        self.assertEqual(effect(inactive, inactive), "unchanged")
        with self.assertRaises(ValueError):
            Assessment("unknown", "not_assessed")

    def test_newly_active_guard_is_introduced_without_losing_existing_protection(self):
        record = {
            "applicability": {"expression": {"predicate": "scope"}},
            "clause_logic": {
                "all": [
                    {"clause": "existing"},
                    {
                        "when": {"predicate": "new_feature"},
                        "then": {"any": [{"clause": "a"}, {"clause": "b"}]},
                        "else": "not_applicable_branch",
                    },
                ]
            },
            "clause_normativity": {
                cid: {"force": "required", "nature": "technical_control"}
                for cid in ("existing", "a", "b")
            },
        }
        before_facts = {"scope": True, "new_feature": False}
        after_facts = before_facts | {"new_feature": True}
        before = {"existing": {"assessment": "satisfied_by_spec"}}
        after = before | {cid: {"assessment": "not_addressed"} for cid in ("a", "b")}
        change, branches = record_effect(
            record,
            matrix_row(record, before_facts, before),
            matrix_row(record, after_facts, after),
            before_facts,
            after_facts,
        )
        self.assertEqual(change, "introduced")
        self.assertEqual([b["change_effect"] for b in branches], ["introduced"])
        after["existing"] = {"assessment": "contradicted_by_spec"}
        change, branches = record_effect(
            record,
            matrix_row(record, before_facts, before),
            matrix_row(record, after_facts, after),
            before_facts,
            after_facts,
        )
        self.assertEqual(change, "regressed")
        self.assertEqual({b["change_effect"] for b in branches}, {"introduced", "regressed"})

    def test_current_implementation_claims_are_separate_from_future_requirements(self):
        for text in [
            "Контроли уже реализованы.",
            "Защита внедрена.",
            "All controls are implemented.",
            "Protection has been verified.",
            "Security is guaranteed.",
        ]:
            self.assertTrue(implementation_claim(text), text)
        for text in [
            "Контроль должен быть реализован.",
            "Защита будет внедрена.",
            "Controls must be implemented.",
            "Protection shall be verified before release.",
        ]:
            self.assertFalse(implementation_claim(text), text)

    def test_independent_axes_and_change_effects(self):
        ok = Assessment("applicable", "satisfied_by_spec")
        bad = Assessment("applicable", "contradicted_by_spec")
        gap = Assessment("applicable", "not_addressed")
        absent = Assessment("not_applicable", "not_assessed")
        unknown = Assessment("unknown", "unknown")
        self.assertEqual(effect(ok, bad), "regressed")
        self.assertEqual(effect(bad, bad), "unchanged")
        self.assertEqual(effect(bad, ok), "improved")
        self.assertEqual(effect(absent, gap), "introduced")
        self.assertEqual(effect(unknown, gap), "unresolved")
        with self.assertRaises(ValueError):
            Assessment("unknown", "satisfied_by_spec")
        with self.assertRaises(ValueError):
            Assessment("applicable", "satisfied_by_spec", "verified")

    def test_existing_debt_does_not_hide_conjunct_regression_and_any_remains_atomic(self):
        record = {
            "applicability": {"expression": {"predicate": "scope"}},
            "clause_logic": {"all": [{"clause": "debt"}, {"clause": "access"}]},
            "clause_normativity": {
                cid: {"force": "required", "nature": "technical_control"}
                for cid in ["debt", "access"]
            },
        }
        facts = {"scope": True}
        before = {
            "debt": {"assessment": "not_addressed"},
            "access": {"assessment": "satisfied_by_spec"},
        }
        after = before | {"access": {"assessment": "contradicted_by_spec"}}
        change, details = record_effect(
            record,
            matrix_row(record, facts, before),
            matrix_row(record, facts, after),
            facts,
            facts,
        )
        self.assertEqual(change, "regressed")
        self.assertEqual(details[0]["expression"], {"clause": "access"})
        record["clause_logic"] = {"any": [{"clause": "debt"}, {"clause": "access"}]}
        after = {
            "debt": {"assessment": "satisfied_by_spec"},
            "access": {"assessment": "contradicted_by_spec"},
        }
        change, details = record_effect(
            record,
            matrix_row(record, facts, before),
            matrix_row(record, facts, after),
            facts,
            facts,
        )
        self.assertEqual(change, "unchanged")
        self.assertEqual(details, [])

    def test_guarded_conjuncts_context_and_unknown_are_not_flattened_into_all(self):
        norms = {
            cid: {"force": "required", "nature": "technical_control"} for cid in ["debt", "access"]
        }
        norms["context"] = {"force": "not_a_requirement", "nature": "context"}
        record = {
            "applicability": {"expression": {"predicate": "scope"}},
            "clause_logic": {
                "when": {"predicate": "feature"},
                "then": {"all": [{"clause": "debt"}, {"clause": "access"}, {"clause": "context"}]},
                "else": "not_applicable_branch",
            },
            "clause_normativity": norms,
        }
        before = {
            "debt": {"assessment": "not_addressed"},
            "access": {"assessment": "satisfied_by_spec"},
        }
        after = before | {"access": {"assessment": "contradicted_by_spec"}}
        for feature, expected in [(True, "regressed"), (False, "unchanged"), (None, "unresolved")]:
            facts = {"scope": True, "feature": feature}
            change, details = record_effect(
                record,
                matrix_row(record, facts, before),
                matrix_row(record, facts, after),
                facts,
                facts,
            )
            self.assertEqual(change, expected)
            if feature is False:
                self.assertEqual(details, [])


if __name__ == "__main__":
    unittest.main()
