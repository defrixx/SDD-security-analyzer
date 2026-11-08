import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace

from jsonschema import Draft202012Validator, ValidationError
from pydantic import ValidationError as ContractError

from spec_enricher.catalog import canonical
from spec_enricher.effective_state import Operation, blocks, resolve
from spec_enricher.pipeline import PipelineRunner
from spec_enricher.stage_contracts import (
    DeltaOperation,
    FactsOutput,
    JudgmentsOutput,
    OperationsOutput,
    PredicateFact,
    bind_schema,
    declared_add_only,
    declared_operation_deferral,
    validate_facts,
)


class OperationsContractTests(unittest.TestCase):
    def test_explicit_operation_deferral_preserves_author_question_and_human_override(self):
        text = (
            "Неясно, заменяет ли это существующий API или добавляет новый endpoint. "
            "Просьба уточнить замысел, прежде чем строить proposed-state."
        )
        for pending in (
            text,
            "Unclear whether to add or replace this API. Please clarify intent before proposed-state.",
            "It is not yet decided whether to delete or replace the endpoint. Clarify the operation.",
            "Не определено: удалить или добавить обработчик. Уточните операцию.",
            "Decision: add. Later clarification: " + text,
        ):
            with self.subTest(pending=pending):
                self.assertTrue(declared_operation_deferral(pending))
        for inert in (
            "> " + text,
            "```text\n" + text + "\n```\nAdd an unrelated endpoint.",
            "~~~\n" + text + "\n~~~",
            "    " + text,
            '"' + text + '"',
            'Review note: "' + text + '"',
            "Example: `" + text + "`",
            "Add an endpoint. Security details are unclear; clarify the access policy.",
            "Add an endpoint; replace the test fixture. Please clarify the intent of the review.",
            text + " Решено: добавить отдельный endpoint.",
            text + " Decision: replace the existing endpoint.",
        ):
            with self.subTest(inert=inert):
                self.assertFalse(declared_operation_deferral(inert))

        runner = PipelineRunner.__new__(PipelineRunner)
        runner.state = {
            "P1": {
                "documents": [
                    {"id": "base", "role": "master_spec", "content": "An existing private API."},
                    {"id": "delta", "role": "delta", "content": text},
                ]
            }
        }
        runner.request = SimpleNamespace(operation_overrides=[])
        runner.manifest = {"connections": {"P2": {"content": {"context_budget": 32768}}}}
        delta = blocks("delta", text.encode())[0]
        operation = {
            "id": "test",
            "kind": "add",
            "delta_block_id": delta.id,
            "target_block_id": None,
            "target_hash": None,
            "target_quote": None,
            "explanation": "Synthetic resolver output.",
            "question": None,
        }

        def respond(stage, data, contract, validate):
            value = contract.model_validate({"operations": [operation], "questions": []})
            validate(value)
            return value

        runner.call = respond
        with self.assertRaisesRegex(ValueError, "explicit_operation_deferral_requires_question"):
            runner.P2()
        operation.update(kind="unresolved", question="Add or replace the existing API?")
        result = runner.P2()
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["reason"], "delta_ambiguity")
        self.assertIn(operation["question"], result["questions"])
        self.assertTrue(result["unresolved_operations"])
        self.assertEqual(result["operations"][0]["kind"], "unresolved")
        # An explicit human clarification is allowed even while the original bytes
        # retain their historical request for clarification.
        runner.request.operation_overrides = [
            DeltaOperation.model_validate(operation | {"kind": "add", "question": None})
        ]
        runner.call = lambda *args: self.fail("Human override must not invoke the model")
        result = runner.P2()
        self.assertFalse(result["incomplete"])
        self.assertEqual(result["operations"][0]["kind"], "add")

    def test_native_operation_binding_keeps_deferred_and_resolved_blocks_separate(self):
        data = {
            "delta_blocks": [
                {
                    "id": "pending",
                    "text": "Unclear: add or replace? Clarify intent before proposed-state.",
                },
                {"id": "addition", "text": "Add an independent endpoint."},
                {"id": "ordinary", "text": "Replace the old endpoint."},
            ],
            "baseline_blocks": [{"id": "base", "hash": "a" * 64}],
        }
        schema = bind_schema("P2", OperationsOutput.model_json_schema(), data)
        Draft202012Validator.check_schema(schema)
        operation_schema = {"$defs": schema["$defs"], "$ref": "#/$defs/DeltaOperation"}
        validator = Draft202012Validator(operation_schema)
        base = {
            "id": "test",
            "operation": "unresolved",
            "delta_block_id": "pending",
            "explanation": "The author defers choosing the operation.",
            "target_block_id": None,
            "target_hash": None,
            "target_quote": None,
            "question": "Which operation is intended?",
        }
        validator.validate(base)
        for block_id, kinds in (
            ("pending", {"unresolved"}),
            ("addition", {"add"}),
            ("ordinary", {"add", "replace", "delete", "unresolved"}),
        ):
            for kind in ("add", "replace", "delete", "unresolved"):
                value = base | {
                    "delta_block_id": block_id,
                    "operation": kind,
                    "question": base["question"] if kind == "unresolved" else None,
                }
                if kind in {"replace", "delete"}:
                    value.update(
                        target_block_id="base", target_hash="a" * 64, target_quote="Old text."
                    )
                self.assertEqual(validator.is_valid(value), kind in kinds, (block_id, kind))

    def test_fact_citations_keep_side_when_identical_quote_survives_partial_replace(self):
        quote = "There are no public links."
        before = blocks("same-snapshot", (quote + " No MCP is connected.\n").encode())
        delta = blocks("delta", b'Replace "No MCP is connected." with undecided MCP scope.\n')
        state = resolve(
            before,
            delta,
            [
                Operation(
                    "replace-mcp",
                    "replace",
                    delta[0].id,
                    before[0].id,
                    before[0].hash,
                    target_quote="No MCP is connected.",
                )
            ],
        )
        self.assertFalse(state.unresolved)
        retained = next(b for b in state.blocks if quote in b.text)
        self.assertEqual(retained.snapshot_id, before[0].snapshot_id)
        self.assertNotEqual(retained.id, before[0].id)
        records = [
            {
                "id": "USER-TEST",
                "version": "1.0.0",
                "applicability": {"predicates": [{"id": "scope"}]},
            }
        ]
        data = {"requirements": records}
        for side, items in (("baseline", before), ("proposed", state.blocks)):
            data[side + "_blocks"] = [asdict(b) | {"evidence_side": side} for b in items]
        schema = bind_schema("P3", FactsOutput.model_json_schema(), data, literal_choices=False)
        validator = Draft202012Validator(schema)

        def fact(side, block_id):
            return {
                "requirement_id": "USER-TEST",
                "version": "1.0.0",
                "predicate_id": "scope",
                "side": side,
                "truth": False,
                "support": "direct",
                "evidence": [{"block_id": block_id, "quote": quote}],
                "explanation": "The product explicitly has no public links.",
                "question": None,
            }

        original_fact = fact("baseline", before[0].id)
        retained_fact = fact("proposed", retained.id)
        value = {"facts": [original_fact, retained_fact], "conflicts": [], "questions": []}
        before_map = {b.id: b for b in before}
        after_map = {b.id: b for b in state.blocks}
        validator.validate(value)
        validate_facts(FactsOutput.model_validate(value), records, before_map, after_map)
        for baseline_fact, proposed_fact in (
            (fact("baseline", retained.id), retained_fact),
            (original_fact, fact("proposed", before[0].id)),
        ):
            invalid = value | {"facts": [baseline_fact, proposed_fact]}
            with self.assertRaises(ValidationError):
                validator.validate(invalid)
            with self.assertRaisesRegex(ValueError, "invented_or_out_of_scope_evidence"):
                validate_facts(FactsOutput.model_validate(invalid), records, before_map, after_map)

    def test_explicit_add_cannot_delete_history_and_mixed_intent_stays_unresolved(self):
        for text in (
            "Add public links. Existing session rules remain unchanged.",
            "Add confidential links. Token generation and expiry are not specified.",
            "Добавить публичные ссылки. Правило сессий сохраняется.",
        ):
            self.assertTrue(declared_add_only(text))
            schema = bind_schema(
                "P2",
                OperationsOutput.model_json_schema(),
                {
                    "delta_blocks": [{"id": "change", "text": text}],
                    "baseline_blocks": [{"id": "old", "hash": "a" * 64}],
                },
            )
            validator = Draft202012Validator(schema)
            operation = {
                "id": "one",
                "delta_block_id": "change",
                "operation": "add",
                "explanation": "The author explicitly adds a feature.",
                "target_block_id": None,
                "target_hash": None,
                "target_quote": None,
                "question": None,
            }
            validator.validate({"operations": [operation], "questions": []})
            for kind in ("replace", "delete"):
                invalid = operation | {
                    "operation": kind,
                    "target_block_id": "old",
                    "target_hash": "a" * 64,
                    "target_quote": "There were no public links.",
                }
                self.assertFalse(validator.is_valid({"operations": [invalid], "questions": []}))
            question = operation | {
                "operation": "unresolved",
                "question": "Which document boundary should contain the addition?",
            }
            self.assertFalse(validator.is_valid({"operations": [question], "questions": []}))
            self.assertFalse(
                validator.is_valid({"operations": [operation], "questions": [question["question"]]})
            )
        for mixed in (
            "Add or replace the endpoint; the author has not decided.",
            "Add a feature by replacing the old parser.",
            "Добавить функцию и заменить старую реализацию.",
            "A note about adding a feature, with no decided operation.",
            "Add a feature if its scope is approved.",
            "Add a feature? The operation is undecided.",
            "Add a feature. The author has not decided which operation is intended.",
            "Add a feature and modify the existing endpoint.",
            "Добавить функцию, если автор подтвердит намерение.",
        ):
            self.assertFalse(declared_add_only(mixed))
            schema = bind_schema(
                "P2",
                OperationsOutput.model_json_schema(),
                {"delta_blocks": [{"id": "change", "text": mixed}], "baseline_blocks": []},
            )
            question = {
                "id": "uncertain",
                "delta_block_id": "change",
                "operation": "unresolved",
                "explanation": "The author has not unconditionally selected an operation.",
                "target_block_id": None,
                "target_hash": None,
                "target_quote": None,
                "question": "Which operation does the author intend?",
            }
            Draft202012Validator(schema).validate({"operations": [question], "questions": []})

    def test_decision_aliases_follow_evidence_and_preserve_frozen_api_names(self):
        operation = {
            "id": "change",
            "delta_block_id": "delta",
            "explanation": "The document explicitly adds a capability.",
            "target_block_id": None,
            "target_hash": None,
            "target_quote": None,
            "question": None,
            "operation": "add",
        }
        fact = {
            "requirement_id": "USER-TEST",
            "version": "1.0.0",
            "predicate_id": "scope",
            "side": "proposed",
            "evidence": [],
            "explanation": "The future scope is undecided.",
            "support": "insufficient",
            "question": "Which protocol will be used?",
            "truth": None,
        }
        for model, value, aliases in (
            (DeltaOperation, operation, {"operation": "kind"}),
            (PredicateFact, fact, {"support": "evidence_state", "truth": "value"}),
        ):
            stored = model.model_validate(value).model_dump()
            historical = {aliases.get(k, k): v for k, v in value.items()}
            self.assertEqual(stored, historical)
            self.assertEqual(model.model_validate(value).model_dump(by_alias=True), historical)
            self.assertEqual(model.model_validate(historical).model_dump(), stored)
            for alias, old_name in aliases.items():
                with self.assertRaises(ContractError):
                    model.model_validate(value | {old_name: value[alias]})
            schema = model.model_json_schema()
            for body in (json.dumps(schema), canonical(schema)):
                props = list(json.loads(body)["properties"])
                for decision in aliases:
                    self.assertLess(props.index("explanation"), props.index(decision))
                    if "evidence" in props:
                        self.assertLess(props.index("evidence"), props.index(decision))

    def test_operation_question_cannot_accompany_a_guessed_resolved_change(self):
        operation = {
            "id": "change",
            "operation": "add",
            "delta_block_id": "delta",
            "target_block_id": None,
            "target_hash": None,
            "target_quote": None,
            "explanation": "Whether this replaces or adds an endpoint is undecided.",
            "question": "Does this replace the API or add a separate endpoint?",
        }
        schema = bind_schema(
            "P2",
            OperationsOutput.model_json_schema(),
            {"delta_blocks": [{"id": "delta"}], "baseline_blocks": []},
        )
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate({"operations": [operation], "questions": []})
        with self.assertRaises(ContractError):
            OperationsOutput.model_validate({"operations": [operation], "questions": []})
        with self.assertRaises(ContractError):
            OperationsOutput.model_validate(
                {
                    "operations": [operation | {"question": None}],
                    "questions": [operation["question"]],
                }
            )
        unresolved = {"operations": [operation | {"operation": "unresolved"}], "questions": []}
        Draft202012Validator(schema).validate(unresolved)
        self.assertEqual(
            OperationsOutput.model_validate(unresolved).operations[0].kind, "unresolved"
        )

    def test_literal_citation_choices_preserve_newlines_and_bind_quote_to_block(self):
        schema = bind_schema(
            "P5",
            JudgmentsOutput.model_json_schema(),
            {
                "requested_judgments": [["USER-TEST", "proposed", "c1"]],
                "requirements": [{"version": "1.0.0"}],
                "baseline_blocks": [],
                "proposed_blocks": [
                    {"id": "a", "text": "Session valid.\nOwner check required.\n"},
                    {"id": "b", "text": "Different source."},
                ],
            },
        )
        validator = Draft202012Validator(schema)
        judgment = {
            "requirement_id": "USER-TEST",
            "version": "1.0.0",
            "clause_id": "c1",
            "side": "proposed",
            "verdict": "satisfied_by_spec",
            "applicable_design": "An owner check is explicitly required.",
            "control_removal": None,
            "support": "direct",
            "evidence": [{"block_id": "a", "quote": "Owner check required."}],
            "explanation": "Exact source statement.",
            "question": None,
        }

        def value(citation):
            return {
                "judgments": [judgment | {"evidence": [citation]}],
                "questions": [],
                "retrieval_queries": [],
            }

        validator.validate(value(judgment["evidence"][0]))
        validator.validate(
            value({"block_id": "a", "quote": "Session valid.\nOwner check required.\n"})
        )
        for invalid in [
            {"block_id": "a", "quote": "Session valid. Owner check required."},
            {"block_id": "b", "quote": "Owner check required."},
            {"block_id": "a", "quote": "Invented positive evidence."},
        ]:
            with self.assertRaises(ValidationError):
                validator.validate(value(invalid))

    def test_active_clause_confidence_does_not_mix_violation_with_source_conflict(self):
        schema = bind_schema(
            "P5",
            JudgmentsOutput.model_json_schema(),
            {
                "requested_judgments": [["USER-TEST", "proposed", "c1"]],
                "requirements": [{"version": "1.0.0"}],
                "baseline_blocks": [{"id": "before", "text": "Control was enabled."}],
                "proposed_blocks": [
                    {"id": "a", "text": "The control is removed.\nControl enabled."},
                    {"id": "b", "text": "Control disabled."},
                ],
            },
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        judgment = {
            "requirement_id": "USER-TEST",
            "version": "1.0.0",
            "clause_id": "c1",
            "side": "proposed",
            "verdict": "contradicted_by_spec",
            "applicable_design": "The proposed document removes the control.",
            "control_removal": None,
            "support": "direct",
            "evidence": [{"block_id": "a", "quote": "The control is removed."}],
            "explanation": "The specification explicitly removes the required protection.",
            "question": None,
        }

        def output(item):
            return {"judgments": [item], "retrieval_queries": [], "questions": []}

        validator.validate(output(judgment))
        JudgmentsOutput.model_validate(output(judgment))
        for update in [
            {"support": "conflicting"},
            {"support": "insufficient"},
            {"evidence": []},
            {"verdict": "unknown", "question": None},
        ]:
            with self.assertRaises(ValidationError):
                validator.validate(output(judgment | update))
            with self.assertRaises(ContractError):
                JudgmentsOutput.model_validate(output(judgment | update))
        conflict = judgment | {
            "verdict": "unknown",
            "support": "conflicting",
            "evidence": [
                {"block_id": "a", "quote": "Control enabled."},
                {"block_id": "b", "quote": "Control disabled."},
            ],
            "question": "Which same-side source is authoritative?",
        }
        validator.validate(output(conflict))
        JudgmentsOutput.model_validate(output(conflict))
        with self.assertRaises(ValidationError):
            validator.validate(output(judgment | {"verdict": "not_assessed"}))
        conflict["evidence"][1]["block_id"] = "a"
        with self.assertRaises(ContractError):
            JudgmentsOutput.model_validate(output(conflict))

    def test_known_fact_schema_requires_consistent_evidence_state_and_citations(self):
        schema = bind_schema(
            "P3",
            FactsOutput.model_json_schema(),
            {
                "requirements": [
                    {
                        "id": "USER-TEST",
                        "version": "1.0.0",
                        "applicability": {"predicates": [{"id": "scope"}]},
                    }
                ],
                "baseline_blocks": [{"id": "base", "text": "Known feature."}],
                "proposed_blocks": [{"id": "after", "text": "Known feature."}],
            },
        )
        Draft202012Validator.check_schema(schema)

        def fact(side, block):
            return {
                "requirement_id": "USER-TEST",
                "version": "1.0.0",
                "predicate_id": "scope",
                "side": side,
                "truth": True,
                "support": "direct",
                "evidence": [{"block_id": block, "quote": "Known feature."}],
                "explanation": "The named operation exists.",
                "question": None,
            }

        value = {
            "facts": [fact("baseline", "base"), fact("proposed", "after")],
            "conflicts": [],
            "questions": [],
        }
        validator = Draft202012Validator(schema)
        validator.validate(value)
        for invalid in [
            {"evidence": []},
            {"support": "conflicting"},
            {"support": "insufficient"},
            {"truth": False, "support": "inferred"},
        ]:
            candidate = value | {"facts": [value["facts"][0] | invalid, value["facts"][1]]}
            with self.assertRaises(ValidationError):
                validator.validate(candidate)
        validator.validate(
            value
            | {
                "facts": [
                    value["facts"][0]
                    | {
                        "truth": None,
                        "support": "conflicting",
                        "question": "Which source is authoritative?",
                    },
                    value["facts"][1],
                ]
            }
        )

    def test_side_specific_citations_reject_deleted_baseline_ids_in_proposed(self):
        schema = bind_schema(
            "P5",
            JudgmentsOutput.model_json_schema(),
            {
                "requested_judgments": [["USER-TEST", "proposed", "c1"]],
                "requirements": [{"version": "1.0.0"}],
                "baseline_blocks": [{"id": "deleted", "text": "Old text."}],
                "proposed_blocks": [{"id": "preserved-span", "text": "Original span."}],
            },
        )
        Draft202012Validator.check_schema(schema)
        value = {
            "judgments": [
                {
                    "requirement_id": "USER-TEST",
                    "version": "1.0.0",
                    "clause_id": "c1",
                    "side": "proposed",
                    "verdict": "not_addressed",
                    "applicable_design": "The supplied proposed span does not specify this design.",
                    "control_removal": None,
                    "support": "insufficient",
                    "evidence": [{"block_id": "deleted", "quote": "Old text."}],
                    "explanation": "Known scope, missing design.",
                    "question": None,
                }
            ],
            "retrieval_queries": [],
            "questions": [],
        }
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate(value)
        value["judgments"][0]["evidence"] = []
        Draft202012Validator(schema).validate(value)
        value["judgments"][0]["evidence"] = [
            {"block_id": "preserved-span", "quote": "Original span."}
        ]
        Draft202012Validator(schema).validate(value)

    def test_undecided_scope_cannot_be_a_known_false_with_an_unresolved_question(self):
        fact = {
            "requirement_id": "USER-TEST",
            "version": "1.0.0",
            "predicate_id": "scope",
            "side": "proposed",
            "truth": False,
            "support": "direct",
            "evidence": [{"block_id": "delta", "quote": "Protocol scope is undecided."}],
            "explanation": "The protocol use is unknown.",
            "question": "Which protocol will the integration use?",
        }
        with self.assertRaises(ContractError):
            PredicateFact.model_validate(fact)
        self.assertIsNone(PredicateFact.model_validate(fact | {"truth": None}).value)
        with self.assertRaises(ContractError):
            PredicateFact.model_validate(fact | {"truth": None, "question": None})

    def test_empty_and_inconsistent_target_output_is_rejected_before_resolver(self):
        schema = bind_schema(
            "P2",
            OperationsOutput.model_json_schema(),
            {
                "delta_blocks": [{"id": "delta"}],
                "baseline_blocks": [{"id": "baseline", "hash": "a" * 64}],
            },
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        good = {
            "id": "local-model-id",
            "operation": "replace",
            "delta_block_id": "delta",
            "target_block_id": "baseline",
            "target_hash": "a" * 64,
            "target_quote": "Baseline sentence.",
            "explanation": "Explicit textual replacement.",
            "question": None,
        }
        validator.validate({"operations": [good], "questions": []})
        for invalid in [
            good | {"operation": "add"},
            good | {"target_hash": None},
            good | {"operation": "unresolved", "question": None},
        ]:
            with self.assertRaises(ContractError):
                DeltaOperation.model_validate(invalid)
            with self.assertRaises(ValidationError):
                validator.validate({"operations": [invalid], "questions": []})
        with self.assertRaises(ValidationError):
            validator.validate({"operations": [], "questions": []})
        validator.validate(
            {
                "operations": [
                    good
                    | {
                        "operation": "add",
                        "target_block_id": None,
                        "target_hash": None,
                        "target_quote": None,
                    }
                ],
                "questions": [],
            }
        )
        validator.validate(
            {
                "operations": [
                    good
                    | {
                        "operation": "unresolved",
                        "target_block_id": None,
                        "target_hash": None,
                        "question": "Does this add or replace an operation?",
                    }
                ],
                "questions": [],
            }
        )
