"""Offline contract-bound metadata hydration; no model requests."""
import copy
import unittest

from spec_enricher import fact_wire
from spec_enricher.connections import ConnectionProfile
from spec_enricher.llm import OutputSchemaError
from spec_enricher.prompts import defaults
from spec_enricher.stage_contracts import FactsOutput, bind_schema, validate_facts


class FactWireTests(unittest.TestCase):
    def setUp(self):
        self.records = [{"id": "R1", "version": "2.3.4", "applicability": {"predicates": [{"id": "p1"}]}}]
        self.data = {"requirements": self.records, "baseline_blocks": [], "proposed_blocks": []}
        bound = bind_schema("P3", FactsOutput.model_json_schema(), self.data, literal_choices=False)
        self.schema, self.pins, self.metadata = fact_wire.prepare(bound, self.records)
        self.value = {"facts": [{"requirement_id": "R1", "predicate_id": "p1", "side": side,
            "truth": None, "support": "insufficient", "evidence": [], "explanation": "Not supplied.",
            "question": "Does this operation exist?"} for side in ("baseline", "proposed")],
            "conflicts": [], "questions": []}

    def test_explicit_wire_validates_before_pinned_hydration(self):
        original = copy.deepcopy(self.value)
        result = fact_wire.hydrate(self.value, self.schema, self.pins)
        validate_facts(result, self.records, {}, {})
        self.assertEqual([x.version for x in result.facts], ["2.3.4", "2.3.4"])
        self.assertEqual(self.value, original)
        self.assertNotIn("version", self.schema["$defs"]["PredicateFact"]["properties"])
        self.assertEqual(self.metadata["wire_contract"], fact_wire.WIRE_CONTRACT)

    def test_model_version_is_forbidden_even_if_correct(self):
        for version in ("2.3.4", "invented"):
            value = copy.deepcopy(self.value); value["facts"][0]["version"] = version
            with self.assertRaises(OutputSchemaError):
                fact_wire.hydrate(value, self.schema, self.pins)

    def test_unknown_duplicate_missing_and_invalid_fields_reject(self):
        for field, bad in (("requirement_id", "R2"), ("predicate_id", "p2"), ("side", "other"), ("truth", "true")):
            value = copy.deepcopy(self.value); value["facts"][0][field] = bad
            with self.assertRaises(ValueError):
                fact_wire.hydrate(value, self.schema, self.pins)
        value = copy.deepcopy(self.value); value["facts"][1] = value["facts"][0]
        with self.assertRaisesRegex(ValueError, "unknown_duplicate_or_unpinned_predicate"):
            fact_wire.hydrate(value, self.schema, self.pins)
        value = copy.deepcopy(self.value); value["facts"].pop()
        with self.assertRaises(OutputSchemaError):
            fact_wire.hydrate(value, self.schema, self.pins)

    def test_ambiguous_input_and_changed_pin_are_not_guessed(self):
        for extra in (self.records[0], self.records[0] | {"version": "9.0.0"}):
            with self.assertRaisesRegex(ValueError, "ambiguous_pinned_fact_metadata"):
                fact_wire.pins(self.records + [extra])
        changed = copy.deepcopy(self.records); changed[0]["version"] = "9.0.0"
        _, _, metadata = fact_wire.prepare(bind_schema("P3", FactsOutput.model_json_schema(), self.data, literal_choices=False), changed)
        self.assertNotEqual(metadata["pinned_metadata_sha256"], self.metadata["pinned_metadata_sha256"])

    def test_known_ids_in_wrong_combination_and_duplicate_input_predicate_reject(self):
        records = copy.deepcopy(self.records)
        records.append({"id": "R2", "version": "5.0.0", "applicability": {"predicates": [{"id": "p2"}]}})
        data = self.data | {"requirements": records}
        schema, pinned, _ = fact_wire.prepare(
            bind_schema("P3", FactsOutput.model_json_schema(), data, literal_choices=False), records
        )
        value = copy.deepcopy(self.value)
        value["facts"] += [fact | {"requirement_id": "R2", "predicate_id": "p2"} for fact in copy.deepcopy(value["facts"])]
        value["facts"][0]["predicate_id"] = "p2"
        with self.assertRaisesRegex(ValueError, "unknown_duplicate_or_unpinned_predicate"):
            fact_wire.hydrate(value, schema, pinned)
        records[0]["applicability"]["predicates"].append({"id": "p1"})
        with self.assertRaisesRegex(ValueError, "ambiguous_pinned_fact_metadata"):
            fact_wire.pins(records)

    def test_evidence_constraints_remain_mandatory(self):
        value = copy.deepcopy(self.value)
        value["facts"][0].update(truth=True, support="direct", question=None)
        with self.assertRaises(OutputSchemaError):
            fact_wire.hydrate(value, self.schema, self.pins)
        value["facts"][0]["evidence"] = [{"block_id": "invented", "quote": "fake"}]
        with self.assertRaises(ValueError):
            output = fact_wire.hydrate(value, self.schema, self.pins)
            validate_facts(output, self.records, {}, {})

    def test_custom_prompts_skills_review_and_other_adapters_unchanged(self):
        profile = ConnectionProfile(name="Test", adapter="lm_studio_native", endpoint="http://127.0.0.1:1234/api/v1", local_addresses=["127.0.0.1"], output_mode="prompt_json")
        args = dict(stage="P3", profile=profile, contract=FactsOutput, instruction=defaults()["P3"], prompt=None, schema=None, skill=None)
        self.assertTrue(fact_wire.enabled(**args))
        for change in ({"instruction": defaults()["P3"] | {"id": "custom", "revision": 1}},
                       {"instruction": defaults()["P3"] | {"template": "changed"}},
                       {"prompt": defaults()["P3"]}, {"skill": {}}, {"schema": {}}, {"stage": "P5"},
                       {"profile": profile.model_copy(update={"adapter": "lm_studio"})}):
            self.assertFalse(fact_wire.enabled(**(args | change)))


class NormalizedFactBoundaryTests(FactWireTests):
    def test_normalized_syntax_does_not_relax_unknown_ids_or_evidence(self):
        import json
        from spec_enricher.llm import output_object
        def normalize(value):
            text = json.dumps(value)
            offset = text.index('{', text.index('['))
            malformed = text[:offset] + text[offset + 1:]
            decoded, marker = output_object(malformed, "prompt_json", allow_missing_array_object_open=True)
            self.assertIn("char_offset=", marker)
            return fact_wire.hydrate(decoded, self.schema, self.pins)
        result = normalize(self.value)
        validate_facts(result, self.records, {}, {})
        bad = copy.deepcopy(self.value); bad["facts"][0]["requirement_id"] = "unknown"
        with self.assertRaises(ValueError):
            normalize(bad)
        bad = copy.deepcopy(self.value); bad["facts"][0].update(truth=True, support="direct", question=None, evidence=[{"block_id": "forged", "quote": "invented"}])
        with self.assertRaises(ValueError):
            output = normalize(bad)
            validate_facts(output, self.records, {}, {})
