"""New provider contracts stay strict while frozen historical DTOs remain readable."""

import copy
import json
import unittest

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from spec_enricher.catalog import canonical
from spec_enricher.connections import ConnectionProfile
from spec_enricher.effective_state import blocks
from spec_enricher.llm import LLMAdapter
from spec_enricher.pipeline_config import rendered
from spec_enricher.prompts import POLICY, defaults
from spec_enricher.schema_worker import validate_payload
from spec_enricher.secrets import SecretStore
from spec_enricher.stage_contracts import (
    Citation,
    ClauseJudgment,
    CritiqueOutput,
    FactsOutput,
    JudgmentsOutput,
    bind_schema,
    evidence,
)


class JudgmentContractTests(unittest.TestCase):
    def setUp(self):
        self.old = {
            "requirement_id": "USER-CLASS",
            "version": "1.0.0",
            "clause_id": "c1",
            "side": "baseline",
            "assessment": "satisfied_by_spec",
            "evidence_state": "direct",
            "evidence": [{"block_id": "before", "quote": "Personal fields are excluded."}],
            "explanation": "The exclusion is explicit.",
            "question": None,
            "control_removal": None,
        }
        self.schema = bind_schema(
            "P5",
            JudgmentsOutput.model_json_schema(),
            {
                "requirements": [{"id": "USER-CLASS", "version": "1.0.0"}],
                "requested_judgments": [["USER-CLASS", "baseline", "c1"]],
                "baseline_blocks": [{"id": "before", "text": "Personal fields are excluded."}],
                "proposed_blocks": [{"id": "after", "text": "Personal fields are excluded."}],
                "removal_delta_block_ids": [],
            },
        )

    def envelope(self, judgment):
        return {"judgments": [judgment], "questions": [], "retrieval_queries": []}

    def test_history_is_readable_but_new_provider_requires_bounded_factual_design(self):
        historical = ClauseJudgment.model_validate(self.old)
        self.assertEqual(historical.assessment, "satisfied_by_spec")
        self.assertTrue(validate_payload(self.schema, self.envelope(self.old)))
        new = self.provider_value()
        self.assertFalse(validate_payload(self.schema, self.envelope(new)))
        self.assertEqual(ClauseJudgment.model_validate(new).assessment, historical.assessment)
        for bad in (None, "", "x" * 601):
            with self.subTest(summary=bad if bad is None else len(bad)):
                self.assertTrue(
                    validate_payload(self.schema, self.envelope(new | {"applicable_design": bad}))
                )
        # The wire grammar must not cut a valid shared-contract comparison at
        # the old provider-only 800-character boundary before emitting a verdict.
        comparison = (
            "The supplied design explicitly excludes personal fields. Its retained "
            "guarantee applies to this side; another control does not change that exclusion. "
        ) * 8
        self.assertGreater(len(comparison), 800)
        complete = new | {"explanation": comparison}
        self.assertEqual(ClauseJudgment.model_validate(complete).explanation, comparison)
        self.assertFalse(validate_payload(self.schema, self.envelope(complete)))
        bound = ClauseJudgment.model_json_schema()["properties"]["explanation"]["maxLength"]
        oversized = new | {"explanation": "x" * (bound + 1)}
        self.assertTrue(validate_payload(self.schema, self.envelope(oversized)))
        with self.assertRaises(ValidationError):
            ClauseJudgment.model_validate(oversized)

    def test_wire_schema_places_factual_design_before_verdict_and_keeps_evidence_guards(self):
        # HTTP JSON retains schema declaration order; the canonical exact-payload
        # preview sorts keys. Both representations must put design before verdict.
        for body in (json.dumps(self.schema), canonical(self.schema)):
            wire = json.loads(body)
            props = list(wire["$defs"]["ClauseJudgment"]["properties"])
            for field in ("applicable_design", "evidence", "explanation"):
                self.assertLess(props.index(field), props.index("support"))
                self.assertLess(props.index(field), props.index("verdict"))
        # The actual HTTP schema retains insertion order. Canonical previews
        # sort object keys and cannot represent native generation order.
        props = list(self.schema["$defs"]["ClauseJudgment"]["properties"])
        self.assertLess(props.index("verdict"), props.index("support"))
        value = self.provider_value()
        invalid = copy.deepcopy(value)
        invalid["evidence"] = [{"block_id": "after", "quote": "Personal fields are excluded."}]
        self.assertTrue(validate_payload(self.schema, self.envelope(invalid)))
        invalid = value | {"support": "conflicting"}
        self.assertTrue(validate_payload(self.schema, self.envelope(invalid)))

    def provider_value(self):
        return {k: v for k, v in self.old.items() if k not in {"assessment", "evidence_state"}} | {
            "verdict": self.old["assessment"],
            "support": self.old["evidence_state"],
            "applicable_design": "The design explicitly excludes personal fields.",
        }

    def test_provider_alias_preserves_api_and_rejects_conflicting_duplicate_decisions(self):
        value = self.provider_value()
        typed = ClauseJudgment.model_validate(value)
        self.assertEqual(typed.model_dump()["assessment"], self.old["assessment"])
        self.assertEqual(typed.model_dump(by_alias=True)["assessment"], self.old["assessment"])
        self.assertNotIn("verdict", typed.model_dump())
        duplicate = value | {"assessment": "contradicted_by_spec"}
        self.assertTrue(validate_payload(self.schema, self.envelope(duplicate)))
        with self.assertRaises(ValidationError):
            ClauseJudgment.model_validate(duplicate)
        with self.assertRaises(ValidationError):
            ClauseJudgment.model_validate(value | {"evidence_state": "conflicting"})

    def test_one_block_cannot_supply_two_distinct_conflicting_sources(self):
        text = "Personal fields are excluded.\nNo MCP is connected."
        data = {
            "requirements": [{"id": "USER-CLASS", "version": "1.0.0"}],
            "requested_judgments": [["USER-CLASS", "baseline", "c1"]],
            "baseline_blocks": [{"id": "before", "text": text}],
            "proposed_blocks": [{"id": "after", "text": text}],
        }
        value = self.provider_value() | {
            "support": "conflicting",
            "verdict": "unknown",
            "question": "Which source is authoritative?",
            "evidence": [
                {"block_id": "before", "quote": "Personal fields are excluded."},
                {"block_id": "before", "quote": "No MCP is connected."},
            ],
        }
        with self.assertRaisesRegex(ValidationError, "unknown_and_two_sources"):
            ClauseJudgment.model_validate(value)
        schema = bind_schema("P5", JudgmentsOutput.model_json_schema(), data)
        self.assertTrue(validate_payload(schema, self.envelope(value)))
        # Missing design can still be reported honestly, without fabricating a
        # second source or converting uncertainty into satisfaction.
        for verdict in ("not_addressed", "unknown"):
            allowed = value | {"support": "insufficient", "verdict": verdict}
            self.assertFalse(validate_payload(schema, self.envelope(allowed)))
            ClauseJudgment.model_validate(allowed)

    def test_two_distinct_blocks_keep_legitimate_conflict_unknown(self):
        data = {
            "requirements": [{"id": "USER-CLASS", "version": "1.0.0"}],
            "requested_judgments": [["USER-CLASS", "baseline", "c1"]],
            "baseline_blocks": [
                {"id": "before", "text": "Personal fields are excluded."},
                {"id": "other", "text": "Personal fields are logged."},
            ],
            "proposed_blocks": [],
        }
        value = self.provider_value() | {
            "support": "conflicting",
            "verdict": "unknown",
            "question": "Which source is authoritative?",
            "evidence": [
                {"block_id": "before", "quote": "Personal fields are excluded."},
                {"block_id": "other", "quote": "Personal fields are logged."},
            ],
        }
        schema = bind_schema("P5", JudgmentsOutput.model_json_schema(), data)
        self.assertFalse(validate_payload(schema, self.envelope(value)))
        self.assertEqual(ClauseJudgment.model_validate(value).assessment, "unknown")

    def test_conflict_generation_rejects_duplicate_sources_even_when_other_blocks_exist(self):
        # Seven blocks exercise unequal partitions and both orders of every
        # pair. Different quotes from one block remain the same source. Neither
        # provider mode nor P8 may bypass the same runtime requirement.
        source_blocks = [
            {"id": f"block-{index}", "text": f"Claim {index}.\nOther claim {index}."}
            for index in range(7)
        ]
        for stage, contract in (("P5", JudgmentsOutput), ("P8", CritiqueOutput)):
            for literal_choices in (True, False):
                with self.subTest(stage=stage, literal_choices=literal_choices):
                    data = {
                        "requirements": [{"id": "USER-CLASS", "version": "1.0.0"}],
                        "catalog": [{"id": "USER-CLASS", "version": "1.0.0"}],
                        "requested_judgments": [["USER-CLASS", "proposed", "c1"]],
                        "baseline_blocks": [],
                        "proposed_blocks": source_blocks,
                        "candidate_blocks": source_blocks,
                        "patches": [],
                    }
                    schema = bind_schema(
                        stage, contract.model_json_schema(), data, literal_choices=literal_choices
                    )
                    validator = Draft202012Validator(schema)
                    for first in source_blocks:
                        for second in source_blocks:
                            value = self.provider_value() | {
                                "side": "proposed",
                                "support": "conflicting",
                                "verdict": "unknown",
                                "question": "Which statement governs?",
                                "evidence": [
                                    {
                                        "block_id": first["id"],
                                        "quote": first["text"].splitlines()[0],
                                    },
                                    {
                                        "block_id": second["id"],
                                        "quote": second["text"].splitlines()[1],
                                    },
                                ],
                            }
                            envelope = (
                                self.envelope(value)
                                if stage == "P5"
                                else {"issues": [], "reassessment": [value], "questions": []}
                            )
                            self.assertEqual(
                                validator.is_valid(envelope), first["id"] != second["id"]
                            )
                    # A real quote paired with the wrong source is still
                    # rejected by native literal choices or runtime validation.
                    bad = value | {
                        "evidence": [
                            {"block_id": "block-0", "quote": "Claim 1."},
                            {"block_id": "block-1", "quote": "Claim 1."},
                        ]
                    }
                    if literal_choices:
                        envelope = (
                            self.envelope(bad)
                            if stage == "P5"
                            else {"issues": [], "reassessment": [bad], "questions": []}
                        )
                        self.assertFalse(validator.is_valid(envelope))

    def test_architecture_conflict_also_requires_two_distinct_baseline_blocks(self):
        data = {
            "requirements": [],
            "baseline_blocks": [
                {"id": "first", "text": "Private documents.\nPersonal data."},
                {"id": "second", "text": "Public documents."},
            ],
            "proposed_blocks": [],
        }
        schema = bind_schema("P3", FactsOutput.model_json_schema(), data)
        validator = Draft202012Validator(schema)
        value = {
            "facts": [],
            "questions": [],
            "conflicts": [
                {
                    "requirement_ids": ["USER-CLASS"],
                    "explanation": "The documentation disagrees.",
                    "evidence": [
                        {"block_id": "first", "quote": "Private documents."},
                        {"block_id": "first", "quote": "Personal data."},
                    ],
                }
            ],
        }
        self.assertFalse(validator.is_valid(value))
        value["conflicts"][0]["evidence"][1] = {"block_id": "second", "quote": "Public documents."}
        self.assertTrue(validator.is_valid(value))
        data["baseline_blocks"].pop()
        one_source = bind_schema("P3", FactsOutput.model_json_schema(), data)
        self.assertFalse(Draft202012Validator(one_source).is_valid(value))

    def test_shared_literal_menus_preserve_quotes_and_the_explicit_payload_budget(self):
        # Source count alone cannot guarantee that a particular context budget
        # fits. The larger input remains available, but must require an explicit
        # larger budget instead of silently truncating quotes or raising limits.
        source_blocks = [
            {
                "id": f"source-{index:02d}",
                "text": f"Policy fragment {index}: "
                + "Documented ownership checks and audit event fields. " * 10,
            }
            for index in range(10)
        ]
        data = {
            "requirements": [{"id": "USER-CLASS", "version": "1.0.0"}],
            "requested_judgments": [
                ["USER-CLASS", "baseline", "c1"],
                ["USER-CLASS", "proposed", "c1"],
            ],
            "baseline_blocks": source_blocks[:6],
            "proposed_blocks": source_blocks,
        }
        schema = bind_schema("P5", JudgmentsOutput.model_json_schema(), data)
        profile = ConnectionProfile(
            name="Synthetic bounded native profile",
            endpoint="http://127.0.0.1:1234/v1",
            local_addresses=["127.0.0.1"],
            model="test-only",
            max_output_tokens=4096,
            context_budget=65536,
        )
        messages = [
            {
                "role": "system",
                "content": POLICY + "\n\n" + rendered(defaults()["P5"], "P5", schema),
            },
            {
                "role": "user",
                "content": canonical({"trust": "untrusted_data_only", "data": data}).decode(),
            },
        ]
        payload = LLMAdapter("test-only", profile, SecretStore()).payload(messages, schema)
        self.assertLessEqual(
            len(json.dumps(payload, ensure_ascii=False).encode()) + profile.max_output_tokens,
            profile.context_budget,
        )
        for block in source_blocks:
            block["text"] += "Documented ownership checks and audit event fields. " * 8
        larger = bind_schema("P5", JudgmentsOutput.model_json_schema(), data)
        messages[0]["content"] = POLICY + "\n\n" + rendered(defaults()["P5"], "P5", larger)
        messages[1]["content"] = canonical({"trust": "untrusted_data_only", "data": data}).decode()
        larger_payload = LLMAdapter("test-only", profile, SecretStore()).payload(messages, larger)
        self.assertGreater(
            len(json.dumps(larger_payload, ensure_ascii=False).encode())
            + profile.max_output_tokens,
            profile.context_budget,
        )
        judgments = [
            self.provider_value()
            | {
                "side": side,
                "evidence": [
                    {"block_id": source_blocks[0]["id"], "quote": source_blocks[0]["text"]}
                ],
            }
            for side in ("baseline", "proposed")
        ]
        self.assertTrue(
            Draft202012Validator(larger).is_valid(
                {"judgments": judgments, "questions": [], "retrieval_queries": []}
            )
        )

    def test_prompt_json_omits_literal_menu_but_keeps_side_and_exact_quote_checks(self):
        data = {
            "requirements": [{"id": "USER-CLASS", "version": "1.0.0"}],
            "requested_judgments": [["USER-CLASS", "baseline", "c1"]],
            "baseline_blocks": [{"id": "before", "text": "Personal fields are excluded."}],
            "proposed_blocks": [{"id": "after", "text": "Personal fields are excluded."}],
            "removal_delta_block_ids": [],
        }
        compact = bind_schema(
            "P5", JudgmentsOutput.model_json_schema(), data, literal_choices=False
        )
        self.assertLess(len(canonical(compact)), len(canonical(self.schema)))
        value = self.provider_value()
        self.assertFalse(validate_payload(compact, self.envelope(value)))
        wrong_side = value | {"evidence": [{"block_id": "after", "quote": "excluded"}]}
        self.assertTrue(validate_payload(compact, self.envelope(wrong_side)))
        block = blocks("source", b"Personal fields are excluded.")[0]
        allowed = {block.id: block}
        self.assertEqual(
            evidence([Citation(block_id=block.id, quote="fields are excluded")], allowed)[0][
                "quote"
            ],
            "fields are excluded",
        )
        for quote, identifier in (("fields are collected", block.id), ("excluded", "other-side")):
            with self.assertRaisesRegex(ValueError, "invented_or_out_of_scope_evidence"):
                evidence([Citation(block_id=identifier, quote=quote)], allowed)


