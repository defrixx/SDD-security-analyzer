"""Unresolved operation uncertainty does not hide failure/branch/adoption axes."""

import unittest

import test_pipeline as pipeline_fixture

from spec_enricher.effective_state import blocks
from spec_enricher.storage import Store


def check_case(name):
    test = pipeline_fixture.PipelineTests()
    test.setUp()
    try:
        store = Store(test.root / "app.sqlite3")
        catalogue = store.catalog(test.pid, test.request["catalog_ids"][0])
        record = catalogue["requirements"][0]
        if name == "no_active_clauses_ambiguous":
            predicate = record["applicability"]["predicates"][0]["id"]
            clause = record["clauses"][0]["id"]
            catalogue["catalog_id"] = "synthetic.no-active-ambiguity"
            catalogue["catalog_version"] = "1.1.0"
            record["version"] = "1.1.0"
            record["applicability"]["expression"] = {"not": {"predicate": predicate}}
            record["clause_logic"] = {
                "when": {"predicate": predicate},
                "then": {"clause": clause},
                "else": "not_applicable_branch",
            }
            test.request["catalog_ids"] = [store.bind_catalog(test.pid, catalogue)]
            test.request["fact_overrides"] = [
                {
                    "requirement_id": record["id"],
                    "side": side,
                    "predicate_id": predicate,
                    "value": False,
                    "reason": "Explicit synthetic author clarification: predicate is false.",
                }
                for side in ("baseline", "proposed")
            ]
        store.close()
        pipeline_fixture.FixtureAdapter.mode = (
            "regression" if name == "resolved_control" else "ambiguous"
        )
        if name.startswith("advisory_") or name.startswith("waived_"):
            mode = name.split("_")[0]
            test.request["adoption_overrides"] = [
                {
                    "requirement_id": record["id"],
                    "mode": mode,
                    "reason": "Explicit synthetic project decision.",
                }
            ]
        if name == "failed_stage_ambiguous":
            pipeline_fixture.FixtureAdapter.mode = "invented"
            delta = blocks(test.delta_id, test.delta)[0]
            test.request["operation_overrides"] = [
                {
                    "id": "author-question",
                    "kind": "unresolved",
                    "delta_block_id": delta.id,
                    "target_block_id": None,
                    "target_hash": None,
                    "target_quote": None,
                    "question": "Does this add or replace the endpoint?",
                    "explanation": "The author explicitly defers choosing the operation.",
                }
            ]
        rid, _ = test.start("finite-state-" + name)
        run = test.wait(rid)
        rows = test.client.get(f"/api/v1/projects/{test.pid}/runs/{rid}/assessments").json()
        assert isinstance(rows, list) and rows
        row = rows[0]["proposed"]
        observed = {
            k: row.get(k)
            for k in (
                "applicability",
                "assessment",
                "enforcement",
                "reason",
                "enforcement_reason",
                "implementation_verification",
            )
        }
        if name == "resolved_control":
            expected = {"assessment": "contradicted_by_spec", "enforcement": "contradicted_by_spec"}
        elif name == "failed_stage_ambiguous":
            expected = {
                "assessment": "not_assessed",
                "enforcement": "not_assessed",
                "reason": "pipeline_incomplete",
            }
        elif name == "no_active_clauses_ambiguous":
            expected = {
                "assessment": "not_assessed",
                "enforcement": "not_assessed",
                "reason": "no_active_clauses",
            }
        else:
            expected = {
                "assessment": "unknown",
                "enforcement": name.split("_")[0]
                if name.startswith(("advisory_", "waived_"))
                else "unknown",
                "reason": "unresolved_delta_operation",
            }
        return {
            "case": name,
            "run_status": run["status"],
            "observed": observed,
            "expected": expected,
            "passed": all(observed.get(k) == v for k, v in expected.items()),
            "fake_adapter_calls": pipeline_fixture.FixtureAdapter.calls.copy(),
        }
    finally:
        test.tearDown()


class UnresolvedDeltaStateTests(unittest.TestCase):
    def test_uncertainty_preserves_enforcement_adoption_failures_and_no_active_clauses(self):
        for name in (
            "required_ambiguous",
            "advisory_ambiguous",
            "waived_ambiguous",
            "failed_stage_ambiguous",
            "no_active_clauses_ambiguous",
            "resolved_control",
        ):
            with self.subTest(case=name):
                result = check_case(name)
                for field, expected in result["expected"].items():
                    self.assertEqual(result["observed"][field], expected, result)
                self.assertEqual(result["observed"]["implementation_verification"], "not_verified")
