import json
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from spec_enricher.api import create_app
from spec_enricher.authoring import new_fields
from spec_enricher.inputs import parse

ORIGIN = "http://127.0.0.1:8080"


class AuthoringTests(unittest.TestCase):
    def test_project_ranking_is_separate_from_catalogue_and_other_projects(self):
        draft, preview = self.save("project", self.pid, self.fields())
        committed = self.commit(draft, preview, activate=True).json()
        path = f"/api/v1/projects/{self.pid}/requirement-ranking"
        body = {
            "requirement_id": self.fields()["id"],
            "version": "1.0.0",
            "expected_revision": 0,
            "ranking": "critical",
            "reason": "Project-specific review priority only.",
        }
        result = self.client.put(path, json=body, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["upstream_severity_unchanged"])
        exported = self.client.get(
            f"/api/v1/projects/{self.pid}/catalogs/{committed['catalog_id']}/export"
        ).json()
        self.assertNotIn("ranking", json.dumps(exported))
        self.assertEqual(exported["requirements"][0]["severity"], "unrated")
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{self.other}/requirement-rankings").json(), []
        )
        self.assertEqual(
            self.client.put(
                f"/api/v1/projects/{self.other}/requirement-ranking",
                json=body,
                headers=self.headers,
            ).status_code,
            404,
        )
        self.assertEqual(self.client.put(path, json=body, headers=self.headers).status_code, 409)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.app = create_app(self.root, ORIGIN)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        token = (self.root / "bootstrap.token").read_text()
        login = self.client.post(
            "/api/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
        ).json()
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        self.pid = self.client.post(
            "/api/v1/projects", json={"name": "A"}, headers=self.headers
        ).json()["id"]
        self.other = self.client.post(
            "/api/v1/projects", json={"name": "B"}, headers=self.headers
        ).json()["id"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def fields(self):
        fields = json.loads(Path("templates/requirements.example.json").read_text())[
            "requirements"
        ][0]
        for key in ["review", "normativity_review", "source_refs", "source_context", "record_key"]:
            fields.pop(key)
        fields["id"] = "USER-OWNED-TEST"
        fields["origin"] = {
            "author": "Synthetic author",
            "external_provenance": None,
            "license": None,
        }
        for part in (
            fields["clauses"]
            + fields["applicability"]["predicates"]
            + list(fields["clause_normativity"].values())
        ):
            part.pop("source_ref_ids", None)
        fields["tags"] = ["roundtrip", "synthetic"]
        return fields

    def save(self, scope, owner, fields):
        result = self.client.post(
            f"/api/v1/requirement-drafts/{scope}/{owner}",
            json={"fields": fields},
            headers=self.headers,
        )
        self.assertEqual(result.status_code, 201, result.text)
        draft = result.json()
        preview = self.client.get(
            f"/api/v1/requirement-drafts/{scope}/{owner}/{draft['draft_id']}/preview"
        ).json()
        return draft, preview

    def commit(self, draft, preview, **kwargs):
        owner = draft["project_id"] or draft["user_catalogue_id"]
        return self.client.post(
            f"/api/v1/requirement-drafts/{draft['scope']}/{owner}/{draft['draft_id']}/commit",
            json={
                "expected_revision": draft["revision"],
                "expected_sha256": preview["sha256"],
                "confirmed": True,
                **kwargs,
            },
            headers=self.headers,
        )

    def test_incomplete_draft_structure_protected_namespace_and_scope(self):
        draft, preview = self.save("project", self.pid, new_fields())
        self.assertTrue(preview["issues"])
        self.assertIsNotNone(preview.get("candidate"), preview)
        self.assertEqual(preview["candidate"]["release_status"], "draft")
        self.assertFalse(preview["candidate"]["ready_for_use"])
        self.assertEqual(self.commit(draft, preview).status_code, 409)
        wrong = self.client.get(
            f"/api/v1/requirement-drafts/project/{self.other}/{draft['draft_id']}/preview"
        )
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/projects/{self.pid}/catalogs").json(), [])

    def test_preview_commit_new_version_diff_and_json_yaml_roundtrip(self):
        fields = self.fields()
        draft, preview = self.save("project", self.pid, fields)
        self.assertEqual(preview["issues"], [], preview)
        result = self.commit(draft, preview, activate=True)
        self.assertEqual(result.status_code, 200, result.text)
        cid = result.json()["catalog_id"]
        original = self.client.get(f"/api/v1/projects/{self.pid}/catalogs/{cid}/export").json()
        self.assertEqual(original["requirements"][0]["review"]["status"], "user_approved")
        exported = self.client.get(f"/api/v1/projects/{self.pid}/catalogs/{cid}/export?format=yaml")
        self.assertEqual(parse(exported.content, "yaml"), original)
        self.assertEqual(json.loads(json.dumps(yaml.safe_load(exported.content))), original)
        changed = fields | {
            "version": "2.0.0",
            "statement": fields["statement"] + " Revised own policy.",
        }
        updated = self.client.put(
            f"/api/v1/requirement-drafts/project/{self.pid}/{draft['draft_id']}",
            json={"expected_revision": 1, "fields": changed},
            headers=self.headers,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        new = updated.json()
        preview = self.client.get(
            f"/api/v1/requirement-drafts/project/{self.pid}/{draft['draft_id']}/preview"
        ).json()
        self.assertTrue(preview["conflicts"][0]["diff"])
        self.assertEqual(self.commit(new, preview, activate=True).status_code, 409)
        committed = self.commit(new, preview, activate=True, replace_previous_bindings=True)
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{self.pid}/catalogs/{cid}/export").json(), original
        )
        bindings = self.client.get(f"/api/v1/projects/{self.pid}/catalogs").json()
        self.assertEqual(sum(bool(b["enabled"]) for b in bindings), 1)

    def test_user_catalogue_explicit_reuse_copies_no_private_project_data(self):
        user = self.client.post(
            "/api/v1/user-catalogues", json={"name": "Reusable own policies"}, headers=self.headers
        ).json()["id"]
        self.client.post(
            f"/api/v1/projects/{self.pid}/documents?filename=private.md",
            content=b"PRIVATE DOCUMENT NEVER IN CATALOGUE",
            headers=self.headers,
        )
        draft, preview = self.save("user_catalogue", user, self.fields())
        self.assertEqual(preview["issues"], [])
        result = self.commit(draft, preview)
        self.assertEqual(result.status_code, 200, result.text)
        versions = self.client.get(f"/api/v1/user-catalogues/{user}/versions").json()
        self.assertNotIn("PRIVATE DOCUMENT NEVER IN CATALOGUE", json.dumps(versions))
        self.assertEqual(self.client.get(f"/api/v1/projects/{self.other}/catalogs").json(), [])
        row = versions[0]
        body = {
            "user_catalogue_id": user,
            "revision": row["revision"],
            "expected_sha256": row["sha256"],
            "confirmed": True,
        }
        binding = self.client.post(
            f"/api/v1/projects/{self.other}/user-catalogue-bindings",
            json=body,
            headers=self.headers,
        )
        self.assertEqual(binding.status_code, 200, binding.text)
        self.assertEqual(self.client.get(f"/api/v1/projects/{self.other}/documents").json(), [])
        self.assertEqual(self.client.get(f"/api/v1/projects/{self.other}/connections").json(), [])

    def test_global_preview_preserves_all_records_and_deleted_revision_is_never_reused(self):
        owner = self.client.post(
            "/api/v1/user-catalogues", json={"name": "Versioned own policies"}, headers=self.headers
        ).json()["id"]
        first, preview = self.save("user_catalogue", owner, self.fields())
        self.assertEqual(self.commit(first, preview).status_code, 200)
        second_fields = self.fields()
        second_fields["id"] = "USER-SECOND-SYNTHETIC"
        second_fields["title"] = "Second independent authored policy"
        second, preview = self.save("user_catalogue", owner, second_fields)
        self.assertEqual(
            {r["id"] for r in preview["candidate"]["requirements"]},
            {self.fields()["id"], second_fields["id"]},
        )
        self.assertEqual(self.commit(second, preview).json()["revision"], 2)
        delete = f"/api/v1/user-catalogues/{owner}/versions/2"
        plan = self.client.get(delete + "/delete-preview").json()
        self.assertTrue(plan["can_delete"])
        self.assertEqual(
            self.client.post(
                delete + "/delete",
                json={"confirmed": True, "expected_sha256": plan["sha256"]},
                headers=self.headers,
            ).status_code,
            200,
        )
        third, preview = self.save("user_catalogue", owner, second_fields)
        self.assertIsNotNone(preview.get("candidate"), preview)
        self.assertEqual(preview["candidate"]["catalog_version"], "3")
        committed = self.commit(third, preview)
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertEqual(committed.json()["revision"], 3)
        version = self.client.get(f"/api/v1/user-catalogues/{owner}/versions").json()[0]
        binding = self.client.post(
            f"/api/v1/projects/{self.pid}/user-catalogue-bindings",
            json={
                "user_catalogue_id": owner,
                "revision": version["revision"],
                "expected_sha256": version["sha256"],
                "confirmed": True,
            },
            headers=self.headers,
        )
        self.assertEqual(binding.status_code, 200, binding.text)
        plan = self.client.get(f"/api/v1/user-catalogues/{owner}/versions/3/delete-preview").json()
        self.assertFalse(plan["can_delete"])
        self.assertEqual(
            self.client.post(
                f"/api/v1/user-catalogues/{owner}/versions/3/delete",
                json={"confirmed": True, "expected_sha256": plan["sha256"]},
                headers=self.headers,
            ).status_code,
            409,
        )
