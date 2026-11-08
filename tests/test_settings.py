import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from spec_enricher.api import create_app
from spec_enricher.connections import ConnectionProfile
from spec_enricher.pipeline_config import CONTRACTS
from spec_enricher.registry import Registry
from spec_enricher.storage import Store
from spec_enricher.workers import WorkerError

ORIGIN = "http://127.0.0.1:8080"


class SettingsTests(unittest.TestCase):
    def test_import_preparation_failure_does_not_create_partial_scopes(self):
        raw = self.export()
        preview = self.preview(raw).json()
        before = self.client.get("/api/v1/projects").json()
        with patch(
            "spec_enricher.settings.bounded", side_effect=WorkerError("worker time limit exceeded")
        ):
            response = self.client.post(
                "/api/v1/settings/import/commit",
                json={
                    "preview_id": preview["preview_id"],
                    "expected_sha256": preview["sha256"],
                    "confirmed": True,
                },
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/v1/projects").json(), before)
        self.assertEqual(self.client.get("/api/v1/settings/imports").json(), [])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root, ORIGIN)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        token = (self.root / "bootstrap.token").read_text()
        login = self.client.post(
            "/api/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
        ).json()
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        store = Store(self.root / "app.sqlite3")
        self.pid = store.create_project("Internal project", "Private deployment description")
        store.add_document(
            self.pid, "private.md", "baseline", b"PRIVATE_DOCUMENT_NEVER_TRANSFERRED"
        )
        registry = Registry(store)
        self.key = "SYNTHETIC_SECRET_NEVER_IN_SETTINGS_BUNDLE_876543"
        reference = self.app.state.secrets.put(self.pid, self.key)
        profile = ConnectionProfile(
            name="Internal model",
            endpoint="http://127.0.0.1:1234/v1",
            local_addresses=["127.0.0.1"],
            model="synthetic-model",
            enabled=True,
            auth="bearer",
            secret_ref=reference,
            data_scopes=["delta", "baseline_fragments", "catalog", "skill_outputs"],
        )
        self.connection = registry.save(self.pid, "connection", profile.model_dump())
        registry.report(
            self.pid, self.connection["id"], 1, {"status": "passed", "kind": "synthetic_fixture"}
        )
        registry.grant(
            self.pid, self.connection["id"], 1, self.connection["sha256"], profile.data_scopes
        )
        self.prompt = registry.save(
            self.pid,
            "prompt",
            {
                "name": "Private prompt",
                "version": "1.0.0",
                "stage": "P5",
                "template": "Pinned original review prompt",
                "output_schema": CONTRACTS["P5"].model_json_schema(),
                "status": "published",
                "changelog": "Original",
            },
        )
        registry.save(
            self.pid,
            "prompt",
            self.prompt["content"]
            | {"version": "2.0.0", "template": "New head, not the pinned replacement."},
            object_id=self.prompt["id"],
            expected_revision=1,
        )
        skill = registry.save(
            self.pid,
            "skill",
            {
                "name": "Replacement",
                "version": "1.0.0",
                "description": "Synthetic config only",
                "stage": "P5",
                "hook": "replace",
                "input_schema": {"type": "object"},
                "output_schema": CONTRACTS["P5"].model_json_schema(),
                "prompt_ref": {"id": self.prompt["id"], "revision": 1},
                "data_scopes": profile.data_scopes,
                "enabled": True,
            },
        )
        registry.save(
            self.pid,
            "pipeline",
            {
                "name": "Portable pipeline",
                "version": "1.0.0",
                "connection": {"id": self.connection["id"], "revision": 1},
                "skill_refs": [{"id": skill["id"], "revision": 1}],
            },
        )
        store.bind_catalog(
            self.pid, json.loads(Path("templates/requirements.example.json").read_text())
        )
        store.close()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def export(self, sanitized=False):
        selection = {
            "project_ids": [self.pid],
            "include_catalogues": not sanitized,
            "sanitized": sanitized,
        }
        response = self.client.post(
            "/api/v1/settings/export/preview", json=selection, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        download = self.client.post(
            "/api/v1/settings/export",
            json=selection | {"expected_sha256": result["preview"]["sha256"], "confirmed": True},
            headers=self.headers,
        )
        self.assertEqual(download.status_code, 200, download.text)
        return download.content

    def preview(self, raw):
        return self.client.post(
            "/api/v1/settings/import/preview",
            content=raw,
            headers=self.headers | {"Content-Type": "application/octet-stream"},
        )

    def test_bundle_roundtrip_remaps_versions_and_never_imports_docs_secrets_or_permissions(self):
        raw = self.export()
        self.assertNotIn(self.key.encode(), raw)
        self.assertNotIn(b"PRIVATE_DOCUMENT_NEVER_TRANSFERRED", raw)
        with patch(
            "spec_enricher.network.Transport.request",
            side_effect=AssertionError("Import cannot perform network requests"),
        ):
            preview = self.preview(raw)
            self.assertEqual(preview.status_code, 200, preview.text)
            plan = preview.json()
            self.assertTrue(plan["projects"][0]["unresolved"])
            body = {
                "preview_id": plan["preview_id"],
                "expected_sha256": plan["sha256"],
                "confirmed": True,
            }
            result = self.client.post(
                "/api/v1/settings/import/commit", json=body, headers=self.headers
            )
            self.assertEqual(result.status_code, 200, result.text)
            receipt = result.json()
            new_pid = receipt["projects"][0]["project_id"]
            self.assertNotEqual(new_pid, self.pid)
            repeated = self.client.post(
                "/api/v1/settings/import/commit", json=body, headers=self.headers
            ).json()
            self.assertTrue(repeated["replayed"])
            self.assertEqual(repeated["projects"], receipt["projects"])
        self.assertEqual(self.client.get(f"/api/v1/projects/{new_pid}/documents").json(), [])
        connection = self.client.get(f"/api/v1/projects/{new_pid}/connections").json()[0]
        self.assertFalse(connection["content"]["enabled"])
        self.assertEqual(connection["content"]["auth"], "none")
        self.assertIsNone(connection["content"]["secret_ref"])
        self.assertEqual(connection["granted_scopes"], [])
        self.assertIsNone(connection["capability_report"])
        catalogue = self.client.get(f"/api/v1/projects/{new_pid}/catalogs").json()[0]
        self.assertFalse(catalogue["enabled"])
        store = Store(self.root / "app.sqlite3")
        registry = Registry(store)
        replacement = registry.entries(new_pid, "skill")[0]
        ref = replacement["content"]["prompt_ref"]
        self.assertNotEqual(ref["id"], self.prompt["id"])
        self.assertEqual(ref["revision"], 1)
        self.assertEqual(
            registry.get(new_pid, "prompt", ref["id"], 1)["content"]["template"],
            "Pinned original review prompt",
        )
        self.assertEqual(registry.get(new_pid, "prompt", ref["id"])["revision"], 2)
        store.close()

    def test_sanitized_export_explicitly_resets_private_addresses_names_and_templates(self):
        raw = self.export(sanitized=True)
        for excluded in [
            b"127.0.0.1",
            b"Internal project",
            b"Private deployment",
            b"Pinned original review prompt",
            self.key.encode(),
        ]:
            self.assertNotIn(excluded, raw)
        data = json.loads(raw)
        self.assertTrue(data["sanitized"])
        self.assertIn("reset", " ".join(data["redactions"]).lower())
        self.assertTrue(
            all(c["content"] is None for p in data["projects"] for c in p["catalogues"])
        )
        self.assertEqual(self.preview(raw).status_code, 200)

    def test_future_unsafe_duplicates_and_changed_preview_do_not_create_projects(self):
        raw = self.export()
        count = len(self.client.get("/api/v1/projects").json())
        data = json.loads(raw)
        data["schema_version"] = 2
        self.assertEqual(self.preview(json.dumps(data).encode()).status_code, 422)
        self.assertEqual(self.preview(b'{"schema_version":1,"schema_version":2}').status_code, 422)
        preview = self.preview(raw).json()
        bad = self.client.post(
            "/api/v1/settings/import/commit",
            json={
                "preview_id": preview["preview_id"],
                "expected_sha256": "0" * 64,
                "confirmed": True,
            },
            headers=self.headers,
        )
        self.assertEqual(bad.status_code, 409)
        self.assertEqual(len(self.client.get("/api/v1/projects").json()), count)
