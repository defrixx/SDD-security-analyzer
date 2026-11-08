"""Scope facts stay separate from conflicting documentary control descriptions."""

import copy
import json
import unittest
from pathlib import Path

from spec_enricher.assessment import record_effect
from spec_enricher.evaluation import matrix_row


class ConflictAxesTests(unittest.TestCase):
    def test_independent_contradiction_survives_a_conflicting_all_sibling(self):
        facts = {"p1": True, "p2": False}
        before = matrix_row(
            self.record,
            facts,
            {
                "c1": {"assessment": "satisfied_by_spec", "evidence_state": "direct"},
                "c2": {"assessment": "not_addressed", "evidence_state": "insufficient"},
                "c5": {"assessment": "not_addressed", "evidence_state": "insufficient"},
            },
        )
        after = matrix_row(
            self.record,
            facts,
            {
                "c1": {"assessment": "contradicted_by_spec", "evidence_state": "direct"},
                "c2": {"assessment": "unknown", "evidence_state": "conflicting"},
                "c5": {"assessment": "not_addressed", "evidence_state": "insufficient"},
            },
        )
        self.assertEqual(after["assessment"], "contradicted_by_spec")
        self.assertEqual(after["enforcement"], "contradicted_by_spec")
        self.assertTrue(after["incomplete"])
        self.assertEqual(after["conflicting_clause_ids"], ["c2"])
        self.assertEqual(record_effect(self.record, before, after, facts, facts)[0], "regressed")

    def test_missing_design_regression_is_not_hidden_by_separate_conflict(self):
        facts = {"p1": True, "p2": False}
        judgments = {
            "c1": {"assessment": "satisfied_by_spec", "evidence_state": "direct"},
            "c2": {"assessment": "unknown", "evidence_state": "conflicting"},
            "c5": {"assessment": "not_addressed", "evidence_state": "insufficient"},
        }
        before = matrix_row(self.record, facts, judgments)
        after = matrix_row(self.record, facts, judgments | {"c1": {"assessment": "not_addressed"}})
        self.assertEqual(after["assessment"], "unknown")
        self.assertEqual(after["reason"], "source_conflict")
        self.assertEqual(after["source_conflict_scope"], "clauses")
        self.assertEqual(record_effect(self.record, before, after, facts, facts)[0], "regressed")

    def test_conflicting_alternative_does_not_defeat_a_supported_any(self):
        record = copy.deepcopy(self.record)
        record["clause_logic"] = {"any": [{"clause": "c1"}, {"clause": "c2"}]}
        row = matrix_row(
            record,
            {"p1": True, "p2": False},
            {
                "c1": {"assessment": "satisfied_by_spec", "evidence_state": "direct"},
                "c2": {"assessment": "unknown", "evidence_state": "conflicting"},
            },
        )
        self.assertEqual(row["assessment"], "satisfied_by_spec")
        self.assertEqual(row["enforcement"], "satisfied_by_spec")
        self.assertTrue(row["incomplete"])

    def test_advisory_conflict_does_not_downgrade_required_enforcement(self):
        record = copy.deepcopy(self.record)
        record["clause_logic"] = {"all": [{"clause": "c1"}, {"clause": "c2"}]}
        record["clause_normativity"]["c2"]["force"] = "recommended"
        row = matrix_row(
            record,
            {"p1": True, "p2": False},
            {
                "c1": {"assessment": "satisfied_by_spec", "evidence_state": "direct"},
                "c2": {"assessment": "unknown", "evidence_state": "conflicting"},
            },
        )
        self.assertEqual(row["assessment"], "unknown")
        self.assertEqual(row["enforcement"], "satisfied_by_spec")

    @classmethod
    def setUpClass(cls):
        data = json.loads(Path("catalog/requirements.full.json").read_text())
        cls.record = next(r for r in data["requirements"] if r["id"] == "PSH3-APP-6C4147420216")

    def test_control_conflict_keeps_known_architectural_scope(self):
        judgments = {
            cid: {"assessment": "satisfied_by_spec", "evidence_state": "direct"}
            for cid in ("c1", "c2", "c5")
        }
        row = matrix_row(self.record, {"p1": True, "p2": False}, judgments, source_conflict=True)
        self.assertEqual(row["applicability"], "applicable")
        self.assertEqual(row["assessment"], "unknown")
        self.assertEqual(row["enforcement"], "unknown")
        self.assertEqual(row["reason"], "source_conflict")
        self.assertEqual(row["implementation_verification"], "not_verified")

    def test_unknown_scope_is_unknown_and_inactive_scope_does_not_gain_a_blocker(self):
        for value, applicability, assessment in [
            (None, "unknown", "unknown"),
            (False, "not_applicable", "not_assessed"),
        ]:
            with self.subTest(value=value):
                row = matrix_row(self.record, {"p1": value, "p2": False}, {}, source_conflict=True)
                self.assertEqual(row["applicability"], applicability)
                self.assertEqual(row["assessment"], assessment)

    def test_all_context_stays_unassessed_before_unknown_guards_or_conflicts(self):
        record = copy.deepcopy(self.record)
        for value in record["clause_normativity"].values():
            value["force"] = "not_a_requirement"
        for scope in (True, False, None):
            with self.subTest(scope=scope):
                row = matrix_row(record, {"p1": scope, "p2": None}, {}, source_conflict=True)
                self.assertEqual(row["assessment"], "not_assessed")
                self.assertEqual(row["reason"], "no_active_clauses")
                self.assertEqual(row["enforcement"], "not_assessed")

    def test_conflict_does_not_rewrite_explicit_waiver(self):
        row = matrix_row(
            self.record,
            {"p1": True, "p2": False},
            {},
            source_conflict=True,
            adoption={"mode": "waived", "reason": "Explicit operator decision."},
        )
        self.assertEqual(row["assessment"], "unknown")
        self.assertEqual(row["applicability"], "applicable")
        self.assertEqual(row["enforcement"], "waived")
