"""Exact, side-scoped evidence pairs; no new requirement IDs or force changes."""

import copy
import unittest

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from spec_enricher.effective_state import blocks
from spec_enricher.stage_contracts import JudgmentsOutput, bind_schema, validate_judgments


class ControlRemovalTests(unittest.TestCase):
    def test_both_proof_sides_and_actual_delta_identity_are_required(self):
        base = blocks("master", b"Secrets excluded from logs.\n")[0]
        delta = blocks("change", b"Log the access token.\n")[0]
        retained = blocks("retained", b"Other source statement.\n")[0]
        records = [{"id": "USER-LOG", "version": "1.0.0"}]
        expected = {("USER-LOG", "proposed", "c1")}
        before, after = {base.id: base}, {b.id: b for b in (delta, retained)}
        value = {
            "judgments": [
                {
                    "requirement_id": "USER-LOG",
                    "version": "1.0.0",
                    "clause_id": "c1",
                    "side": "proposed",
                    "verdict": "contradicted_by_spec",
                    "applicable_design": "The proposed design logs the access token.",
                    "support": "direct",
                    "evidence": [{"block_id": delta.id, "quote": delta.text}],
                    "explanation": "An explicit secret-logging contradiction.",
                    "question": None,
                    "control_removal": {
                        "baseline_evidence": [{"block_id": base.id, "quote": base.text}],
                        "delta_evidence": [{"block_id": delta.id, "quote": delta.text}],
                        "explanation": "The previously described exclusion is explicitly removed.",
                    },
                }
            ],
            "questions": [],
            "retrieval_queries": [],
        }
        output = JudgmentsOutput.model_validate(value)
        validate_judgments(output, records, expected, before, after, removal_delta_ids={delta.id})
        schema = bind_schema(
            "P5",
            JudgmentsOutput.model_json_schema(),
            {
                "requested_judgments": [list(k) for k in expected],
                "requirements": records,
                "baseline_blocks": [{"id": base.id, "text": base.text}],
                "proposed_blocks": [{"id": b.id, "text": b.text} for b in (delta, retained)],
                "delta_block_ids": [delta.id],
                "removal_delta_block_ids": [delta.id],
            },
        )
        Draft202012Validator(schema).validate(value)
        with self.assertRaises(ValueError):
            validate_judgments(output, records, expected, before, after)
        for side, bad in [
            ("baseline_evidence", {"block_id": delta.id, "quote": delta.text}),
            ("delta_evidence", {"block_id": retained.id, "quote": retained.text}),
            ("delta_evidence", {"block_id": delta.id, "quote": "Invented removal."}),
        ]:
            changed = copy.deepcopy(value)
            changed["judgments"][0]["control_removal"][side] = [bad]
            with self.assertRaises(ValueError):
                validate_judgments(
                    JudgmentsOutput.model_validate(changed),
                    records,
                    expected,
                    before,
                    after,
                    removal_delta_ids={delta.id},
                )
            self.assertFalse(Draft202012Validator(schema).is_valid(changed))
        for update in [
            {"side": "baseline"},
            {"verdict": "satisfied_by_spec"},
            {"verdict": "unknown", "question": "What changed?"},
        ]:
            changed = value | {"judgments": [value["judgments"][0] | update]}
            with self.assertRaises(ValidationError):
                JudgmentsOutput.model_validate(changed)
