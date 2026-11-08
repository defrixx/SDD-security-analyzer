import copy
import json
import unittest
from pathlib import Path

from spec_enricher.catalog import migration_preview, semantic_hash, validate


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full = json.loads(Path("catalog/requirements.full.json").read_text())
        cls.example = json.loads(Path("templates/requirements.example.json").read_text())

    def test_full_and_synthetic_example_structural_and_reference_checks(self):
        self.assertEqual(validate(self.full), [])
        self.assertEqual(validate(self.example), [])

    def test_duplicate_ids_unknown_operator_missing_force(self):
        value = copy.deepcopy(self.example)
        value["requirements"].append(copy.deepcopy(value["requirements"][0]))
        self.assertIn("duplicate_id", {x["code"] for x in validate(value)})
        value = copy.deepcopy(self.example)
        value["requirements"][0]["applicability"]["expression"] = {"exec": "nothing"}
        self.assertIn("schema_error", {x["code"] for x in validate(value)})
        value = copy.deepcopy(self.example)
        value["requirements"][0]["clause_normativity"] = {}
        self.assertTrue(validate(value))

    def test_dependency_closure_hash_and_cycles(self):
        value = copy.deepcopy(self.example)
        r = value["requirements"][0]
        r["dependencies"] = [
            {
                "requirement_id": r["id"],
                "version": r["version"],
                "target_semantic_sha256": semantic_hash(r),
                "source_sha256": "a" * 64,
                "originating_source_ref_ids": [r["source_refs"][0]["id"]],
                "activation": "explicit",
                "deduplication": "same target once",
            }
        ]
        self.assertIn("dependency_cycle", {x["code"] for x in validate(value)})
        r["dependencies"][0]["requirement_id"] = "missing.target"
        self.assertIn("missing_dependency", {x["code"] for x in validate(value)})
        r["dependencies"][0]["requirement_id"] = r["id"]
        r["dependencies"][0]["target_semantic_sha256"] = "0" * 64
        self.assertIn("stale_dependency", {x["code"] for x in validate(value)})

    def test_legacy_preview_never_guesses_force(self):
        preview = migration_preview(
            {"schema_version": "3.0", "requirements": [{"id": "old", "source_kind": "mandatory"}]}
        )
        self.assertFalse(preview["active"])
        self.assertEqual(preview["records"][0]["original"]["source_kind"], "mandatory")
        self.assertIn("clause_normativity", preview["records"][0]["unresolved_fields"])
        self.assertEqual(validate({"schema_version": "3.0"})[0]["code"], "unsupported_schema")

    def test_actual_legacy53_aliases_and_selected_schema3_remain_inactive(self):
        legacy = json.loads(Path("fixtures/migration/starter-1.1.json").read_text())
        original = copy.deepcopy(legacy)
        result = migration_preview(legacy)
        self.assertEqual(len(result["records"]), 53)
        self.assertFalse(result["active"])
        self.assertEqual(result["status"], "draft_only")
        current = {record["id"]: record for record in self.full["requirements"]}
        for row in result["records"]:
            self.assertTrue(row["current_targets"], row["source_id"])
            self.assertIn("clause_normativity", row["unresolved_fields"])
            for target in row["current_targets"]:
                self.assertEqual(
                    target["target_semantic_sha256"], semantic_hash(current[target["id"]])
                )
        self.assertEqual(legacy, original)
        wrapper = json.loads(Path("fixtures/migration/schema3-selected-records.json").read_text())
        # Extract only historical input bytes. Wrapper expected data stays here.
        selected = {"schema_version": "3.0", "requirements": wrapper["records"]}
        result = migration_preview(selected)
        self.assertFalse(result["active"])
        self.assertTrue(all(row["mapping_requires_semantic_review"] for row in result["records"]))
        rejected = migration_preview(
            {"schema_version": "2.0", "requirements": [{"id": "not-a-current-policy"}]}
        )
        self.assertTrue(rejected["rejected_source"])
        self.assertEqual(rejected["records"][0]["current_targets"], [])
        with self.assertRaises(ValueError):
            migration_preview({"schema_version": "1.1", "requirements": "not-records"})


if __name__ == "__main__":
    unittest.main()