class NativeOptionalDesignTests(JudgmentContractTests):
    """The optional field never relaxes decisions or evidence contracts."""

    def test_native_missing_summary_stays_absent_not_invented(self):
        from spec_enricher import judgment_wire
        native, metadata = judgment_wire.prepare(self.schema)
        value = self.provider_value()
        del value["applicable_design"]
        original = copy.deepcopy(value)
        output = judgment_wire.decode(self.envelope(value), native)
        self.assertIsNone(output.judgments[0].applicable_design)
        self.assertEqual(output.judgments[0].assessment, "satisfied_by_spec")
        self.assertEqual(value, original)
        self.assertEqual(metadata["missing_presentation_handling"], "absent_to_null_without_inference")
        self.assertTrue(validate_payload(self.schema, self.envelope(value)))
        expected = copy.deepcopy(self.schema)
        expected["$defs"]["ClauseJudgment"]["required"].remove("applicable_design")
        expected["title"] = judgment_wire.WIRE_CONTRACT
        self.assertEqual(native, expected)

    def test_native_provided_summary_keeps_nonempty_string_bound(self):
        from spec_enricher import judgment_wire
        native, _ = judgment_wire.prepare(self.schema)
        for bad in (None, "", "x" * 601, 42):
            with self.assertRaises(ValueError):
                judgment_wire.decode(self.envelope(self.provider_value() | {"applicable_design": bad}), native)
        self.assertEqual(judgment_wire.decode(self.envelope(self.provider_value()), native).judgments[0].applicable_design,
                         self.provider_value()["applicable_design"])

    def test_native_decision_identifiers_evidence_and_removal_remain_required(self):
        from spec_enricher import judgment_wire
        native, _ = judgment_wire.prepare(self.schema)
        value = self.provider_value(); del value["applicable_design"]
        for field in ("requirement_id", "version", "clause_id", "side", "verdict", "support", "evidence", "explanation", "question", "control_removal"):
            invalid = copy.deepcopy(value); del invalid[field]
            with self.assertRaises(ValueError, msg=field):
                judgment_wire.decode(self.envelope(invalid), native)
        for change in ({"requirement_id": "unknown"}, {"version": "untrusted"}, {"clause_id": "wrong"},
                       {"side": "proposed"}, {"evidence": []},
                       {"evidence": [{"block_id": "unknown", "quote": "invented"}]},
                       {"support": "conflicting"}, {"control_removal": {}},
                       {"verdict": "unknown", "question": None}):
            with self.assertRaises(ValueError, msg=str(list(change))):
                judgment_wire.decode(self.envelope(value | change), native)

    def test_native_scope_is_exact_builtin_p5_only(self):
        from spec_enricher import judgment_wire
        profile = ConnectionProfile(name="Synthetic", adapter="lm_studio_native", endpoint="http://127.0.0.1:1234/api/v1", local_addresses=["127.0.0.1"], output_mode="prompt_json")
        args = dict(stage="P5", profile=profile, contract=JudgmentsOutput, instruction=defaults()["P5"], prompt=None, schema=None, skill=None)
        self.assertTrue(judgment_wire.enabled(**args))
        adapted = judgment_wire.instruction_for(defaults()["P5"])
        self.assertNotIn("First state applicable_design:", adapted["template"])
        self.assertIn("Optionally state applicable_design:", adapted["template"])
        self.assertIn("First state applicable_design:", defaults()["P5"]["template"])
        for change in ({"stage": "P8"}, {"instruction": defaults()["P5"] | {"id": "custom", "revision": 1}},
                       {"instruction": defaults()["P5"] | {"template": "custom"}}, {"prompt": defaults()["P5"]},
                       {"schema": {}}, {"skill": {}}, {"profile": profile.model_copy(update={"adapter": "lm_studio"})}):
            self.assertFalse(judgment_wire.enabled(**(args | change)))
