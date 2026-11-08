import copy
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from spec_enricher.api import create_app
from spec_enricher.artifacts import ArtifactStore
from spec_enricher.catalog import digest, semantic_hash, validate
from spec_enricher.catalog_management import coverage, domain_subset
from spec_enricher.runs import RunRepository
from spec_enricher.storage import Store

ORIGIN = "http://127.0.0.1:8080"


class CatalogManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.client = TestClient(create_app(self.root, ORIGIN), base_url=ORIGIN)
        self.client.__enter__()
        login = self.client.post(
            "/api/v1/session/bootstrap",
            json={"token": (self.root / "bootstrap.token").read_text()},
            headers={"Origin": ORIGIN},
        ).json()
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        self.pid, self.other = [
            self.client.post("/api/v1/projects", json={"name": name}, headers=self.headers).json()[
                "id"
            ]
            for name in ["A", "B"]
        ]
        self.value = json.loads(Path("templates/requirements.example.json").read_text())

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def post(self, path, body):
        return self.client.post(
            f"/api/v1/projects/{self.pid}/{path}", json=body, headers=self.headers
        )

    def stage(self, value):
        response = self.client.post(
            f"/api/v1/projects/{self.pid}/catalog-imports/preview",
            content=json.dumps(value),
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def bind(self, value):
        staged = self.stage(value)
        self.assertEqual(staged["issues"], [], staged)
        result = self.post(
            f"catalog-imports/{staged['id']}/commit",
            {"confirmed": True, "expected_sha256": staged["sha256"]},
        )
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["id"]

    def test_coverage_and_domain_selection_preserve_records_and_dependency_closure(self):
        full = json.loads(Path("catalog/requirements.full.json").read_text())
        data = coverage(full)
        self.assertEqual(data["active_records"], 820)
        self.assertEqual(len(data["declared_sources"]), 26)
        self.assertGreater(sum(item["context_clauses"] for item in data["source_coverage"]), 0)
        self.assertGreater(
            sum(
                item["nature_counts"].get("review_obligation", 0)
                for item in data["source_coverage"]
            ),
            0,
        )
        domain = full["requirements"][0]["domain"]
        subset, additions = domain_subset(full, [domain])
        self.assertEqual(validate(subset), [])
        originals = {item["id"]: item for item in full["requirements"]}
        for record in subset["requirements"]:
            self.assertEqual(record, originals[record["id"]])
        self.assertEqual(
            set(additions),
            {item["id"] for item in subset["requirements"] if item["domain"] != domain},
        )
        self.assertEqual(subset["origin"]["subset_of"]["sha256"], digest(full))
        with self.assertRaises(ValueError):
            domain_subset(full, ["not-a-domain"])
        result = self.post("builtin-catalog/preview", {"domains": [domain]}).json()
        self.assertEqual(result["issues"], [], result)
        self.assertEqual(self.client.get(f"/api/v1/projects/{self.pid}/catalogs").json(), [])

    def test_split_clause_requires_exact_mapping_and_cannot_bypass_binding_guard(self):
        old_id = self.bind(self.value)
        changed = copy.deepcopy(self.value)
        changed["catalog_version"] = "9.0.0"
        record = changed["requirements"][0]
        record["version"] = "2.0.0"
        old_cid = record["clauses"][0]["id"]
        new_cid = old_cid + "-split"

        # Replace only exact AST identifier values, retaining typed logic/guards.
        def replace(value):
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {
                    new_cid if key == old_cid else key: replace(item) for key, item in value.items()
                }
            return new_cid if value == old_cid else value

        changed = replace(changed)
        staged = self.stage(changed)
        self.assertEqual(staged["issues"], [], staged)
        body = {"previous_catalog_version_id": old_id, "import_id": staged["id"]}
        preview = self.post("catalog-updates/preview", body).json()
        self.assertFalse(preview["can_activate"])
        self.assertEqual(
            preview["missing_clause_mappings"],
            [{"old_requirement_id": record["id"], "old_clause_id": old_cid}],
        )
        installed = self.post(
            "catalog-updates/commit",
            body | {"confirmed": True, "activate": False, "expected_sha256": preview["sha256"]},
        )
        self.assertEqual(installed.status_code, 200, installed.text)
        new_id = installed.json()["id"]
        blocked = self.client.patch(
            f"/api/v1/projects/{self.pid}/catalogs/{new_id}/binding",
            json={"enabled": True},
            headers=self.headers,
        )
        self.assertIn(blocked.status_code, [409, 422])
        body["clause_mappings"] = [
            {
                "old_requirement_id": record["id"],
                "old_clause_id": old_cid,
                "targets": [{"requirement_id": record["id"], "clause_id": new_cid}],
            }
        ]
        preview = self.post("catalog-updates/preview", body).json()
        self.assertTrue(preview["can_activate"], preview)
        self.assertEqual(
            len(preview["clause_mappings"][0]["targets"][0]["target_semantic_sha256"]), 64
        )
        stale = self.post(
            "catalog-updates/commit",
            body | {"confirmed": True, "activate": True, "expected_sha256": "0" * 64},
        )
        self.assertEqual(stale.status_code, 409)
        accepted = self.post(
            "catalog-updates/commit",
            body | {"confirmed": True, "activate": True, "expected_sha256": preview["sha256"]},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        bindings = {
            item["id"]: item["enabled"]
            for item in self.client.get(f"/api/v1/projects/{self.pid}/catalogs").json()
        }
        self.assertFalse(bindings[old_id])
        self.assertTrue(bindings[new_id])
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{self.pid}/catalogs/{old_id}/export").json(),
            self.value,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/projects/{self.other}/catalogs/{new_id}/update-receipt"
            ).status_code,
            404,
        )
        wrong_scope = self.client.post(
            f"/api/v1/projects/{self.other}/catalog-updates/preview",
            json=body,
            headers=self.headers,
        )
        self.assertEqual(wrong_scope.status_code, 404)

    def test_immutable_requirement_version_and_explicit_retirement(self):
        old_id = self.bind(self.value)
        changed = copy.deepcopy(self.value)
        changed["catalog_version"] = "9.0.0"
        changed["requirements"][0]["statement"] += " Different policy."
        self.assertIn(
            "immutable_requirement_version_conflict",
            {issue["code"] for issue in self.stage(changed)["issues"]},
        )
        # A new record can replace a removed ID only with an explicit mapping.
        changed["requirements"][0]["id"] += "-REPLACEMENT"
        staged = self.stage(changed)
        self.assertEqual(staged["issues"], [], staged)
        body = {"previous_catalog_version_id": old_id, "import_id": staged["id"]}
        preview = self.post("catalog-updates/preview", body).json()
        rid = self.value["requirements"][0]["id"]
        self.assertEqual(preview["missing_record_mappings"], [rid])
        body["record_mappings"] = [
            {
                "old_requirement_id": rid,
                "target_requirement_ids": [changed["requirements"][0]["id"]],
            }
        ]
        preview = self.post("catalog-updates/preview", body).json()
        self.assertTrue(preview["can_activate"], preview)
        self.assertEqual(
            preview["record_mappings"][0]["old_semantic_sha256"],
            semantic_hash(self.value["requirements"][0]),
        )
        self.assertEqual(
            preview["record_mappings"][0]["targets"][0]["target_semantic_sha256"],
            semantic_hash(changed["requirements"][0]),
        )

    def test_local_override_conflict_blocks_activation_and_old_run_remains_frozen(self):
        old_id = self.bind(self.value)
        store = Store(self.root / "app.sqlite3")
        repository = RunRepository(store, ArtifactStore(self.root / "artifacts"))
        rid, _ = repository.create(
            self.pid,
            "freeze-before-update",
            {},
            {
                "documents": [],
                "catalogs": [{"id": old_id, "sha256": digest(self.value)}],
                "configuration_refs": [],
            },
        )
        before = repository.get(self.pid, rid)
        store.close()
        override = copy.deepcopy(self.value)
        override["catalog_id"] += ".local"
        record = override["requirements"][0]
        record["id"] = "USER-LOCAL-OVERRIDE"
        record["record_key"] = record["id"]
        target = self.value["requirements"][0]
        record["policy_scope"] = {
            "override_of": {
                "catalog_version_id": old_id,
                "requirement_id": target["id"],
                "version": target["version"],
                "target_semantic_sha256": semantic_hash(target),
            }
        }
        override_id = self.bind(override)
        changed = copy.deepcopy(self.value)
        changed["catalog_version"] = "9.0.0"
        changed["requirements"][0]["version"] = "2.0.0"
        changed["requirements"][0]["statement"] += " Revised policy decision."
        staged = self.stage(changed)
        body = {"previous_catalog_version_id": old_id, "import_id": staged["id"]}
        plan = self.post("catalog-updates/preview", body).json()
        self.assertFalse(plan["can_activate"])
        self.assertEqual(plan["local_conflicts"][0]["kind"], "local_override_target_changed")
        rejected = self.post(
            "catalog-updates/commit",
            body | {"confirmed": True, "activate": True, "expected_sha256": plan["sha256"]},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(len(self.client.get(f"/api/v1/projects/{self.pid}/catalogs").json()), 2)
        self.client.patch(
            f"/api/v1/projects/{self.pid}/catalogs/{override_id}/binding",
            json={"enabled": False},
            headers=self.headers,
        ).raise_for_status()
        plan = self.post("catalog-updates/preview", body).json()
        self.assertTrue(plan["can_activate"])
        result = self.post(
            "catalog-updates/commit",
            body | {"confirmed": True, "activate": True, "expected_sha256": plan["sha256"]},
        )
        self.assertEqual(result.status_code, 200, result.text)
        store = Store(self.root / "app.sqlite3")
        self.assertEqual(
            RunRepository(store, ArtifactStore(self.root / "artifacts")).get(self.pid, rid), before
        )
        store.close()
        self.client.patch(
            f"/api/v1/projects/{self.pid}/catalogs/{override_id}/binding",
            json={"enabled": True},
            headers=self.headers,
        ).raise_for_status()
        candidate_id = result.json()["id"]
        self.client.patch(
            f"/api/v1/projects/{self.pid}/catalogs/{candidate_id}/binding",
            json={"enabled": False},
            headers=self.headers,
        ).raise_for_status()
        stale_acceptance = self.client.patch(
            f"/api/v1/projects/{self.pid}/catalogs/{candidate_id}/binding",
            json={"enabled": True},
            headers=self.headers,
        )
        self.assertEqual(stale_acceptance.status_code, 409)

    def test_selected_export_exact_versions_fields_and_foreign_scope(self):
        cid = self.bind(self.value)
        for key, value in {
            "domain": self.value["requirements"][0]["domain"],
            "profile": self.value["requirements"][0]["source_context"]["profile"],
            "level": self.value["requirements"][0]["level"],
            "source_kind": self.value["requirements"][0]["source_kind"],
        }.items():
            path = f"/api/v1/projects/{self.pid}/catalogs/{cid}/requirements"
            self.assertEqual(len(self.client.get(path, params={key: value}).json()), 1)
            self.assertEqual(self.client.get(path, params={key: "missing-filter-value"}).json(), [])
        rid = self.value["requirements"][0]["id"]
        result = self.post(
            f"catalogs/{cid}/selected-export", {"requirement_ids": [rid], "format": "json"}
        )
        self.assertEqual(result.status_code, 200, result.text)
        preview = result.json()
        self.assertFalse(preview["includes_project_documents"])
        self.assertFalse(preview["includes_secrets"])
        exported = json.loads(preview["content"])
        self.assertEqual(exported["requirements"], self.value["requirements"])
        self.assertEqual(
            preview["selected_versions"],
            [{"id": rid, "version": self.value["requirements"][0]["version"]}],
        )
        self.assertEqual(
            self.post(
                f"catalogs/{cid}/selected-export", {"requirement_ids": [rid, rid]}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.post(
                f"catalogs/{cid}/selected-export", {"requirement_ids": ["foreign-id"]}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/projects/{self.other}/catalogs/{cid}/selected-export",
                json={"requirement_ids": [rid]},
                headers=self.headers,
            ).status_code,
            404,
        )
