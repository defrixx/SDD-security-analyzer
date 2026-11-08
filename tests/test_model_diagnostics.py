import json
import unittest

from pydantic import ValidationError

from spec_enricher.effective_state import blocks
from spec_enricher.llm import OutputSchemaError
from spec_enricher.model_diagnostics import (
    safe_network_failure,
    safe_output_rejection,
    safe_rejection,
)
from spec_enricher.network import NetworkError
from spec_enricher.stage_contracts import (
    EvidenceValidationError,
    FactsOutput,
    JudgmentsOutput,
    validate_facts,
    validate_judgments,
)


class RepairDiagnosticsTests(unittest.TestCase):
    def test_generic_union_error_can_explain_known_conflict_failure_without_echoing_values(self):
        marker = "PRIVATE_CONFLICT_DIAGNOSTIC_93071"
        value = {
            "judgments": [
                {
                    "requirement_id": "USER-DIAG",
                    "version": "1.0.0",
                    "clause_id": "c1",
                    "side": "baseline",
                    "applicable_design": marker,
                    "evidence": [{"block_id": "one", "quote": marker}],
                    "explanation": "A conflicting claim without the second source.",
                    "support": "conflicting",
                    "verdict": "unknown",
                    "question": "Which source is authoritative?",
                    "control_removal": None,
                }
            ],
            "retrieval_queries": [],
            "questions": [],
        }
        error = OutputSchemaError([{"location": ["judgments", 0], "keyword": "anyOf"}])
        for mode in ("json_schema", "prompt_json"):
            with self.subTest(mode=mode):
                content = json.dumps(value)
                if mode == "prompt_json":
                    content = "```json\n" + content + "\n```"
                raw = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                result = safe_output_rejection(
                    error,
                    "P5",
                    repair_permitted=True,
                    raw_content=raw,
                    contract=JudgmentsOutput,
                    output_mode=mode,
                )
                self.assertEqual(
                    result["reason_code"], "conflicting_judgment_requires_unknown_and_two_sources"
                )
                self.assertTrue(result["diagnostic_only_dto_refinement"])
                self.assertEqual(result["schema_validation_paths"][0]["type"], "schema_anyOf")
                self.assertIn("DISTINCT block_ids", result["repair_hint"])
                self.assertNotIn(marker, json.dumps(result))

    def test_diagnostic_refinement_never_accepts_bound_schema_failure_or_native_fences(self):
        error = OutputSchemaError([{"location": ["judgments"], "keyword": "minItems"}])
        valid_dto = {"judgments": [], "retrieval_queries": [], "questions": []}
        for content in (json.dumps(valid_dto), "```json\n" + json.dumps(valid_dto) + "\n```"):
            with self.subTest(fenced=content.startswith("```")):
                raw = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                result = safe_output_rejection(
                    error,
                    "P5",
                    repair_permitted=False,
                    raw_content=raw,
                    contract=JudgmentsOutput,
                    output_mode="json_schema",
                )
                self.assertEqual(result, safe_rejection(error, "P5", repair_permitted=False))
                self.assertNotIn("diagnostic_only_dto_refinement", result)

    def test_bound_conflict_pair_limit_does_not_change_historical_dto_capacity(self):
        value = {
            "judgments": [
                {
                    "requirement_id": "USER-DIAG",
                    "version": "1.0.0",
                    "clause_id": "c1",
                    "side": "proposed",
                    "applicable_design": "PRIVATE_QUOTE_LIMIT_38271",
                    "evidence": [
                        {"block_id": str(n), "quote": "PRIVATE_QUOTE_LIMIT_38271"} for n in range(3)
                    ],
                    "explanation": "Three supplied sources; the wire contract needs a representative pair.",
                    "support": "conflicting",
                    "verdict": "unknown",
                    "question": "Which source is authoritative?",
                    "control_removal": None,
                }
            ],
            "retrieval_queries": [],
            "questions": [],
        }
        self.assertEqual(len(JudgmentsOutput.model_validate(value).judgments[0].evidence), 3)
        raw = json.dumps({"choices": [{"message": {"content": json.dumps(value)}}]}).encode()
        result = safe_output_rejection(
            OutputSchemaError([{"location": ["judgments", 0], "keyword": "anyOf"}]),
            "P5",
            repair_permitted=True,
            raw_content=raw,
            contract=JudgmentsOutput,
            output_mode="json_schema",
        )
        self.assertEqual(result["reason_code"], "bound_conflict_requires_two_citations")
        self.assertEqual(result["validation_paths"][0]["path"], ["judgments", 0, "evidence"])
        self.assertIn("exactly TWO", result["repair_hint"])
        self.assertNotIn("PRIVATE_QUOTE_LIMIT_38271", json.dumps(result))

    def test_transport_metadata_keeps_bounded_status_but_never_unknown_messages(self):
        self.assertEqual(
            safe_network_failure(NetworkError("http_error", 400, 2), "P5"),
            {"stage": "P5", "code": "http_error", "http_status": 400, "retry_after_seconds": 2},
        )
        marker = "PRIVATE_PROVIDER_ERROR_38739"
        for status, retry in ((marker, marker), (True, True), (999, -1)):
            with self.subTest(status_type=type(status).__name__):
                value = safe_network_failure(NetworkError(marker, status, retry), marker)
                self.assertEqual(
                    value,
                    {
                        "stage": "model",
                        "code": "transport_error",
                        "http_status": None,
                        "retry_after_seconds": None,
                    },
                )
                self.assertNotIn(marker, json.dumps(value))

    def facts_input(self):
        before = blocks("baseline", b"The feature is present.")[0]
        after = blocks("proposed", b"The feature includes preview.")[0]
        record = {
            "id": "USER-DIAG",
            "version": "1.0.0",
            "applicability": {"predicates": [{"id": "feature"}]},
        }
        value = {
            "facts": [
                {
                    "requirement_id": record["id"],
                    "version": record["version"],
                    "predicate_id": "feature",
                    "side": side,
                    "truth": True,
                    "support": "direct",
                    "question": None,
                    "explanation": "The supplied feature exists.",
                    "evidence": [{"block_id": block.id, "quote": block.text}],
                }
                for side, block in (("baseline", before), ("proposed", after))
            ],
            "conflicts": [],
            "questions": [],
        }
        return before, after, record, value

    def test_nonliteral_or_wrong_side_fact_reports_exact_path_without_source_text(self):
        before, after, record, value = self.facts_input()
        marker = "PRIVATE_SOURCE_MARKER_51479"
        for reference in (
            {"block_id": after.id, "quote": marker},
            {"block_id": before.id, "quote": before.text},
        ):
            with self.subTest(block_in_correct_side=reference["block_id"] == after.id):
                value["facts"][1]["evidence"] = [reference]
                with self.assertRaises(EvidenceValidationError) as caught:
                    validate_facts(
                        FactsOutput.model_validate(value),
                        [record],
                        {before.id: before},
                        {after.id: after},
                    )
                result = safe_rejection(caught.exception, "P3", repair_permitted=True)
                self.assertEqual(
                    result["validation_paths"],
                    [{"path": ["facts", 1, "evidence", 0], "type": "literal_reference"}],
                )
                self.assertIn("exact contiguous substring", result["repair_hint"])
                self.assertNotIn(marker, json.dumps(result))
                self.assertNotIn(before.text, json.dumps(result))

    def test_conflict_citation_error_has_its_own_path(self):
        before, after, record, value = self.facts_input()
        opposing = blocks("second-source", b"The feature is absent.")[0]
        value["conflicts"] = [
            {
                "requirement_ids": [record["id"]],
                "explanation": "Sources disagree about the feature.",
                "evidence": [
                    {"block_id": before.id, "quote": before.text},
                    {"block_id": opposing.id, "quote": "Not a literal citation."},
                ],
            }
        ]
        with self.assertRaises(EvidenceValidationError) as caught:
            validate_facts(
                FactsOutput.model_validate(value),
                [record],
                {before.id: before, opposing.id: opposing},
                {after.id: after},
            )
        result = safe_rejection(caught.exception, "P3", repair_permitted=True)
        self.assertEqual(result["validation_paths"][0]["path"], ["conflicts", 0, "evidence", 1])

    def test_control_removal_reference_paths_preserve_the_two_evidence_sides(self):
        _, _, record, _ = self.facts_input()
        before = blocks("baseline", b"Every read checks ownership.")[0]
        after = blocks("proposed", b"Remove ownership checks before reads.")[0]
        for field, block in (("baseline_evidence", before), ("delta_evidence", after)):
            with self.subTest(field=field):
                removal = {
                    "baseline_evidence": [{"block_id": before.id, "quote": before.text}],
                    "delta_evidence": [{"block_id": after.id, "quote": after.text}],
                    "explanation": "Synthetic two-side removal proof.",
                }
                removal[field] = [{"block_id": block.id, "quote": "PRIVATE_BAD_QUOTE_3791"}]
                value = JudgmentsOutput.model_validate(
                    {
                        "judgments": [
                            {
                                "requirement_id": record["id"],
                                "version": record["version"],
                                "clause_id": "c1",
                                "side": "proposed",
                                "verdict": "contradicted_by_spec",
                                "support": "direct",
                                "question": None,
                                "explanation": "Synthetic validator-path case.",
                                "evidence": [{"block_id": after.id, "quote": after.text}],
                                "control_removal": removal,
                            }
                        ],
                        "retrieval_queries": [],
                        "questions": [],
                    }
                )
                with self.assertRaises(EvidenceValidationError) as caught:
                    validate_judgments(
                        value,
                        [record],
                        {(record["id"], "proposed", "c1")},
                        {before.id: before},
                        {after.id: after},
                        removal_delta_ids={after.id},
                    )
                result = safe_rejection(caught.exception, "P5", repair_permitted=True)
                self.assertEqual(
                    result["validation_paths"][0]["path"],
                    ["judgments", 0, "control_removal", field, 0],
                )
                self.assertNotIn("PRIVATE_BAD_QUOTE_3791", json.dumps(result))

    def test_semantic_reference_locations_cannot_echo_untrusted_field_names(self):
        marker = "PRIVATE_FIELD_NAME_47952"
        result = safe_rejection(
            EvidenceValidationError(("facts", 1, marker, "evidence", 0)),
            "P3",
            repair_permitted=True,
        )
        self.assertEqual(
            result["validation_paths"][0]["path"], ["facts", 1, "<untrusted_field>", "evidence", 0]
        )
        self.assertNotIn(marker, json.dumps(result))

    def test_schema_required_paths_preserved_but_untrusted_names_and_keywords_hidden(self):
        private = "SYNTHETIC_PRIVATE_SCHEMA_KEY_538496"
        error = OutputSchemaError(
            [
                {
                    "location": ["facts", 1],
                    "keyword": "required",
                    "missing_fields": ["support", private],
                },
                {"location": [private, "0"], "keyword": private},
            ]
        )
        result = safe_rejection(error, "P3", repair_permitted=True)
        self.assertEqual(str(error), "output_schema_invalid")
        self.assertEqual(
            result["validation_paths"][0],
            {"path": ["facts", 1, "support"], "type": "missing"},
        )
        self.assertEqual(
            result["validation_paths"][-1],
            {"path": ["<untrusted_field>", "<untrusted_field>"], "type": "schema_validation"},
        )
        self.assertNotIn(private, json.dumps(result))

    def test_many_schema_errors_cannot_expand_repair_feedback_without_bound(self):
        errors = [
            {
                "location": ["facts", number],
                "keyword": "required",
                "missing_fields": ["support", "question"] * 10,
            }
            for number in range(20)
        ]
        result = safe_rejection(OutputSchemaError(errors), "P3", repair_permitted=False)
        self.assertEqual(len(result["validation_paths"]), 10)
        self.assertFalse(result["repair_permitted"])

    def test_known_validator_code_survives_without_private_input(self):
        private = "SYNTHETIC_PRIVATE_INPUT_794632"
        value = {
            "judgments": [
                {
                    "requirement_id": "USER-DIAG",
                    "version": "1.0.0",
                    "clause_id": "c1",
                    "side": "proposed",
                    "applicable_design": private,
                    "evidence": [
                        {"block_id": "one", "quote": "first"},
                        {"block_id": "one", "quote": "second"},
                    ],
                    "explanation": "Two quotations from one source.",
                    "support": "conflicting",
                    "verdict": "unknown",
                    "question": "Which statement is authoritative?",
                }
            ],
            "retrieval_queries": [],
            "questions": [],
        }
        with self.assertRaises(ValidationError) as caught:
            JudgmentsOutput.model_validate(value)
        result = safe_rejection(caught.exception, "P5", repair_permitted=True)
        self.assertEqual(
            result["reason_code"], "conflicting_judgment_requires_unknown_and_two_sources"
        )
        self.assertIn("DISTINCT block_ids", result["repair_hint"])
        self.assertNotIn(private, json.dumps(result))

    def test_untrusted_field_keys_and_exception_messages_are_not_exported(self):
        private = "SYNTHETIC_PRIVATE_KEY_128547"
        with self.assertRaises(ValidationError) as caught:
            JudgmentsOutput.model_validate(
                {"judgments": [], "retrieval_queries": [], "questions": [], private: "value"}
            )
        for exc in (caught.exception, ValueError(private)):
            result = safe_rejection(exc, "P5", repair_permitted=True)
            self.assertNotIn(private, json.dumps(result))
            self.assertEqual(result["reason_code"], "schema_or_output_contract_validation")
