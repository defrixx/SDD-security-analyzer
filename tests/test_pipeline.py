"""Synthetic model plumbing only. Expected labels never enter production adapters."""

import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from spec_enricher.api import create_app
from spec_enricher.connections import ConnectionProfile
from spec_enricher.llm import Completion, LLMAdapter
from spec_enricher.network import NetworkError
from spec_enricher.pipeline import ContextExceeded
from spec_enricher.prompts import ARCHITECTURAL_REVIEW_PHASE
from spec_enricher.registry import Registry
from spec_enricher.storage import Store
from spec_enricher.workers import WorkerError

ORIGIN = "http://127.0.0.1:8080"


class FixtureAdapter(LLMAdapter):
    calls = []
    mode = "regression"
    pause = None
    request_started = threading.Event()

    def complete(self, messages, schema, *, before_request=None, after_response=None):
        payload = self.payload(messages, schema)
        if self.mode == "missing-credential":
            self.secrets.remove(self.project_id, self.profile.secret_ref)
            self.headers()  # Same pre-transmission resolution as the real adapter.
        if before_request:
            before_request(payload)
        if self.mode in {"network-timeout", "revocation-retry"}:
            self.calls.append("transport-timeout")
            if self.mode == "revocation-retry":
                type(self).request_started.set()
            raise NetworkError(
                "request_timeout", retry_after=0.5 if self.mode == "revocation-retry" else 0
            )
        data = json.loads(messages[1]["content"])["data"]
        properties = schema["properties"]
        if self.mode == "p8-context-exceeded" and "reassessment" in properties:
            raise ContextExceeded("synthetic_context_budget_exceeded")
        if "annotations" in properties:
            self.calls.append("skill")
            value = {"annotations": ["Synthetic annotation; cannot grant permissions."]}
            if after_response:
                after_response(
                    json.dumps({"choices": [{"message": {"content": json.dumps(value)}}]}).encode(),
                    0,
                )
            return Completion(value, {}, {"total_tokens": 10}, payload, 0)
        if "operations" in properties:
            block, target = data["delta_blocks"][0], data["baseline_blocks"][0]
            add_only = self.mode in {
                "multi-add",
                "source-conflict",
                "p5-control-conflict",
                "p5-inconsistent-conflict",
            }
            value = {
                "operations": [
                    {
                        "id": "change",
                        "kind": "unresolved"
                        if self.mode == "ambiguous"
                        else "add"
                        if add_only
                        else "replace",
                        "delta_block_id": block["id"],
                        "target_block_id": None if add_only else target["id"],
                        "target_hash": None if add_only else target["hash"],
                        "target_quote": None
                        if add_only or self.mode == "ambiguous"
                        else target["text"],
                        "explanation": "Explicit change in synthetic fixture.",
                        "question": "Which control changes?" if self.mode == "ambiguous" else None,
                    }
                ],
                "questions": [],
            }
            if self.mode == "empty-operations":
                value["operations"] = []
            step = "P2"
        elif "facts" in properties:
            facts = []
            for record in data["requirements"]:
                for side in ("baseline", "proposed"):
                    block = data[side + "_blocks"][0]
                    for predicate in record["applicability"]["predicates"]:
                        review = data.get("assessment_phase") == ARCHITECTURAL_REVIEW_PHASE
                        scope_conflict = (
                            self.mode == "genuine-scope-conflict"
                            or self.mode == "scope-control-confusion"
                            and not review
                        )
                        unknown = self.mode == "unknown" or scope_conflict
                        facts.append(
                            {
                                "requirement_id": record["id"],
                                "version": record["version"],
                                "predicate_id": predicate["id"],
                                "side": side,
                                "value": None
                                if unknown
                                else False
                                if self.mode == "scope-absence"
                                else True,
                                "evidence_state": "conflicting"
                                if scope_conflict
                                else "insufficient"
                                if unknown
                                else "direct",
                                "evidence": []
                                if unknown and not scope_conflict
                                else [
                                    {"block_id": b["id"], "quote": b["text"]}
                                    for b in data[side + "_blocks"][:2]
                                ]
                                if scope_conflict
                                else [{"block_id": block["id"], "quote": block["text"]}],
                                "explanation": "Private read scope is explicit in the synthetic snapshot.",
                                "question": "Are these documents private?" if unknown else None,
                            }
                        )
            value = {"facts": facts, "conflicts": [], "questions": []}
            if self.mode in {
                "source-conflict",
                "scope-control-confusion",
                "genuine-scope-conflict",
            } and not data.get("assessment_phase"):
                value["conflicts"] = [
                    {
                        "requirement_ids": [r["id"] for r in data["requirements"]],
                        "evidence": [
                            {"block_id": b["id"], "quote": b["text"]}
                            for b in data["baseline_blocks"][:2]
                        ],
                        "explanation": "Equally authoritative sources disagree about the control, not private-document scope.",
                    }
                ]
            step = "P3-review" if data.get("assessment_phase") else "P3"
        elif "judgments" in properties:
            judgments = []
            candidate_review = data.get("assessment_phase") == "candidate_reassessment@1.0.0"
            for rid, side, cid in data["requested_judgments"]:
                record = next(item for item in data["requirements"] if item["id"] == rid)
                block = data[side + "_blocks"][0]
                label = (
                    "satisfied_by_spec"
                    if side == "baseline"
                    else "not_addressed"
                    if self.mode == "gap"
                    else "contradicted_by_spec"
                )
                if side == "baseline" and self.mode in {
                    "wide-clause-removal",
                    "invented-removal-source",
                }:
                    label = "not_addressed"
                if self.mode == "overconfident-positive":
                    label = "satisfied_by_spec"
                judgments.append(
                    {
                        "requirement_id": "invented" if self.mode == "invented" else rid,
                        "version": record["version"],
                        "clause_id": cid,
                        "side": side,
                        "assessment": label,
                        "applicable_design": block["text"][:600],
                        "control_removal": None,
                        "evidence_state": "direct",
                        "evidence": [{"block_id": block["id"], "quote": block["text"]}],
                        "explanation": "Explicit synthetic design statement.",
                        "question": None,
                    }
                )
                if self.mode in {"wide-clause-removal", "invented-removal-source"}:
                    if side == "baseline":
                        judgments[-1].update(evidence=[], evidence_state="insufficient")
                    else:
                        original = data["baseline_blocks"][0]
                        judgments[-1]["control_removal"] = {
                            "baseline_evidence": [
                                {"block_id": original["id"], "quote": original["text"]}
                            ],
                            "delta_evidence": [
                                {
                                    "block_id": original["id"]
                                    if self.mode == "invented-removal-source"
                                    else block["id"],
                                    "quote": original["text"]
                                    if self.mode == "invented-removal-source"
                                    else block["text"],
                                }
                            ],
                            "explanation": "Explicitly described domain authorization is removed, while retention was already unspecified.",
                        }
                if (
                    side == "proposed"
                    and self.mode
                    in {
                        "p5-control-conflict",
                        "p5-inconsistent-conflict",
                    }
                    or side == "proposed"
                    and self.mode == "p5-mixed-conflict"
                    and cid == "c2"
                ):
                    judgments[-1].update(
                        assessment="unknown"
                        if self.mode in {"p5-control-conflict", "p5-mixed-conflict"}
                        else "satisfied_by_spec",
                        evidence_state="conflicting",
                        evidence=[
                            {"block_id": b["id"], "quote": b["text"]}
                            for b in data["proposed_blocks"][:2]
                        ],
                        question="Which conflicting control description is authoritative?"
                        if self.mode in {"p5-control-conflict", "p5-mixed-conflict"}
                        else None,
                    )
            if candidate_review:
                for item in judgments:
                    block = data[item["side"] + "_blocks"][0]
                    item.update(
                        assessment="not_addressed"
                        if item["side"] == "proposed"
                        and self.mode
                        in {
                            "wide-clause-removal",
                            "overconfident-positive",
                            "overconfident-proposal",
                        }
                        else "satisfied_by_spec",
                        control_removal=None,
                        evidence_state="direct",
                        evidence=[{"block_id": block["id"], "quote": block["text"]}],
                        question=None,
                    )
                    if (
                        self.mode == "candidate-alternative"
                        and item["side"] == "proposed"
                        and item["clause_id"] == "c2"
                    ):
                        item["assessment"] = "not_addressed"
            value = {"judgments": judgments, "retrieval_queries": [], "questions": []}
            if candidate_review and self.mode == "candidate-extra-retrieval":
                value["retrieval_queries"] = ["unrequested extra product documentation"]
            if candidate_review and self.mode == "candidate-cross-side-citation":
                original = data["baseline_blocks"][0]
                for item in judgments:
                    if item["side"] == "proposed":
                        item["evidence"] = [{"block_id": original["id"], "quote": original["text"]}]
            step = (
                "P8-review"
                if candidate_review
                else "P5-review"
                if data.get("assessment_phase")
                else "P5"
            )
        elif "patches" in properties:
            block = data["delta_blocks"][0]
            value = {
                "patches": [
                    {
                        "id": "add-access-control",
                        "finding_ids": [item["id"] for item in data["findings"]],
                        "operation": "replace",
                        "target_block_id": block["id"],
                        "target_hash": block["hash"],
                        "original": block["text"],
                        "replacement": "The product keeps private documents. Every read requires server authorization.\n\n## Security acceptance\nVerify denied cross-owner reads.\n",
                        "explanation": "Proposed design requirement; no runtime claim.",
                        "acceptance_criteria": ["Cross-owner reads are denied."],
                        "changes_business_scope": False,
                    }
                ],
                "questions": [],
            }
            step = "P7"
        else:
            rows = []
            for rid, side, cid in data["requested_reassessment"]:
                record = next(item for item in data["catalog"] if item["id"] == rid)
                block = data["candidate_blocks"][0]
                rows.append(
                    {
                        "requirement_id": rid,
                        "version": record["version"],
                        "clause_id": cid,
                        "side": side,
                        "assessment": "not_addressed"
                        if self.mode == "wide-clause-removal"
                        or self.mode in {"overconfident-positive", "overconfident-proposal"}
                        and data.get("assessment_phase")
                        else "satisfied_by_spec",
                        "applicable_design": block["text"][:600],
                        "control_removal": None,
                        "evidence_state": "direct",
                        "evidence": [{"block_id": block["id"], "quote": block["text"]}],
                        "explanation": "Candidate states the proposed control.",
                        "question": None,
                    }
                )
            value = {
                "issues": [
                    {
                        "type": "remaining_gap",
                        "patch_id": None,
                        "requirement_id": rows[0]["requirement_id"],
                        "explanation": "The candidate restores authorization but still omits retention.",
                    }
                ]
                if self.mode == "wide-clause-removal" and rows
                else [],
                "reassessment": rows,
                "questions": [],
            }
            step = "P8-review" if data.get("assessment_phase") else "P8"
        # Provider decisions follow evidence; stored/API names remain stable.
        # Keep intentionally invalid fixtures otherwise unchanged.
        for operation in value.get("operations", []):
            operation["operation"] = operation.pop("kind")
        for fact in value.get("facts", []):
            fact["truth"] = fact.pop("value")
            fact["support"] = fact.pop("evidence_state")
        for judgment in value.get("judgments", value.get("reassessment", [])):
            judgment["verdict"] = judgment.pop("assessment")
            judgment["support"] = judgment.pop("evidence_state")
        type(self).calls.append(step)
        type(self).request_started.set()
        if type(self).pause is not None:
            type(self).pause.wait(5)
        raw = {
            "choices": [{"message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200},
        }
        if after_response:
            after_response(json.dumps(raw).encode(), 1)
        if self.mode == "local-schema-unavailable" and step == "P5":
            from spec_enricher.schema_worker import validate_payload

            with patch(
                "spec_enricher.schema_worker.bounded",
                side_effect=WorkerError("worker time limit exceeded"),
            ):
                validate_payload(schema, value)
        return Completion(value, raw, raw["usage"], payload, 1)


class PipelineTests(unittest.TestCase):
    def test_architectural_review_separates_controls_and_preserves_genuine_unknown_or_absence(self):
        for mode, expected in (
            ("scope-control-confusion", "applicable"),
            ("genuine-scope-conflict", "unknown"),
            ("scope-absence", "not_applicable"),
        ):
            with self.subTest(mode=mode):
                FixtureAdapter.mode = mode
                FixtureAdapter.calls = []
                content = (
                    b"No private documents or protected operations exist.\n"
                    if mode == "scope-absence"
                    else b"Private documents exist; their owner protection is disputed.\n"
                )
                base = self.client.post(
                    f"/api/v1/projects/{self.pid}/documents?filename={mode}.md",
                    content=content,
                    headers=self.headers,
                ).json()["id"]
                other = self.client.post(
                    f"/api/v1/projects/{self.pid}/documents?filename=other-{mode}.md",
                    content=b"No private documents exist.\n"
                    if mode == "genuine-scope-conflict"
                    else content,
                    headers=self.headers,
                ).json()["id"]
                delta = self.client.post(
                    f"/api/v1/projects/{self.pid}/documents?filename=delta-{mode}.md&role=delta",
                    content=content,
                    headers=self.headers,
                ).json()["id"]
                self.request |= {
                    "baseline_document_ids": [base, other],
                    "delta_document_id": delta,
                    "step_mode": True,
                }
                rid, _ = self.start("architectural-" + mode)
                result = self.wait(rid)
                for _ in range(40):
                    if result["status"] != "waiting_approval":
                        break
                    result = self.advance_step(rid, result)
                self.assertNotEqual(result["status"], "failed", result)
                self.assertEqual(FixtureAdapter.calls.count("P3"), 1)
                self.assertEqual(FixtureAdapter.calls.count("P3-review"), 1)
                prefix = f"/api/v1/projects/{self.pid}/runs/{rid}"
                row = self.client.get(prefix + "/assessments").json()[0]
                self.assertEqual(row["baseline"]["applicability"], expected)
                self.assertEqual(row["proposed"]["implementation_verification"], "not_verified")
                artifacts = self.client.get(prefix + "/artifacts").json()
                review = next(
                    a for a in artifacts if a["kind"] == "assessment.architectural_review"
                )
                body = self.client.get(prefix + f"/artifacts/{review['id']}").json()
                self.assertFalse(body["semantic_proof"])
                if mode == "scope-control-confusion":
                    self.assertTrue(body["control_conflicts_retained"])
                    self.assertEqual(row["baseline"]["assessment"], "unknown")
                    self.assertTrue(all(f["value"] is True for f in body["reviewed"]))
                elif mode == "genuine-scope-conflict":
                    self.assertTrue(all(f["value"] is None for f in body["reviewed"]))
                else:
                    self.assertTrue(all(f["value"] is False for f in body["reviewed"]))
                    self.assertNotIn("P5", FixtureAdapter.calls)

    def test_p5_clause_conflict_does_not_mask_independent_regression_in_api(self):
        store = Store(self.root / "app.sqlite3")
        policy = store.catalog(self.pid, self.request["catalog_ids"][0])
        policy["catalog_id"] = "synthetic.independent-conflict"
        record = policy["requirements"][0]
        record["id"] = "USER-INDEPENDENT-CONFLICT"
        record["clauses"].append(
            {
                "id": "c2",
                "obligation": "Resolve an independent secondary safeguard.",
                "source_ref_ids": ["s1"],
            }
        )
        record["clause_normativity"]["c2"] = dict(record["clause_normativity"]["c1"])
        record["clause_logic"] = {"all": [{"clause": "c1"}, {"clause": "c2"}]}
        for plan in (
            record["verification"]["specification"],
            record["verification"]["source_level"],
        ):
            for step in plan["steps"]:
                step["clause_ids"] = ["c1", "c2"]
        self.request["catalog_ids"] = [store.bind_catalog(self.pid, policy)]
        store.close()
        doc = self.client.post(
            f"/api/v1/projects/{self.pid}/documents?filename=secondary.md",
            content=b"The secondary safeguard has an unresolved description.\n",
            headers=self.headers,
        ).json()["id"]
        self.request["baseline_document_ids"].append(doc)
        FixtureAdapter.mode = "p5-mixed-conflict"
        rid, _ = self.start("independent-clause-conflict")
        result = self.wait(rid)
        self.assertEqual(result["status"], "partial", result)
        prefix = f"/api/v1/projects/{self.pid}/runs/{rid}"
        row = self.client.get(prefix + "/assessments").json()[0]
        self.assertEqual(row["proposed"]["assessment"], "contradicted_by_spec")
        self.assertEqual(row["proposed"]["enforcement"], "contradicted_by_spec")
        self.assertEqual(row["change_effect"], "regressed")
        self.assertEqual(row["proposed"]["conflicting_clause_ids"], ["c2"])
        self.assertTrue(row["proposed"]["incomplete"])
        finding = self.client.get(prefix + "/findings").json()[0]
        self.assertEqual(finding["type"], "explicit_contradiction")
        self.assertEqual(finding["change_effect"], "regressed")

    def test_proposal_positive_review_keeps_omitted_duties_as_remaining_gap(self):
        FixtureAdapter.mode = "overconfident-proposal"
        self.request["step_mode"] = True
        rid, _ = self.start("proposal-positive-review")
        result = self.wait(rid)
        for _ in range(35):
            if result["status"] != "waiting_approval":
                break
            result = self.advance_step(rid, result)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(FixtureAdapter.calls.count("P8"), 1)
        self.assertEqual(FixtureAdapter.calls.count("P8-review"), 1)
        prefix = f"/api/v1/projects/{self.pid}/runs/{rid}"
        artifacts = self.client.get(prefix + "/artifacts").json()
        p8 = next(a for a in artifacts if a["kind"] == "stage.P8.output")
        report = self.client.get(prefix + f"/artifacts/{p8['id']}").json()
        self.assertEqual(report["implementation_verification"], "not_verified")
        self.assertTrue(report["incomplete"])
        self.assertTrue(
            all(j["assessment"] == "not_addressed" for j in report["reassessment_of_proposal_only"])
        )
        self.assertTrue(any(i["type"] == "remaining_gap" for i in report["critique"]))

    def test_candidate_reassessment_separates_documentary_evidence_from_patch_history(self):
        rid, _ = self.start("candidate-documentary-reassessment")
        result = self.wait(rid)
        self.assertEqual(result["status"], "succeeded", result)
        prefix = f"/api/v1/projects/{self.pid}/runs/{rid}"
        manifest = self.client.get(prefix + "/manifest").json()
        recipe = manifest["candidate_reassessment_prompt"]
        self.assertEqual(recipe["template"], manifest["prompts"]["P5"]["template"])
        self.assertEqual(recipe["connection_stage"], "P8")
        self.assertIn(
            "core.P8.candidate_documentary_reassessment",
            next(item["calls"] for item in manifest["hook_order"] if item["stage"] == "P8"),
        )
        artifacts = self.client.get(prefix + "/artifacts").json()
        requests = [
            self.client.get(prefix + f"/artifacts/{a['id']}").json()
            for a in artifacts
            if a["kind"] == "model.request"
        ]
        reviews = [
            json.loads(req["messages"][1]["content"])["data"]
            for req in requests
            if "candidate_reassessment@1.0.0" in req["messages"][1]["content"]
        ]
        self.assertEqual(len(reviews), 1)
        review = reviews[0]
        self.assertNotIn("patches", review)
        self.assertNotIn("findings", review)
        self.assertEqual(review["removal_delta_block_ids"], [])
        self.assertEqual(
            {key[1] for key in review["requested_judgments"]}, {"baseline", "proposed"}
        )
        self.assertIn(self.baseline.decode(), [b["text"] for b in review["baseline_blocks"]])
        self.assertFalse(
            any("Remove server authorization" in b["text"] for b in review["proposed_blocks"])
        )
        self.assertTrue(
            any(
                "Every read requires server authorization" in b["text"]
                for b in review["proposed_blocks"]
            )
        )
        report_artifact = next(
            a for a in artifacts if a["kind"] == "assessment.candidate_reassessment"
        )
        report = self.client.get(prefix + f"/artifacts/{report_artifact['id']}").json()
        self.assertTrue(report["original_P5_matrix_unchanged"])
        self.assertFalse(report["semantic_proof"])
        row = self.client.get(prefix + "/assessments").json()[0]
        self.assertEqual(row["proposed"]["assessment"], "contradicted_by_spec")

    def test_candidate_reassessment_rejects_baseline_citations_for_replaced_candidate(self):
        FixtureAdapter.mode = "candidate-cross-side-citation"
        rid, _ = self.start("candidate-cross-side-citation")
        result = self.wait(rid)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(FixtureAdapter.calls.count("P8-review"), 2)
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        self.assertFalse(any(a["kind"] == "stage.P8.output" for a in artifacts))

    def test_candidate_reassessment_does_not_expand_context_from_model_request(self):
        FixtureAdapter.mode = "candidate-extra-retrieval"
        rid, _ = self.start("candidate-extra-retrieval")
        result = self.wait(rid)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(FixtureAdapter.calls.count("P8-review"), 2)
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        self.assertFalse(any(a["kind"] == "stage.P8.output" for a in artifacts))

    def test_candidate_reassessment_aggregates_original_any_and_all_across_batches(self):
        for operator, status, assessment in [
            ("any", "succeeded", "satisfied_by_spec"),
            ("all", "partial", "not_addressed"),
        ]:
            with self.subTest(operator=operator):
                store = Store(self.root / "app.sqlite3")
                policy = json.loads(Path("templates/requirements.example.json").read_text())
                policy["catalog_id"] = "synthetic.candidate-" + operator
                record = policy["requirements"][0]
                record["id"] = "USER-CANDIDATE-" + operator.upper()
                record["clauses"].append(
                    {
                        "id": "c2",
                        "obligation": "An alternative secondary design control.",
                        "source_ref_ids": ["s1"],
                    }
                )
                record["clause_normativity"]["c2"] = dict(record["clause_normativity"]["c1"])
                record["clause_logic"] = {operator: [{"clause": "c1"}, {"clause": "c2"}]}
                for plan in (
                    record["verification"]["specification"],
                    record["verification"]["source_level"],
                ):
                    for step in plan["steps"]:
                        step["clause_ids"] = ["c1", "c2"]
                self.request["catalog_ids"] = [store.bind_catalog(self.pid, policy)]
                self.request["limits"]["batch_size"] = 1
                store.close()
                FixtureAdapter.mode = "candidate-alternative"
                rid, _ = self.start("candidate-typed-" + operator)
                result = self.wait(rid)
                self.assertEqual(result["status"], status, result)
                report = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/critique").json()
                self.assertEqual(report["candidate_assessments"][0]["assessment"], assessment)
                self.assertEqual(
                    report["candidate_assessments"][0]["implementation_verification"],
                    "not_verified",
                )
                self.assertEqual(
                    {
                        row["clause_id"]: row["assessment"]
                        for row in report["reassessment_of_proposal_only"]
                    },
                    {"c1": "satisfied_by_spec", "c2": "not_addressed"},
                )
                self.assertEqual(bool(report["critique"]), operator == "all")

    def test_p5_second_side_budget_exhaustion_does_not_commit_partial_matrix(self):
        self.request["limits"]["stage_max_requests"] = 1
        rid, _ = self.start("positive-review-budget")
        result = self.wait(rid)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(FixtureAdapter.calls, ["P2", "P3", "P5"])
        rows = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/assessments").json()
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["baseline"]["assessment"], "not_assessed")
            self.assertEqual(row["baseline"]["reason"], "pipeline_incomplete")
            self.assertTrue(row["baseline"]["incomplete"])
        self.assertNotIn("P7", FixtureAdapter.calls)

    def test_single_p5_pass_remains_documentary_unverified_and_requires_each_call_consent(self):
        # A fake adapter exercises consent/status plumbing, not semantic truth.
        self.request["step_mode"] = True
        rid, _ = self.start("single-documentary-pass")
        value = self.wait(rid)
        for _ in range(30):
            if value["status"] != "waiting_approval":
                break
            value = self.advance_step(rid, value)
        self.assertEqual(value["status"], "succeeded", value)
        self.assertEqual(FixtureAdapter.calls.count("P5"), 2)
        self.assertEqual(FixtureAdapter.calls.count("P5-review"), 0)
        prefix = f"/api/v1/projects/{self.pid}/runs/{rid}"
        rows = self.client.get(prefix + "/assessments").json()
        self.assertTrue(
            all(
                row[side]["implementation_verification"] == "not_verified"
                for row in rows
                for side in ("baseline", "proposed")
            )
        )
        artifacts = self.client.get(prefix + "/artifacts").json()
        self.assertFalse(any(a["kind"] == "assessment.positive_review" for a in artifacts))

    def use_prompt_json(self):
        store = Store(self.root / "app.sqlite3")
        registry = Registry(store)
        old = registry.get(self.pid, "connection", self.request["connection"]["id"], 1)
        profile = old["content"] | {"output_mode": "prompt_json"}
        new = registry.save(
            self.pid, "connection", profile, object_id=old["id"], expected_revision=1
        )
        registry.report(
            self.pid,
            new["id"],
            2,
            {"status": "degraded", "inference": "passed", "schema": "passed"},
        )
        registry.grant(self.pid, new["id"], 2, new["sha256"], profile["data_scopes"])
        store.close()
        self.request["connection"] = {"id": new["id"], "revision": 2}
        self.request["step_mode"] = True

    def test_prompt_json_step_mode_sends_exact_approved_payload_once(self):
        self.use_prompt_json()
        rid, _ = self.start("prompt-json-step-payload")
        value = self.wait(rid)
        for _ in range(30):
            if value["status"] != "waiting_approval":
                break
            value = self.advance_step(rid, value)
        self.assertEqual(value["status"], "succeeded", value)
        self.assertEqual(FixtureAdapter.calls, ["P2", "P3", "P5", "P5", "P7", "P8", "P8-review"])
        metadata = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        previews = {}
        actual = []
        for item in metadata:
            if item["kind"] not in {"model.call_preview", "model.request"}:
                continue
            body = self.client.get(
                f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts/{item['id']}"
            ).json()
            if item["kind"] == "model.call_preview":
                previews[item["metadata"]["call_key"]] = body["payload"]
            else:
                actual.append((item["metadata"]["call_key"], body))
        self.assertEqual(len(actual), 7)
        for key, payload in actual:
            self.assertEqual(payload, previews[key])
            self.assertNotIn("response_format", payload)

    def test_prompt_json_repair_retains_new_consent_and_bounded_requests(self):
        self.use_prompt_json()
        FixtureAdapter.mode = "empty-operations"
        rid, _ = self.start("prompt-json-repair-payload")
        value = self.wait(rid)
        previews = []
        for _ in range(15):
            if value["status"] != "waiting_approval":
                break
            if value["checkpoint"].get("pending_call"):
                previews.append(value["checkpoint"]["pending_call"])
            value = self.advance_step(rid, value)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(FixtureAdapter.calls, ["P2", "P2"])
        self.assertEqual([p["request_ordinal"] for p in previews], [1, 2])
        self.assertNotEqual(previews[0]["payload_sha256"], previews[1]["payload_sha256"])

    def test_local_validator_failure_stops_without_model_repair_or_later_calls(self):
        FixtureAdapter.mode = "local-schema-unavailable"
        rid, _ = self.start("local-validator-unavailable-no-repair")
        result = self.wait(rid)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "local_schema_validation_unavailable")
        self.assertEqual(FixtureAdapter.calls.count("P5"), 1)
        self.assertNotIn("P7", FixtureAdapter.calls)
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        self.assertTrue(any(a["kind"] == "model.raw" for a in artifacts))
        self.assertFalse(any(a["kind"] == "model.rejection" for a in artifacts))

    def test_unavailable_credential_stops_without_request_or_model_repair(self):
        store = Store(self.root / "app.sqlite3")
        registry = Registry(store)
        old = registry.get(self.pid, "connection", self.request["connection"]["id"], 1)
        reference = self.app.state.secrets.put(self.pid, "SYNTHETIC_REMOVED_CREDENTIAL_987654")
        profile = old["content"] | {"auth": "bearer", "secret_ref": reference}
        new = registry.save(
            self.pid, "connection", profile, object_id=old["id"], expected_revision=1
        )
        registry.report(self.pid, new["id"], 2, {"status": "passed", "mode": "synthetic_fixture"})
        registry.grant(self.pid, new["id"], 2, new["sha256"], profile["data_scopes"])
        store.close()
        self.request["connection"] = {"id": new["id"], "revision": 2}
        FixtureAdapter.mode = "missing-credential"
        rid, _ = self.start("missing-key-no-repair")
        result = self.wait(rid)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "credential_reference_unavailable")
        self.assertEqual(FixtureAdapter.calls, [])
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        self.assertFalse(
            any(a["kind"] in {"model.request", "model.raw", "model.rejection"} for a in artifacts)
        )

    def test_revocation_during_retry_wait_blocks_every_later_request(self):
        FixtureAdapter.mode = "revocation-retry"
        rid, _ = self.start("revoke-before-automatic-retry")
        self.assertTrue(FixtureAdapter.request_started.wait(2))
        cid = self.request["connection"]["id"]
        revoked = self.client.delete(
            f"/api/v1/projects/{self.pid}/connections/{cid}/grants", headers=self.headers
        )
        self.assertEqual(revoked.status_code, 200)
        result = self.wait(rid)
        self.assertEqual(FixtureAdapter.calls, ["transport-timeout"])
        self.assertEqual(result["error_code"], "egress_permission_revoked")
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        self.assertEqual(sum(a["kind"] == "model.request" for a in artifacts), 1)
        self.assertEqual(sum(a["kind"] == "model.rejection" for a in artifacts), 0)

    def test_revocation_while_invalid_response_is_inflight_blocks_repair(self):
        FixtureAdapter.mode = "empty-operations"
        FixtureAdapter.pause = threading.Event()
        rid, _ = self.start("revoke-before-schema-repair")
        self.assertTrue(FixtureAdapter.request_started.wait(2))
        cid = self.request["connection"]["id"]
        self.assertEqual(
            self.client.delete(
                f"/api/v1/projects/{self.pid}/connections/{cid}/grants", headers=self.headers
            ).status_code,
            200,
        )
        FixtureAdapter.pause.set()
        result = self.wait(rid)
        self.assertEqual(FixtureAdapter.calls, ["P2"])
        self.assertEqual(result["error_code"], "egress_permission_revoked")
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        self.assertEqual(sum(a["kind"] == "model.request" for a in artifacts), 1)
        self.assertEqual(sum(a["kind"] == "model.rejection" for a in artifacts), 1)

    def test_one_removed_safeguard_is_not_hidden_by_other_debt_in_the_same_clause(self):
        store = Store(self.root / "app.sqlite3")
        policy = store.catalog(self.pid, self.request["catalog_ids"][0])
        policy["catalog_id"] = "synthetic.wide-control"
        record = policy["requirements"][0]
        record["id"] = "USER-WIDE-CONTROL"
        record["statement"] = (
            "Require domain authorization for private reads and define audit retention."
        )
        record["clauses"][0]["obligation"] = record["statement"]
        self.request["catalog_ids"] = [store.bind_catalog(self.pid, policy)]
        store.close()
        FixtureAdapter.mode = "wide-clause-removal"
        rid, _ = self.start("partial-coverage-removal")
        self.assertEqual(self.wait(rid)["status"], "partial")
        prefix = f"/api/v1/projects/{self.pid}/runs/{rid}"
        row = self.client.get(prefix + "/assessments").json()[0]
        self.assertEqual(row["baseline"]["assessment"], "not_addressed")
        self.assertEqual(row["proposed"]["assessment"], "contradicted_by_spec")
        self.assertEqual(row["change_effect"], "regressed")
        self.assertEqual(row["baseline"]["implementation_verification"], "not_verified")
        self.assertTrue(row["proposed"]["branch_change_effects"][0]["control_removals"])
        finding = self.client.get(prefix + "/findings").json()[0]
        self.assertEqual(finding["change_effect"], "regressed")
        self.assertEqual(
            {c["snapshot_id"] for c in finding["evidence_refs"]}, {self.baseline_id, self.delta_id}
        )
        FixtureAdapter.mode = "invented-removal-source"
        original = self.client.get(f"/api/v1/projects/{self.pid}/documents/{self.delta_id}").content
        replacement = self.client.post(
            f"/api/v1/projects/{self.pid}/documents?filename=wrong-removal.md&role=delta",
            content=original,
            headers=self.headers,
        ).json()
        self.request["delta_document_id"] = replacement["id"]
        rid, _ = self.start("wrong-control-removal-source")
        self.assertEqual(self.wait(rid)["status"], "failed")
        metadata = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        self.assertEqual(sum(a["kind"] == "model.rejection" for a in metadata), 2)

    def test_transport_retry_requires_new_consent_even_for_identical_payload(self):
        FixtureAdapter.mode = "network-timeout"
        self.request["step_mode"] = True
        rid, _ = self.start("retry-consent")
        value = self.wait(rid)
        previews = []
        for _ in range(15):
            if value["status"] != "waiting_approval":
                break
            pending = value["checkpoint"].get("pending_call")
            if pending:
                previews.append(pending)
            value = self.advance_step(rid, value)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(FixtureAdapter.calls, ["transport-timeout"] * 3)
        self.assertEqual([preview["request_ordinal"] for preview in previews], [1, 2, 3])
        self.assertEqual(len({preview["payload_sha256"] for preview in previews}), 1)
        self.assertEqual(len({preview["call_key"] for preview in previews}), 3)

    def test_resume_after_partial_p6_commit_keeps_one_finding_per_requirement(self):
        rid, _ = self.start("p6-commit-recovery")
        self.assertEqual(self.wait(rid)["status"], "succeeded")
        store = Store(self.root / "app.sqlite3")
        with store.connection:
            store.connection.execute(
                "DELETE FROM stages WHERE project_id=? AND run_id=? AND stage_id IN ('P6','P7','P8')",
                (self.pid, rid),
            )
            store.connection.execute(
                "UPDATE runs SET status='interrupted',error_code='synthetic_crash' WHERE project_id=? AND id=?",
                (self.pid, rid),
            )
        store.close()
        value = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}").json()
        self.assertEqual(value["checkpoint"]["next_stage"], 6)
        value = self.advance_step(rid, value)
        self.assertEqual(value["status"], "succeeded", value)
        self.assertEqual(FixtureAdapter.calls.count("P3"), 1)
        self.assertEqual(FixtureAdapter.calls.count("P5"), 2)
        findings = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/findings").json()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["change_effect"], "regressed")

    def advance_step(self, rid, value):
        body = {"expected_checkpoint_sha256": value["checkpoint"]["sha256"]}
        pending = value["checkpoint"].get("pending_call")
        if pending:
            body |= {
                "confirmed_call": True,
                "call_preview_id": pending["artifact_id"],
                "expected_call_preview_sha256": pending["sha256"],
            }
        time.sleep(0.02)
        response = self.client.post(
            f"/api/v1/projects/{self.pid}/runs/{rid}/resume", json=body, headers=self.headers
        )
        self.assertEqual(response.status_code, 202, response.text)
        return self.wait(rid)

    def test_step_mode_pauses_between_calls_inside_the_same_stage_without_replay(self):
        import copy

        store = Store(self.root / "app.sqlite3")
        catalogue = store.catalog(self.pid, self.request["catalog_ids"][0])
        catalogue["catalog_id"] = "synthetic.per-call-consent"
        original = catalogue["requirements"][0]
        catalogue["requirements"] = [copy.deepcopy(original) for _ in range(3)]
        for index, record in enumerate(catalogue["requirements"]):
            record["id"] = f"USER-STEP-{index}"
        self.request["catalog_ids"] = [store.bind_catalog(self.pid, catalogue)]
        store.close()
        self.request["step_mode"] = True
        self.request["limits"]["batch_size"] = 1
        rid, _ = self.start("per-call-within-stage")
        value = self.wait(rid)
        p3_previews = []
        for _ in range(60):
            if value["status"] != "waiting_approval":
                break
            pending = value["checkpoint"].get("pending_call")
            if pending and pending["stage"] == "P3":
                self.assertEqual(value["checkpoint"]["next_stage"], 3)
                p3_previews.append(pending["payload_sha256"])
                self.assertEqual(FixtureAdapter.calls.count("P3"), len(p3_previews) - 1)
            value = self.advance_step(rid, value)
        self.assertEqual(value["status"], "succeeded", value)
        self.assertEqual(len(set(p3_previews)), 3)
        self.assertEqual(FixtureAdapter.calls.count("P3"), 3)
        self.assertEqual(FixtureAdapter.calls.count("P5"), 6)
        rows = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/assessments").json()
        self.assertEqual(len(rows), 3)
        self.assertTrue(
            all(row["proposed"]["assessment"] == "contradicted_by_spec" for row in rows)
        )

    def test_repair_payload_needs_new_consent_and_remains_bounded_after_resume(self):
        FixtureAdapter.mode = "empty-operations"
        self.request["step_mode"] = True
        rid, _ = self.start("repair-consent")
        value = self.wait(rid)
        for _ in range(10):
            if value["checkpoint"].get("pending_call"):
                break
            value = self.advance_step(rid, value)
        first = value["checkpoint"]["pending_call"]
        self.assertEqual(FixtureAdapter.calls, [])
        value = self.advance_step(rid, value)
        second = value["checkpoint"]["pending_call"]
        self.assertNotEqual(first["payload_sha256"], second["payload_sha256"])
        self.assertEqual(second["request_ordinal"], 2)
        self.assertEqual(FixtureAdapter.calls, ["P2"])
        preview = self.client.get(
            f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts/{second['artifact_id']}"
        ).json()
        self.assertTrue(preview["retry_state"]["repaired"])
        self.assertEqual(len(preview["payload"]["messages"]), 4)
        stale = self.client.post(
            f"/api/v1/projects/{self.pid}/runs/{rid}/resume",
            json={
                "expected_checkpoint_sha256": value["checkpoint"]["sha256"],
                "confirmed_call": True,
                "call_preview_id": first["artifact_id"],
                "expected_call_preview_sha256": first["sha256"],
            },
            headers=self.headers,
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(FixtureAdapter.calls, ["P2"])
        value = self.advance_step(rid, value)
        self.assertEqual(value["status"], "failed")
        self.assertEqual(FixtureAdapter.calls, ["P2", "P2"])
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        self.assertEqual(sum(item["kind"] == "model.call_approval" for item in artifacts), 2)
        self.assertEqual(sum(item["kind"] == "model.rejection" for item in artifacts), 2)

    def test_cancelled_stage_pause_has_unknown_rows_and_code_change_blocks_resume(self):
        from unittest.mock import patch

        self.request["step_mode"] = True
        rid, _ = self.start("cancel-before-assessment")
        value = self.wait(rid)
        self.assertEqual(value["checkpoint"]["next_stage"], 1)
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/assessments").json(), []
        )
        with patch("spec_enricher.run_api.current_algorithm_hashes", return_value={}):
            result = self.client.post(
                f"/api/v1/projects/{self.pid}/runs/{rid}/resume",
                json={"expected_checkpoint_sha256": value["checkpoint"]["sha256"]},
                headers=self.headers,
            )
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json()["code"], "runtime_methodology_changed")
        self.assertEqual(FixtureAdapter.calls, [])
        self.client.post(
            f"/api/v1/projects/{self.pid}/runs/{rid}/cancel", json={}, headers=self.headers
        ).raise_for_status()
        rows = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/assessments").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposed"]["assessment"], "unknown")
        self.assertEqual(rows[0]["proposed"]["implementation_verification"], "not_verified")

    def test_same_connection_id_can_pin_two_different_revisions_in_one_run(self):
        store = Store(self.root / "app.sqlite3")
        registry = Registry(store)
        first = registry.get(self.pid, "connection", self.request["connection"]["id"], 1)
        profile = dict(first["content"])
        profile["name"] = "Synthetic explicit second revision"
        second = registry.save(
            self.pid, "connection", profile, object_id=first["id"], expected_revision=1
        )
        registry.report(
            self.pid, second["id"], 2, {"status": "passed", "mode": "synthetic_test_adapter_only"}
        )
        registry.grant(self.pid, second["id"], 2, second["sha256"], profile["data_scopes"])
        self.request["stage_models"] = {"P5": {"id": first["id"], "revision": 2}}
        store.close()
        rid, _ = self.start("two-revisions")
        self.assertEqual(self.wait(rid)["status"], "succeeded")
        store = Store(self.root / "app.sqlite3")
        revisions = store.connection.execute(
            "SELECT revision FROM run_configs WHERE project_id=? AND run_id=? AND kind='connection' AND object_id=? ORDER BY revision",
            (self.pid, rid, first["id"]),
        ).fetchall()
        self.assertEqual(revisions, [(1,), (2,)])
        store.close()

    def test_proposal_reassessment_context_failure_is_explicit_partial_not_satisfied(self):
        FixtureAdapter.mode = "p8-context-exceeded"
        rid, _ = self.start("critique-context")
        result = self.wait(rid)
        self.assertEqual(result["status"], "partial", result)
        critique = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/critique").json()
        self.assertTrue(critique["unchecked_patch_ids"])
        self.assertTrue(critique["questions"])
        self.assertTrue(
            all(
                item["assessment"] == "unknown"
                for item in critique["reassessment_of_proposal_only"]
            )
        )
        matrix = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/assessments").json()
        self.assertEqual(matrix[0]["proposed"]["assessment"], "contradicted_by_spec")

    def test_operation_override_scope_hash_and_duplicate_rejected_before_model(self):
        from spec_enricher.effective_state import blocks

        delta = blocks(self.delta_id, self.delta)[0]
        baseline = blocks(self.baseline_id, self.baseline)[0]
        op = {
            "id": "user-explicit-change",
            "kind": "replace",
            "delta_block_id": delta.id,
            "target_block_id": baseline.id,
            "target_hash": "0" * 64,
            "explanation": "Explicit user clarification.",
            "question": None,
        }
        self.request["operation_overrides"] = [op]
        prefix = f"/api/v1/projects/{self.pid}/runs/preflight"
        self.assertEqual(
            self.client.post(prefix, json=self.request, headers=self.headers).status_code, 422
        )
        op["target_hash"] = baseline.hash
        self.request["operation_overrides"] = [op, op]
        self.assertEqual(
            self.client.post(prefix, json=self.request, headers=self.headers).status_code, 422
        )
        self.request["operation_overrides"] = [op]
        rid, _ = self.start("explicit-operation")
        self.assertEqual(self.wait(rid)["status"], "succeeded")
        self.assertNotIn("P2", FixtureAdapter.calls)

    def test_control_source_conflict_retains_known_scope_and_both_evidence(self):
        FixtureAdapter.mode = "source-conflict"
        conflicting = self.client.post(
            f"/api/v1/projects/{self.pid}/documents?filename=conflicting.md",
            content=b"Private documents are readable without server authorization. Same authority as baseline.\n",
            headers=self.headers,
        ).json()["id"]
        self.request["baseline_document_ids"].append(conflicting)
        rid, _ = self.start("known-scope-conflicting-control")
        result = self.wait(rid)
        self.assertEqual(result["status"], "partial", result)
        rows = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/assessments").json()
        for side in ("baseline", "proposed"):
            self.assertEqual(rows[0][side]["applicability"], "applicable")
            self.assertEqual(rows[0][side]["assessment"], "unknown")
            self.assertEqual(rows[0][side]["reason"], "source_conflict")
        findings = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/findings").json()
        self.assertEqual(findings[0]["type"], "source_conflict")
        actual = {e["snapshot_id"] for e in findings[0]["evidence_refs"]}
        self.assertTrue({self.baseline_id, conflicting} <= actual)
        self.assertTrue(actual <= {self.baseline_id, conflicting, self.delta_id})

    def test_p5_control_conflict_is_side_specific_and_never_claims_satisfaction(self):
        FixtureAdapter.mode = "p5-control-conflict"
        rid, _ = self.start("p5-conflict-known-scope")
        result = self.wait(rid)
        self.assertNotEqual(result["status"], "failed", result)
        rows = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/assessments").json()
        self.assertEqual(rows[0]["baseline"]["assessment"], "satisfied_by_spec")
        self.assertEqual(rows[0]["proposed"]["applicability"], "applicable")
        self.assertEqual(rows[0]["proposed"]["assessment"], "unknown")
        self.assertEqual(rows[0]["proposed"]["reason"], "source_conflict")
        findings = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/findings").json()
        self.assertEqual(findings[0]["type"], "source_conflict")
        FixtureAdapter.mode = "p5-inconsistent-conflict"
        FixtureAdapter.calls = []
        self.request["delta_document_id"] = self.client.post(
            f"/api/v1/projects/{self.pid}/documents?filename=inconsistent-conflict.md&role=delta",
            content=b"Additional conflicting design description.\n",
            headers=self.headers,
        ).json()["id"]
        rid, _ = self.start("p5-conflict-cannot-pass")
        self.assertEqual(self.wait(rid)["status"], "failed")
        self.assertEqual(FixtureAdapter.calls.count("P5"), 2)

    def test_context_only_policy_does_not_request_architectural_facts(self):
        store = Store(self.root / "app.sqlite3")
        catalog = store.catalog(self.pid, self.request["catalog_ids"][0])
        catalog["catalog_id"] = "synthetic.context-only"
        record = catalog["requirements"][0]
        record["id"] = "USER-CONTEXT-ONLY"
        for value in record["clause_normativity"].values():
            value.update(force="not_a_requirement", nature="context")
        self.request["catalog_ids"] = [store.bind_catalog(self.pid, catalog)]
        store.close()
        rid, _ = self.start("context-does-not-demand-evidence")
        result = self.wait(rid)
        self.assertNotEqual(result["status"], "failed", result)
        self.assertNotIn("P3", FixtureAdapter.calls)
        self.assertNotIn("P5", FixtureAdapter.calls)
        rows = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/assessments").json()
        self.assertEqual(rows[0]["proposed"]["assessment"], "not_assessed")
        self.assertEqual(rows[0]["proposed"]["reason"], "no_active_clauses")
        self.assertFalse(self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/findings").json())

    def test_partial_operation_override_preserves_source_spans_and_rejects_bad_quote(self):
        from spec_enricher.effective_state import blocks

        baseline = blocks(self.baseline_id, self.baseline)[0]
        delta = blocks(self.delta_id, self.delta)[0]
        op = {
            "id": "partial-change",
            "kind": "replace",
            "delta_block_id": delta.id,
            "target_block_id": baseline.id,
            "target_hash": baseline.hash,
            "target_quote": "Not a literal source sentence.",
            "explanation": "Change authorization only, preserve the document-owner premise.",
            "question": None,
        }
        self.request["operation_overrides"] = [op]
        response = self.client.post(
            f"/api/v1/projects/{self.pid}/runs/preflight", json=self.request, headers=self.headers
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(FixtureAdapter.calls)
        op["target_quote"] = "Every read requires server authorization."
        rid, _ = self.start("literal-source-spans")
        result = self.wait(rid)
        self.assertEqual(result["stages"][2]["status"], "succeeded", result)
        metadata = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        artifact = next(a for a in metadata if a["kind"] == "stage.P2.output")
        output = self.client.get(
            f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts/{artifact['id']}"
        ).json()
        spans = [b for b in output["proposed_blocks"] if b["snapshot_id"] == self.baseline_id]
        self.assertTrue(any("Private documents have owners." in b["text"] for b in spans))
        for b in spans:
            self.assertEqual(self.baseline[b["byte_start"] : b["byte_end"]], b["text"].encode())
            self.assertNotIn("Every read requires server authorization.", b["text"])
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{self.pid}/documents/{self.baseline_id}").content,
            self.baseline,
        )

    def test_multiblock_operation_ids_are_owned_by_server_and_empty_coverage_fails_closed(self):
        FixtureAdapter.mode = "multi-add"
        delta = b"Add a display date.\n\nAdd a second display label.\n"
        self.request["delta_document_id"] = self.client.post(
            f"/api/v1/projects/{self.pid}/documents?filename=multi.md&role=delta",
            content=delta,
            headers=self.headers,
        ).json()["id"]
        rid, _ = self.start("multi-operation")
        result = self.wait(rid)
        self.assertEqual(result["stages"][2]["status"], "succeeded", result)
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        output = next(item for item in artifacts if item["kind"] == "stage.P2.output")
        operations = self.client.get(
            f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts/{output['id']}"
        ).json()["operations"]
        self.assertEqual(len(operations), 2)
        self.assertEqual(len({op["id"] for op in operations}), 2)
        self.assertTrue(all(op["kind"] == "add" for op in operations))
        self.assertTrue(all(op["id"].startswith("operation-") for op in operations))
        FixtureAdapter.mode = "empty-operations"
        self.request["delta_document_id"] = self.client.post(
            f"/api/v1/projects/{self.pid}/documents?filename=empty-operations.md&role=delta",
            content=delta,
            headers=self.headers,
        ).json()["id"]
        rid, _ = self.start("empty-operation")
        result = self.wait(rid)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stages"][2]["stage_id"], "P2")
        self.assertEqual(result["stages"][2]["status"], "failed")
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        self.assertEqual(sum(item["kind"] == "model.request" for item in artifacts), 2)

    def test_generation_batches_findings_and_merges_identical_targets_without_losing_refs(self):
        import copy

        store = Store(self.root / "app.sqlite3")
        catalogue = store.catalog(self.pid, self.request["catalog_ids"][0])
        original = catalogue["requirements"][0]
        catalogue["catalog_id"] = "synthetic.batch.policy"
        catalogue["requirements"] = [copy.deepcopy(original) for _ in range(7)]
        for index, record in enumerate(catalogue["requirements"]):
            record["id"] = "USER-BATCH-" + str(index)
        self.request["catalog_ids"] = [store.bind_catalog(self.pid, catalogue)]
        self.request["limits"]["batch_size"] = 2
        store.close()
        rid, _ = self.start("batch-generation")
        result = self.wait(rid)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(FixtureAdapter.calls.count("P7"), 4)
        proposal = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/proposal").json()
        self.assertEqual(len(proposal["patches"]), 1)
        self.assertEqual(len(proposal["patches"][0]["finding_ids"]), 7)
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts").json()
        payloads = [
            json.loads(
                self.client.get(
                    f"/api/v1/projects/{self.pid}/runs/{rid}/artifacts/{item['id']}"
                ).content
            )
            for item in artifacts
            if item["kind"] == "model.request"
        ]
        generation = [
            json.loads(item["messages"][1]["content"])["data"]
            for item in payloads
            if "patches"
            in item.get("response_format", {})
            .get("json_schema", {})
            .get("schema", {})
            .get("properties", {})
        ]
        self.assertEqual(len(generation), 4)
        self.assertTrue(all(len(data["findings"]) <= 2 for data in generation))
        self.assertTrue(
            all(
                "baseline_assessment" not in finding
                for data in generation
                for finding in data["findings"]
            )
        )
        self.assertTrue(
            all(
                context["clause_logic"] == {"clause": "c1"}
                and context["clause_normativity"]["c1"]["force"] == "required"
                for data in generation
                for context in data["policy_context"]
            )
        )

    def test_run_comparison_warns_on_changed_methodology_and_rejects_other_project(self):
        first, _ = self.start("comparison-first")
        self.wait(first)
        second, _ = self.start("comparison-second")
        self.wait(second)
        prefix = f"/api/v1/projects/{self.pid}/runs/{first}/compare/{second}"
        same = self.client.get(prefix).json()
        self.assertTrue(same["comparable_context"], same)
        self.assertEqual(
            self.client.get(
                f"/api/v1/projects/{self.other}/runs/{first}/compare/{second}"
            ).status_code,
            404,
        )
        store = Store(self.root / "app.sqlite3")
        row = store.connection.execute(
            "SELECT manifest FROM runs WHERE project_id=? AND id=?", (self.pid, second)
        ).fetchone()
        manifest = json.loads(row[0])
        manifest["methodology_hash"] = "synthetic-different-method"
        with store.connection:
            store.connection.execute(
                "UPDATE runs SET manifest=? WHERE project_id=? AND id=?",
                (json.dumps(manifest), self.pid, second),
            )
        store.close()
        changed = self.client.get(prefix).json()
        self.assertFalse(changed["comparable_context"])
        self.assertEqual(changed["differences"][0]["field"], "methodology_hash")

    def test_author_left_condition_undefined_is_not_inferred_by_model_or_override(self):
        store = Store(self.root / "app.sqlite3")
        catalog = store.catalog(self.pid, self.request["catalog_ids"][0])
        catalog["catalog_id"] = "own-undefined-condition"
        catalog["requirements"][0]["version"] = "1.1.0"
        catalog["requirements"][0]["policy_scope"] = {
            "authoring_unknown_predicate_ids": ["private-read"]
        }
        cid = store.bind_catalog(self.pid, catalog)
        store.close()
        self.request["catalog_ids"] = [cid]
        run_id, _ = self.start("author-undefined")
        value = self.wait(run_id)
        self.assertIn(value["status"], {"succeeded", "partial"})
        row = self.client.get(f"/api/v1/projects/{self.pid}/runs/{run_id}/assessments").json()[0]
        self.assertEqual(row["proposed"]["applicability"], "unknown")
        self.assertEqual(row["proposed"]["assessment"], "unknown")
        self.assertNotIn("P3", FixtureAdapter.calls)
        self.assertNotIn("P5", FixtureAdapter.calls)
        self.request["fact_overrides"] = [
            {
                "requirement_id": catalog["requirements"][0]["id"],
                "predicate_id": "private-read",
                "side": "proposed",
                "value": True,
                "reason": "Cannot invent the missing meaning of this condition.",
            }
        ]
        response = self.client.post(
            f"/api/v1/projects/{self.pid}/runs/preflight", json=self.request, headers=self.headers
        )
        self.assertEqual(response.status_code, 422)

    def test_step_checkpoints_resume_without_replaying_completed_stages(self):
        self.request["step_mode"] = True
        run_id, _ = self.start("step-mode")
        value = self.wait(run_id)
        self.assertEqual(value["status"], "waiting_approval")
        self.assertEqual(value["checkpoint"]["next_stage"], 1)
        self.assertEqual(FixtureAdapter.calls, [])
        self.assertEqual(
            self.client.post(
                f"/api/v1/projects/{self.pid}/runs/{run_id}/resume",
                json={"expected_checkpoint_sha256": "0" * 64},
                headers=self.headers,
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/projects/{self.other}/runs/{run_id}/resume",
                json={"expected_checkpoint_sha256": value["checkpoint"]["sha256"]},
                headers=self.headers,
            ).status_code,
            404,
        )
        confirmed_calls = 0
        previous_stage = 1
        for _attempt in range(30):
            if value["status"] not in {"waiting_approval", "interrupted"}:
                break
            self.assertGreaterEqual(value["checkpoint"]["next_stage"], previous_stage)
            previous_stage = value["checkpoint"]["next_stage"]
            body = {"expected_checkpoint_sha256": value["checkpoint"]["sha256"]}
            pending = value["checkpoint"].get("pending_call")
            if pending:
                before_calls = list(FixtureAdapter.calls)
                refused = self.client.post(
                    f"/api/v1/projects/{self.pid}/runs/{run_id}/resume",
                    json=body,
                    headers=self.headers,
                )
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertEqual(FixtureAdapter.calls, before_calls)
                preview = self.client.get(
                    f"/api/v1/projects/{self.pid}/runs/{run_id}/artifacts/{pending['artifact_id']}"
                ).json()
                self.assertEqual(preview["payload_sha256"], pending["payload_sha256"])
                body |= {
                    "confirmed_call": True,
                    "call_preview_id": pending["artifact_id"],
                    "expected_call_preview_sha256": pending["sha256"],
                }
                confirmed_calls += 1
            time.sleep(0.02)  # worker teardown follows persisted checkpoint
            response = self.client.post(
                f"/api/v1/projects/{self.pid}/runs/{run_id}/resume",
                json=body,
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 202, response.text)
            value = self.wait(run_id)
        self.assertEqual(value["status"], "succeeded", value)
        self.assertEqual(FixtureAdapter.calls, ["P2", "P3", "P5", "P5", "P7", "P8", "P8-review"])
        self.assertEqual(confirmed_calls, 7)
        self.assertEqual(
            len([stage for stage in value["stages"] if stage["status"] == "succeeded"]), 9
        )
        self.assertEqual(value["stages"][-1]["usage"]["request_count_total"], 7)
        self.assertGreater(
            value["stages"][-1]["usage"]["conservative_token_count"],
            value["stages"][2]["usage"]["conservative_token_count"],
        )
        rows = self.client.get(f"/api/v1/projects/{self.pid}/runs/{run_id}/assessments").json()
        self.assertEqual(rows[0]["baseline"]["assessment"], "satisfied_by_spec")
        self.assertEqual(rows[0]["proposed"]["assessment"], "contradicted_by_spec")

    def test_frozen_prompts_and_ordered_declarative_hooks(self):
        from spec_enricher.pipeline_config import CONTRACTS
        from spec_enricher.prompts import defaults

        store = Store(self.root / "app.sqlite3")
        registry = Registry(store)
        config = registry.get(self.pid, "connection", self.request["connection"]["id"])
        profile = config["content"] | {
            "data_scopes": ["delta", "baseline_fragments", "catalog", "skill_outputs"]
        }
        config = registry.save(
            self.pid, "connection", profile, object_id=config["id"], expected_revision=1
        )
        registry.report(
            self.pid,
            config["id"],
            config["revision"],
            {"status": "passed", "mode": "synthetic_only"},
        )
        registry.grant(
            self.pid, config["id"], config["revision"], config["sha256"], profile["data_scopes"]
        )
        self.request["connection"] = {"id": config["id"], "revision": config["revision"]}
        schema = {
            "type": "object",
            "properties": {"annotations": {"type": "array", "items": {"type": "string"}}},
            "required": ["annotations"],
            "additionalProperties": False,
        }
        annotation = registry.save(
            self.pid,
            "prompt",
            {
                "name": "Annotation",
                "version": "1.0.0",
                "stage": "skill",
                "template": "Return observations only; no permissions.",
                "output_schema": schema,
                "status": "published",
                "changelog": "Synthetic fixture",
            },
        )
        replacement = registry.save(
            self.pid,
            "prompt",
            {
                "name": "Replacement",
                "version": "1.0.0",
                "stage": "P5",
                "template": "PINNED_REPLACEMENT_V1 " + defaults()["P5"]["template"],
                "output_schema": CONTRACTS["P5"].model_json_schema(),
                "status": "published",
                "changelog": "Synthetic replacement",
            },
        )
        refs = []
        for hook in ("before", "replace", "after"):
            prompt = replacement if hook == "replace" else annotation
            skill = registry.save(
                self.pid,
                "skill",
                {
                    "version": "1.0.0",
                    "name": hook,
                    "description": "Synthetic declared hook",
                    "stage": "P5",
                    "hook": hook,
                    "input_schema": {"type": "object"},
                    "output_schema": prompt["content"]["output_schema"],
                    "prompt_ref": {"id": prompt["id"], "revision": 1},
                    "data_scopes": profile["data_scopes"],
                    "enabled": True,
                },
            )
            refs.append({"id": skill["id"], "revision": 1})
        self.request["skill_refs"] = list(reversed(refs))
        store.close()
        run_id, _ = self.start("declared-hooks")
        # Editing the head cannot change the already frozen replacement.
        store = Store(self.root / "app.sqlite3")
        Registry(store).save(
            self.pid,
            "prompt",
            replacement["content"] | {"template": "UNPINNED_V2", "version": "2.0.0"},
            object_id=replacement["id"],
            expected_revision=1,
        )
        store.close()
        result = self.wait(run_id)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(
            FixtureAdapter.calls,
            [
                "P2",
                "P3",
                "skill",
                "P5",
                "skill",
                "skill",
                "P5",
                "skill",
                "P7",
                "P8",
                "P8-review",
            ],
        )
        manifest = self.client.get(f"/api/v1/projects/{self.pid}/runs/{run_id}/manifest").json()
        self.assertEqual(
            next(x for x in manifest["hook_order"] if x["stage"] == "P5")["calls"],
            [ref["id"] for ref in refs],
        )
        artifacts = self.client.get(f"/api/v1/projects/{self.pid}/runs/{run_id}/artifacts").json()
        requests = [
            self.client.get(
                f"/api/v1/projects/{self.pid}/runs/{run_id}/artifacts/{item['id']}"
            ).text
            for item in artifacts
            if item["kind"] == "model.request"
        ]
        self.assertTrue(any("PINNED_REPLACEMENT_V1" in item for item in requests))
        self.assertFalse(any("UNPINNED_V2" in item for item in requests))
        self.assertTrue(
            any(
                "untrusted_annotation_not_permissions_or_review_decision" in item
                for item in requests
            )
        )

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        FixtureAdapter.calls = []
        FixtureAdapter.mode = "regression"
        FixtureAdapter.pause = None
        FixtureAdapter.request_started = threading.Event()
        self.app = create_app(self.root, ORIGIN, adapter_factory=FixtureAdapter)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        token = (self.root / "bootstrap.token").read_text()
        login = self.client.post(
            "/api/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
        ).json()
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        self.pid = self.client.post(
            "/api/v1/projects", json={"name": "Synthetic acceptance"}, headers=self.headers
        ).json()["id"]
        self.other = self.client.post(
            "/api/v1/projects", json={"name": "Isolated B"}, headers=self.headers
        ).json()["id"]
        self.baseline = (
            b"Private documents have owners. Every read requires server authorization.\n"
        )
        self.delta = (
            b"The product keeps private documents. Remove server authorization for every read.\n"
        )
        self.baseline_id = self.client.post(
            f"/api/v1/projects/{self.pid}/documents?filename=baseline.md",
            content=self.baseline,
            headers=self.headers,
        ).json()["id"]
        self.delta_id = self.client.post(
            f"/api/v1/projects/{self.pid}/documents?filename=delta.md&role=delta",
            content=self.delta,
            headers=self.headers,
        ).json()["id"]
        store = Store(self.root / "app.sqlite3")
        catalog_id = store.bind_catalog(
            self.pid, json.loads(Path("templates/requirements.example.json").read_text())
        )
        profile = ConnectionProfile(
            name="Explicit synthetic test adapter",
            endpoint="http://127.0.0.1:1234/v1",
            local_addresses=["127.0.0.1"],
            model="fixture-only",
            enabled=True,
            context_budget=200000,
            data_scopes=["delta", "baseline_fragments", "catalog"],
        )
        registry = Registry(store)
        config = registry.save(self.pid, "connection", profile.model_dump())
        registry.report(
            self.pid, config["id"], 1, {"status": "passed", "mode": "synthetic_test_adapter_only"}
        )
        registry.grant(self.pid, config["id"], 1, config["sha256"], profile.data_scopes)
        store.close()
        self.request = {
            "delta_document_id": self.delta_id,
            "baseline_document_ids": [self.baseline_id],
            "catalog_ids": [catalog_id],
            "connection": {"id": config["id"], "revision": 1},
            "limits": {"max_tokens": 2000000},
        }

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def start(self, key="synthetic-1"):
        preflight = self.client.post(
            f"/api/v1/projects/{self.pid}/runs/preflight", json=self.request, headers=self.headers
        )
        self.assertEqual(preflight.status_code, 200, preflight.text)
        self.assertEqual(preflight.json()["execution_mode"], "synthetic_test_adapter")
        self.assertIn("first_payload_preview", preflight.json())
        body = {
            "request": self.request,
            "expected_plan_sha256": preflight.json()["plan_sha256"],
            "idempotency_key": key,
        }
        result = self.client.post(
            f"/api/v1/projects/{self.pid}/runs", json=body, headers=self.headers
        )
        self.assertEqual(result.status_code, 202, result.text)
        return result.json()["id"], body

    def wait(self, run_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = self.client.get(f"/api/v1/projects/{self.pid}/runs/{run_id}").json()
            if value["status"] not in {"queued", "running"}:
                return value
            time.sleep(0.02)
        self.fail("Synthetic run timed out")

    def test_p0_to_p8_regression_artifacts_idempotency_and_review_scope(self):
        run_id, body = self.start()
        result = self.wait(run_id)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(
            [item["stage_id"] for item in result["stages"]], [f"P{i}" for i in range(9)]
        )
        self.assertEqual(FixtureAdapter.calls, ["P2", "P3", "P5", "P5", "P7", "P8", "P8-review"])
        assessments = self.client.get(
            f"/api/v1/projects/{self.pid}/runs/{run_id}/assessments"
        ).json()
        self.assertEqual(assessments[0]["change_effect"], "regressed")
        self.assertEqual(assessments[0]["baseline"]["assessment"], "satisfied_by_spec")
        self.assertEqual(assessments[0]["proposed"]["assessment"], "contradicted_by_spec")
        self.assertEqual(assessments[0]["proposed"]["implementation_verification"], "not_verified")
        findings = self.client.get(f"/api/v1/projects/{self.pid}/runs/{run_id}/findings").json()
        self.assertEqual(findings[0]["type"], "explicit_contradiction")
        self.assertTrue(findings[0]["evidence_refs"])
        again = self.client.post(
            f"/api/v1/projects/{self.pid}/runs", json=body, headers=self.headers
        )
        self.assertEqual(again.json()["id"], run_id)
        self.assertTrue(again.json()["replayed"])
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{self.other}/runs/{run_id}").status_code, 404
        )
        original = self.client.get(f"/api/v1/projects/{self.pid}/documents/{self.delta_id}")
        self.assertEqual(original.content, self.delta)
        decision = {
            "expected_revision": 0,
            "target_kind": "finding",
            "target_id": findings[0]["id"],
            "decision": "rejected",
            "comment": "Documentary assessment is retained; review is separate.",
        }
        response = self.client.post(
            f"/api/v1/projects/{self.pid}/runs/{run_id}/decisions",
            json=decision,
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["assessment_unchanged"])
        self.assertEqual(
            self.client.post(
                f"/api/v1/projects/{self.pid}/runs/{run_id}/decisions",
                json=decision,
                headers=self.headers,
            ).status_code,
            409,
        )

    def test_invented_requirement_repair_bounded_and_partial_coverage_visible(self):
        FixtureAdapter.mode = "invented"
        run_id, _ = self.start()
        result = self.wait(run_id)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(FixtureAdapter.calls.count("P5"), 2)
        rows = self.client.get(f"/api/v1/projects/{self.pid}/runs/{run_id}/assessments").json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposed"]["assessment"], "not_assessed")
        self.assertEqual(rows[0]["proposed"]["reason"], "pipeline_incomplete")

    def test_context_budget_exhaustion_is_explicit_and_sends_no_oversized_call(self):
        store = Store(self.root / "app.sqlite3")
        registry = Registry(store)
        ref = self.request["connection"]
        previous = registry.get(self.pid, "connection", ref["id"], ref["revision"])
        config = registry.save(
            self.pid, "connection", previous["content"] | {"context_budget": 16000}
        )
        registry.report(
            self.pid, config["id"], 1, {"status": "passed", "mode": "synthetic_test_adapter_only"}
        )
        registry.grant(
            self.pid, config["id"], 1, config["sha256"], config["content"]["data_scopes"]
        )
        store.close()
        self.request["connection"] = {"id": config["id"], "revision": 1}
        rid, _ = self.start("insufficient-context-budget")
        result = self.wait(rid)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(result["error_code"], "context_budget_exceeded")
        stage = next(s for s in result["stages"] if s["status"] == "partial")
        self.assertEqual(stage["errors"][0]["code"], "context_budget_exceeded")
        self.assertIn("no request was sent", stage["errors"][0]["message"])
        self.assertNotIn("P5", FixtureAdapter.calls)
        self.assertNotIn("P7", FixtureAdapter.calls)

    def test_unknown_scope_questions_and_budget_exhaustion_never_pass(self):
        FixtureAdapter.mode = "unknown"
        run_id, _ = self.start()
        result = self.wait(run_id)
        self.assertEqual(result["status"], "partial", result)
        rows = self.client.get(f"/api/v1/projects/{self.pid}/runs/{run_id}/assessments").json()
        self.assertEqual(rows[0]["proposed"]["applicability"], "unknown")
        self.assertEqual(rows[0]["proposed"]["assessment"], "unknown")
        self.assertNotIn("P5", FixtureAdapter.calls)
        self.request["limits"] = {"max_tokens": 2000000, "max_requests": 1}
        run_id, _ = self.start("budgeted")
        result = self.wait(run_id)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(result["error_code"], "budget_exhausted")

    def test_selective_export_and_original_hash_review_conflicts(self):
        run_id, _ = self.start()
        self.assertEqual(self.wait(run_id)["status"], "succeeded")
        prefix = f"/api/v1/projects/{self.pid}/runs/{run_id}"
        proposal = self.client.get(prefix + "/proposal").json()
        patch_id = proposal["patches"][0]["id"]
        export = {
            "expected_review_revision": 0,
            "expected_original_sha256": proposal["original_sha256"],
            "accepted_patch_ids": [],
            "idempotency_key": "none-selected",
        }
        response = self.client.post(prefix + "/exports", json=export, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["requires_new_analysis"])
        bundle = self.client.get(prefix + "/exports/" + response.json()["id"]).content
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "delta.original.md",
                    "delta.proposed.md",
                    "review.json",
                    "review.md",
                    "delta.patch",
                    "run-manifest.json",
                },
            )
            self.assertEqual(archive.read("delta.proposed.md"), self.delta)
            self.assertEqual(archive.read("delta.original.md"), self.delta)
            report = json.loads(archive.read("review.json"))
            scope = report["proposal_assessment_scope"]
            self.assertEqual(report["document_status"], "draft_requires_new_analysis")
            self.assertFalse(scope["p8_applies_to_selected_document"])
            self.assertEqual(scope["selected_document_assessment"]["assessment"], "not_assessed")
            self.assertNotEqual(
                scope["selected_document_sha256"], scope["assessed_candidate_sha256"]
            )
            self.assertEqual(
                json.loads(archive.read("run-manifest.json"))["proposal_assessment_scope"], scope
            )
            self.assertIn(
                b"Selected document requires new analysis: True", archive.read("review.md")
            )
        self.assertTrue(
            self.client.post(prefix + "/exports", json=export, headers=self.headers).json()[
                "replayed"
            ]
        )
        decision = {
            "expected_revision": 0,
            "target_kind": "patch",
            "target_id": patch_id,
            "decision": "accepted",
            "comment": "Accept only this proposed design change.",
        }
        self.assertEqual(
            self.client.post(
                prefix + "/decisions", json=decision, headers=self.headers
            ).status_code,
            200,
        )
        export.update(
            expected_review_revision=1, accepted_patch_ids=[patch_id], idempotency_key="selected"
        )
        response = self.client.post(prefix + "/exports", json=export, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["requires_new_analysis"])
        bundle = self.client.get(prefix + "/exports/" + response.json()["id"]).content
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertNotEqual(archive.read("delta.proposed.md"), self.delta)
            self.assertIn(b"Security acceptance", archive.read("delta.proposed.md"))
            self.assertEqual(
                json.loads(archive.read("review.json"))["accepted_patch_ids"], [patch_id]
            )
            scope = json.loads(archive.read("review.json"))["proposal_assessment_scope"]
            self.assertTrue(scope["p8_applies_to_selected_document"])
            self.assertEqual(scope["selected_document_sha256"], scope["assessed_candidate_sha256"])
        export.update(expected_review_revision=0, idempotency_key="stale")
        self.assertEqual(
            self.client.post(prefix + "/exports", json=export, headers=self.headers).status_code,
            409,
        )
        export.update(
            expected_review_revision=1,
            expected_original_sha256="0" * 64,
            idempotency_key="wrong-hash",
        )
        self.assertEqual(
            self.client.post(prefix + "/exports", json=export, headers=self.headers).status_code,
            409,
        )
        foreign = f"/api/v1/projects/{self.other}/runs/{run_id}/exports/{response.json()['id']}"
        self.assertEqual(self.client.get(foreign).status_code, 404)
        purge_prefix = f"/api/v1/projects/{self.pid}/purge"
        preview = self.client.post(
            purge_prefix + "/preview",
            json={"kind": "project", "object_id": self.pid},
            headers=self.headers,
        ).json()
        self.assertEqual(preview["counts"]["exports"], 2)

        purged = self.client.post(
            purge_prefix + "/commit",
            json={
                "kind": "project",
                "object_id": self.pid,
                "expected_sha256": preview["sha256"],
                "confirmed": True,
                "confirmation": "DELETE HISTORY",
            },
            headers=self.headers,
        )
        self.assertEqual(purged.status_code, 200, purged.text)
        self.assertEqual(purged.json()["status"], "complete")
        self.assertEqual(self.client.get(prefix).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/projects/{self.other}").status_code, 200)

    def test_selected_and_edited_preview_keep_the_original_p8_assessment_target(self):
        run_id, _ = self.start("selective-preview-assessment-scope")
        self.assertEqual(self.wait(run_id)["status"], "succeeded")
        prefix = f"/api/v1/projects/{self.pid}/runs/{run_id}"
        proposal = self.client.get(prefix + "/proposal").json()
        patch = proposal["patches"][0]
        target = proposal["candidate_artifact"]["sha256"]
        calls = list(FixtureAdapter.calls)
        for selected, edits, requires_analysis in [
            ([], {}, True),
            ([patch["id"]], {}, False),
            ([patch["id"]], {patch["id"]: "An independently edited product design.\n"}, True),
        ]:
            with self.subTest(selected=selected, edited=bool(edits)):
                response = self.client.post(
                    prefix + "/proposal/preview",
                    json={
                        "expected_review_revision": 0,
                        "expected_original_sha256": proposal["original_sha256"],
                        "selected_patch_ids": selected,
                        "edited_replacements": edits,
                    },
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 200, response.text)
                preview = response.json()
                self.assertEqual(preview["requires_new_analysis"], requires_analysis)
                self.assertEqual(
                    preview["proposal_assessment_scope"]["assessed_candidate_sha256"], target
                )
                self.assertEqual(
                    preview["proposal_assessment_scope"]["p8_applies_to_selected_document"],
                    not requires_analysis,
                )
        self.assertEqual(FixtureAdapter.calls, calls)

    def test_full_selection_without_completed_p8_is_explicitly_unassessed(self):
        FixtureAdapter.mode = "candidate-extra-retrieval"
        run_id, _ = self.start("export-without-completed-p8")
        self.assertEqual(self.wait(run_id)["status"], "failed")
        prefix = f"/api/v1/projects/{self.pid}/runs/{run_id}"
        proposal = self.client.get(prefix + "/proposal").json()
        patch = proposal["patches"][0]
        self.client.post(
            prefix + "/decisions",
            json={
                "expected_revision": 0,
                "target_kind": "patch",
                "target_id": patch["id"],
                "decision": "accepted",
                "comment": "Export as an explicitly unassessed draft.",
            },
            headers=self.headers,
        ).raise_for_status()
        result = self.client.post(
            prefix + "/exports",
            json={
                "expected_review_revision": 1,
                "expected_original_sha256": proposal["original_sha256"],
                "accepted_patch_ids": [patch["id"]],
                "idempotency_key": "unassessed-full-selection",
            },
            headers=self.headers,
        )
        self.assertEqual(result.status_code, 200, result.text)
        scope = result.json()["proposal_assessment_scope"]
        self.assertTrue(scope["requires_new_analysis"])
        self.assertFalse(scope["p8_applies_to_selected_document"])
        self.assertIsNone(scope["assessed_candidate_sha256"])
        self.assertEqual(scope["selected_document_assessment"]["assessment"], "not_assessed")
        self.assertEqual(scope["selected_document_assessment"]["reason"], "P8_not_completed")

    def test_cancel_stops_new_model_calls_after_inflight_finishes(self):
        FixtureAdapter.pause = threading.Event()
        try:
            run_id, _ = self.start()
            self.assertTrue(FixtureAdapter.request_started.wait(2))
            started = time.monotonic()
            response = self.client.post(
                f"/api/v1/projects/{self.pid}/runs/{run_id}/cancel", headers=self.headers
            )
            self.assertEqual(response.status_code, 200)
            self.assertLess(time.monotonic() - started, 2)
            FixtureAdapter.pause.set()
            self.assertEqual(self.wait(run_id)["status"], "cancelled")
            self.assertEqual(FixtureAdapter.calls, ["P2"])
        finally:
            FixtureAdapter.pause.set()

    def test_explicit_answers_create_new_run_without_changing_old_unknown(self):
        FixtureAdapter.mode = "unknown"
        first, _ = self.start("unknown-first")
        self.assertEqual(self.wait(first)["status"], "partial")
        questions = self.client.get(f"/api/v1/projects/{self.pid}/runs/{first}/questions").json()
        self.assertEqual(questions["total_unknown_facts"], 2)
        self.assertTrue(questions["facts"][0]["predicate_text"])
        self.request["fact_overrides"] = [
            {
                "requirement_id": item["requirement_id"],
                "predicate_id": item["predicate_id"],
                "side": item["side"],
                "value": True,
                "reason": "Author confirms private document reads in this synthetic fixture.",
            }
            for item in questions["facts"]
        ]
        second, _ = self.start("author-answers")
        self.assertEqual(self.wait(second)["status"], "succeeded")
        old = self.client.get(f"/api/v1/projects/{self.pid}/runs/{first}/assessments").json()[0]
        new = self.client.get(f"/api/v1/projects/{self.pid}/runs/{second}/assessments").json()[0]
        self.assertEqual(old["proposed"]["assessment"], "unknown")
        self.assertEqual(new["proposed"]["assessment"], "contradicted_by_spec")
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{self.pid}/runs/{second}/questions").json()[
                "total_unknown_facts"
            ],
            0,
        )
