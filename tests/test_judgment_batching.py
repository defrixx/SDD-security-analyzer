"""Keep active clause coverage exact while isolating baseline from future delta."""

import unittest
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from spec_enricher.effective_state import blocks
from spec_enricher.pipeline import PipelineRunner
from spec_enricher.stage_contracts import JudgmentsOutput


class JudgmentBatchingTests(unittest.TestCase):
    def test_baseline_skills_cannot_reintroduce_future_annotations_through_any_hook(self):
        runner = PipelineRunner.__new__(PipelineRunner)
        runner.skill_outputs = [
            {"stage": "P3", "output": {"note": "FUTURE_ONLY_SENTINEL"}},
            {
                "stage": "P5",
                "assessment_scope": "proposed_with_removal_context",
                "output": {"note": "FUTURE_ONLY_SENTINEL"},
            },
        ]
        runner.manifest = {
            "skills": [
                {
                    "id": hook,
                    "stage": "P5",
                    "hook": hook,
                    "revision": 1,
                    "input_schema": {},
                    "output_schema": {},
                    "prompt": {},
                    "failure_policy": "stop",
                }
                for hook in ("before", "replace", "after")
            ]
        }
        calls = []

        def complete(stage, data, contract, validate=None, **kwargs):
            calls.append(deepcopy(data))
            if contract is None:
                return {"annotation": "Only the supplied baseline input was inspected."}
            self.assertEqual(kwargs["skill"]["id"], "replace")
            return JudgmentsOutput(judgments=[], questions=[], retrieval_queries=[])

        runner.complete = complete
        runner.call(
            "P5", {"assessment_scope": "baseline_only", "baseline_blocks": []}, JudgmentsOutput
        )
        self.assertEqual(len(calls), 3)
        self.assertNotIn("FUTURE_ONLY_SENTINEL", str(calls))
        self.assertEqual(calls[0]["prior_skill_outputs"], [])
        self.assertEqual(len(calls[1]["declarative_skill_outputs"]), 1)
        self.assertEqual(len(calls[2]["prior_skill_outputs"]), 1)
        self.assertEqual(len(runner.skill_outputs), 4)  # history is retained
        self.assertTrue(
            all(x["assessment_scope"] == "baseline_only" for x in runner.skill_outputs[2:])
        )

    def test_side_batches_preserve_asymmetric_active_guards_exactly_once(self):
        runner = PipelineRunner.__new__(PipelineRunner)
        record = {
            key: {} for key in ("clauses", "clause_logic", "clause_normativity", "source_context")
        }
        record.update(
            id="USER-PAIR",
            version="1.0.0",
            statement="Synthetic batch contract.",
            applicability={"expression": {"predicate": "scope"}, "predicates": []},
            clauses=[],
        )
        runner.records = [record]
        runner.request = SimpleNamespace(limits=SimpleNamespace(batch_size=1))
        runner.state = {
            "P2": {"delta_blocks": [], "operations": []},
            "P4": {
                "rows": [
                    {
                        "requirement_id": record["id"],
                        "side": "baseline",
                        "applicability": True,
                        "active_clauses": ["a", "b", "c"],
                        "facts": {},
                    },
                    {
                        "requirement_id": record["id"],
                        "side": "proposed",
                        "applicability": True,
                        "active_clauses": ["b", "c", "d"],
                        "facts": {},
                    },
                ]
            },
        }
        runner.block_maps = lambda: ({}, {})
        runner.contexts = lambda *args: ({}, {})
        batches = []

        def capture(stage, data, contract, validate):
            batches.append({tuple(key) for key in data["requested_judgments"]})
            return JudgmentsOutput(judgments=[], retrieval_queries=[], questions=[])

        runner.call = capture
        with patch("spec_enricher.pipeline.verification_input", return_value={}):
            runner.P5()
        expected = {
            (record["id"], side, cid)
            for side, clauses in [("baseline", "abc"), ("proposed", "bcd")]
            for cid in clauses
        }
        self.assertEqual(set().union(*batches), expected)
        self.assertEqual(sum(map(len, batches)), len(expected))
        self.assertTrue(all(len(batch) == 1 for batch in batches))
        self.assertEqual(len(batches), 6)

    def test_baseline_assessment_and_retrieval_exclude_future_data(self):
        runner = PipelineRunner.__new__(PipelineRunner)
        clause = {"id": "one", "obligation": "Describe the complete applicable policy."}
        normativity = {"force": "required", "nature": "technical"}
        record = {
            "id": "USER-ISOLATION",
            "version": "1.0.0",
            "statement": clause["obligation"],
            "clauses": [clause],
            "applicability": {"expression": {"predicate": "scope"}, "predicates": []},
            "clause_normativity": {"one": normativity},
            "clause_logic": {"clause": "one"},
            "source_context": {},
        }
        runner.records = [record]
        runner.request = SimpleNamespace(limits=SimpleNamespace(batch_size=1))
        before = blocks("baseline", b"The old policy is documented.")[0]
        after = blocks("delta", b"FUTURE_ONLY_SENTINEL: the policy is removed.")[0]
        runner.state = {
            "P2": {
                "delta_blocks": [asdict(after)],
                "operations": [{"kind": "replace", "delta_block_id": after.id}],
            },
            "P4": {
                "rows": [
                    {
                        "requirement_id": record["id"],
                        "side": side,
                        "applicability": True,
                        "active_clauses": ["one"],
                        "facts": {"scope": side == "proposed"},
                    }
                    for side in ("baseline", "proposed")
                ]
            },
        }
        runner.manifest = {}
        runner.block_maps = lambda: ({before.id: before}, {after.id: after})
        retrievals = []

        def retrieve(records, baseline, proposed, query=""):
            retrievals.append((set(proposed), query))
            return baseline, proposed

        runner.contexts = retrieve
        calls = []
        artifacts = []
        runner.artifact = lambda *args: artifacts.append(args)

        def respond(stage, data, contract, validate, **kwargs):
            calls.append(deepcopy(data))
            value = contract.model_validate(
                {
                    "judgments": [
                        {
                            "requirement_id": rid,
                            "version": record["version"],
                            "clause_id": cid,
                            "side": side,
                            "assessment": "satisfied_by_spec",
                            "evidence_state": "direct",
                            "evidence": [{"block_id": b.id, "quote": b.text}],
                            "explanation": "Synthetic positive plumbing claim.",
                            "question": None,
                        }
                        for rid, side, cid in data["requested_judgments"]
                        for b in [before if side == "baseline" else after]
                    ],
                    "retrieval_queries": ["more baseline policy"] if len(calls) == 1 else [],
                    "questions": [],
                }
            )
            validate(value)
            return value

        runner.call = respond
        runner.complete = respond
        with patch(
            "spec_enricher.pipeline.verification_input", side_effect=lambda r, f, a: {"facts": f}
        ):
            result = runner.P5()
        self.assertEqual(len(result["judgments"]), 2)
        self.assertEqual(len(calls), 3)  # baseline expansion + proposed assessment
        self.assertEqual(len(artifacts), 0)
        self.assertTrue(retrievals[1][1])
        self.assertFalse(retrievals[0][0])
        self.assertFalse(retrievals[1][0])
        for data in calls:
            side = data["requested_judgments"][0][1]
            self.assertEqual(set(data["applicability"]), {side})
            self.assertEqual(set(data["verification"]), {side})
            self.assertEqual(data["requested_obligations"][0]["clause"], clause)
            self.assertEqual(data["requested_obligations"][0]["normativity"], normativity)
            if side == "baseline":
                self.assertEqual(data["assessment_scope"], "baseline_only")
                self.assertEqual(data["proposed_blocks"], [])
                self.assertEqual(data["delta_block_ids"], [])
                self.assertEqual(data["removal_delta_block_ids"], [])
                self.assertNotIn("FUTURE_ONLY_SENTINEL", str(data))
            else:
                self.assertEqual(data["assessment_scope"], "proposed_with_removal_context")
                self.assertEqual(data["baseline_blocks"][0]["id"], before.id)
                self.assertEqual(data["proposed_blocks"][0]["id"], after.id)
                self.assertEqual(data["removal_delta_block_ids"], [after.id])
