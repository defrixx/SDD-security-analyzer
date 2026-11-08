import json
import multiprocessing
import unittest
from unittest.mock import patch

from spec_enricher.connections import safe_schema
from spec_enricher.schema_worker import SchemaValidationUnavailable, validate_payload
from spec_enricher.workers import WorkerError, bounded


def nested_definition_check(schema):
    return safe_schema(schema)


class SchemaWorkerTests(unittest.TestCase):
    def test_untrusted_regex_definition_is_bounded_and_rejects_recursion(self):
        before = {p.pid for p in multiprocessing.active_children()}
        with self.assertRaisesRegex(ValueError, "invalid_schema"):
            safe_schema({"type": "string", "pattern": "(" * 1200 + "a" + ")" * 1200})
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
        schema = {"type": "string", "pattern": "^[a-z]{1,32}$"}
        self.assertEqual(safe_schema(schema), schema)
        self.assertEqual(bounded(nested_definition_check, schema, timeout=5), schema)
        self.assertEqual(validate_payload(schema, "valid"), [])
        self.assertTrue(validate_payload(schema, "INVALID"))

    def test_definition_worker_failure_is_not_reported_as_valid_or_invalid_instance(self):
        with patch(
            "spec_enricher.schema_worker.bounded",
            side_effect=WorkerError("worker time limit exceeded"),
        ):
            with self.assertRaises(SchemaValidationUnavailable):
                safe_schema({"type": "object"})

    def test_required_fields_report_typed_locations_without_input_values(self):
        private = "SYNTHETIC_PRIVATE_EXPLANATION_529417"
        issues = validate_payload(
            {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"support": {"type": "string"}},
                            "required": ["support"],
                        },
                    }
                },
            },
            {"facts": [{"support": "direct"}, {"explanation": private}]},
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["location"], ["facts", 1])
        self.assertEqual(issues[0]["missing_fields"], ["support"])
        self.assertNotIn(private, json.dumps(issues))

    def test_local_worker_failure_is_distinct_from_invalid_model_payload(self):
        with patch(
            "spec_enricher.schema_worker.bounded",
            side_effect=WorkerError("worker time limit exceeded"),
        ):
            with self.assertRaises(ValueError) as caught:
                validate_payload({"type": "object"}, {})
        self.assertEqual(type(caught.exception).__name__, "SchemaValidationUnavailable")
        self.assertEqual(str(caught.exception), "local_schema_validation_unavailable")

    def test_schema_external_ref_and_pathological_pattern_cannot_hang_parent(self):
        before = {p.pid for p in multiprocessing.active_children()}
        with self.assertRaises(ValueError):
            validate_payload({"$ref": "https://forbidden.invalid/schema"}, {})
        with self.assertRaises(ValueError):
            validate_payload({"type": "string", "pattern": "^(a+)+$"}, "a" * 10000 + "!")
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
        self.assertEqual(
            validate_payload({"type": "object", "additionalProperties": False}, {}), []
        )


if __name__ == "__main__":
    unittest.main()
