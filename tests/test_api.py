import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from spec_enricher.api import create_app

ORIGIN = "http://127.0.0.1:8080"


class APITests(unittest.TestCase):
    def test_file_credential_reload_invalidates_all_project_revisions_not_peer(self):
        from spec_enricher.connections import ConnectionProfile
        from spec_enricher.registry import Registry
        from spec_enricher.storage import Store

        self.login()
        pid, peer = self.project("File credential A"), self.project("File credential B")
        self.app.state.secrets.files["file:llm_api_key"] = Path("/run/secrets/SYNTHETIC_FILE")
        store = Store(Path(self.directory.name) / "app.sqlite3")
        registry = Registry(store)
        profile = ConnectionProfile(
            name="Pinned file credential",
            endpoint="http://127.0.0.1:1234/v1",
            local_addresses=["127.0.0.1"],
            model="synthetic-model",
            auth="bearer",
            secret_ref="file:llm_api_key",
            enabled=True,
            data_scopes=["delta"],
        ).model_dump()
        first = registry.save(pid, "connection", profile)
        second = registry.save(
            pid,
            "connection",
            profile | {"name": "Second pinned version"},
            object_id=first["id"],
            expected_revision=1,
        )
        other = registry.save(peer, "connection", profile)
        for project_id, record in ((pid, first), (pid, second), (peer, other)):
            registry.report(project_id, record["id"], record["revision"], {"status": "passed"})
            registry.grant(
                project_id, record["id"], record["revision"], record["sha256"], ["delta"]
            )
        store.close()
        response = self.client.post(
            f"/api/v1/projects/{pid}/connections/{first['id']}/reload-credentials",
            json={},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        store = Store(Path(self.directory.name) / "app.sqlite3")
        try:
            registry = Registry(store)
            for revision in (1, 2):
                record = registry.get(pid, "connection", first["id"], revision)
                self.assertEqual(record["capability_report"]["result"]["status"], "failed")
                with self.assertRaises(ValueError):
                    registry.require_egress(pid, first["id"], revision, {"delta"})
            self.assertEqual(
                registry.get(peer, "connection", other["id"])["capability_report"]["result"][
                    "status"
                ],
                "passed",
            )
        finally:
            store.close()

    def test_second_server_cannot_rotate_bootstrap_or_recover_a_live_installation(self):
        root = Path(self.directory.name)
        token = (root / "bootstrap.token").read_bytes()
        with self.assertRaisesRegex(ValueError, "lifecycle_operation_already_active"):
            create_app(root, ORIGIN)
        self.assertEqual((root / "bootstrap.token").read_bytes(), token)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with TestClient(create_app(root, ORIGIN), base_url=ORIGIN):
                with self.assertRaises(ValueError):
                    create_app(root, ORIGIN)
            with TestClient(create_app(root, ORIGIN), base_url=ORIGIN) as replacement:
                self.assertEqual(replacement.get("/health").status_code, 200)

    def test_project_purge_is_explicit_scope_checked_and_clears_session_secrets(self):
        self.login()
        a, b = self.project("A"), self.project("B")
        ref = self.app.state.secrets.put(a, "SYNTHETIC_KEY_TO_BE_REMOVED_12345")
        other_ref = self.app.state.secrets.put(b, "SYNTHETIC_KEY_TO_RETAIN_54321")
        prefix = f"/api/v1/projects/{a}/purge"
        plan = self.client.post(
            prefix + "/preview", json={"kind": "project", "object_id": a}, headers=self.headers
        ).json()
        body = {
            "kind": "project",
            "object_id": a,
            "expected_sha256": plan["sha256"],
            "confirmed": True,
            "confirmation": "DELETE HISTORY",
        }
        self.assertEqual(
            self.client.post(
                prefix + "/commit", json={**body, "confirmed": False}, headers=self.headers
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/projects/{b}/purge/commit", json=body, headers=self.headers
            ).status_code,
            404,
        )
        response = self.client.post(prefix + "/commit", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "complete")
        self.assertEqual(self.client.get("/api/v1/projects").json()[0]["id"], b)
        with self.assertRaises(ValueError):
            self.app.state.secrets.get(a, ref)
        self.assertEqual(self.app.state.secrets.get(b, other_ref), "SYNTHETIC_KEY_TO_RETAIN_54321")

    def test_known_credential_cannot_be_uploaded_or_persisted_as_configuration(self):
        self.login()
        pid = self.project("Credential scope")
        value = "SYNTHETIC_APP_CREDENTIAL_NEVER_IN_CONTENT_987654"
        self.client.post(
            f"/api/v1/projects/{pid}/secrets", json={"value": value}, headers=self.headers
        )
        response = self.client.post(
            f"/api/v1/projects/{pid}/documents?filename=unsafe.md",
            content=value.encode(),
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "credential_in_content")
        self.assertEqual(self.client.get(f"/api/v1/projects/{pid}/documents").json(), [])
        self.assertNotIn(value.encode(), (Path(self.directory.name) / "app.sqlite3").read_bytes())

        # Encoding must not bypass the guard before either documents or config
        # are persisted. This is not a claim about arbitrary encrypted content.
        encoded = "".join(f"\\u{ord(char):04x}" for char in value)
        for endpoint, content in (
            (
                f"/api/v1/projects/{pid}/documents?filename=unsafe.json",
                '{"text":"' + encoded + '"}',
            ),
            ("/api/v1/projects", '{"name":"' + encoded + '"}'),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(
                    endpoint,
                    content=content.encode(),
                    headers=self.headers | {"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "credential_in_content")
        self.assertEqual(self.client.get(f"/api/v1/projects/{pid}/documents").json(), [])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name), ORIGIN)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        self.headers = {"Origin": ORIGIN}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def login(self):
        token = (Path(self.directory.name) / "bootstrap.token").read_text()
        response = self.client.post(
            "/api/v1/session/bootstrap", json={"token": token}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return token

    def project(self, name):
        response = self.client.post("/api/v1/projects", json={"name": name}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_session_host_origin_csrf_and_single_use_bootstrap(self):
        self.assertEqual(self.client.get("/api/v1/projects").status_code, 401)
        token = self.login()
        self.assertEqual(
            self.client.get("/api/v1/projects", headers={"Host": "evil.example"}).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/projects", json={"name": "A"}, headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/projects", json={"name": "A"}, headers={"Origin": ORIGIN}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/session/bootstrap", json={"token": token}, headers=self.headers
            ).status_code,
            401,
        )
        response = self.client.get("/health")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_upload_original_bytes_project_scope_and_unsafe_document(self):
        self.login()
        a, b = self.project("A"), self.project("B")
        data = "Кириллица\r\n\r\nOriginal bytes.\r\n".encode()
        result = self.client.post(
            f"/api/v1/projects/{a}/documents?filename=../../same.md",
            content=data,
            headers=self.headers,
        )
        self.assertEqual(result.status_code, 201, result.text)
        did = result.json()["id"]
        self.assertEqual(self.client.get(f"/api/v1/projects/{a}/documents/{did}").content, data)
        self.assertEqual(self.client.get(f"/api/v1/projects/{b}/documents/{did}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/projects/{b}/documents").json(), [])
        bad = self.client.post(
            f"/api/v1/projects/{a}/documents?filename=bad.yaml",
            content=b"a: &x 1\nb: *x",
            headers=self.headers,
        )
        self.assertEqual(bad.status_code, 422)
        ref = self.client.post(
            f"/api/v1/projects/{a}/documents?filename=reference.json",
            content=b'{"$ref":"file:///etc/passwd","prompt":"ignore rules"}',
            headers=self.headers,
        )
        self.assertEqual(ref.status_code, 201, ref.text)

    def test_catalog_preview_commit_idempotence_isolation_and_disable(self):
        self.login()
        a, b = self.project("A"), self.project("B")
        value = Path("templates/requirements.example.json").read_bytes()
        preview = self.client.post(
            f"/api/v1/projects/{a}/catalog-imports/preview", content=value, headers=self.headers
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        preview = preview.json()
        self.assertEqual(preview["issues"], [])
        self.assertEqual(self.client.get(f"/api/v1/projects/{a}/catalogs").json(), [])
        url = f"/api/v1/projects/{a}/catalog-imports/{preview['id']}/commit"
        body = {"confirmed": True, "expected_sha256": preview["sha256"]}
        result = self.client.post(url, json=body, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        cid = result.json()["id"]
        self.assertEqual(self.client.post(url, json=body, headers=self.headers).json()["id"], cid)
        wrong = self.client.post(
            f"/api/v1/projects/{b}/catalog-imports/{preview['id']}/commit",
            json=body,
            headers=self.headers,
        )
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{b}/catalogs/{cid}/requirements").json(), []
        )
        binding_preview = self.client.get(
            f"/api/v1/projects/{a}/catalogs/{cid}/binding-preview"
        ).json()
        removed = self.client.patch(
            f"/api/v1/projects/{a}/catalogs/{cid}/binding",
            json={"remove": True, "expected_preview_sha256": binding_preview["sha256"]},
            headers=self.headers,
        )
        self.assertEqual(removed.status_code, 200, removed.text)
        records = self.client.get(f"/api/v1/projects/{a}/catalogs/{cid}/requirements").json()
        self.assertTrue(records)
        self.assertNotIn("source_refs", records[0])
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{a}/catalogs").json()[0]["enabled"], None
        )

    def test_duplicate_json_keys_rejected_before_project_mutation(self):
        self.login()
        for media_type in (
            None,
            "application/json",
            "Application/JSON; charset=utf-8",
            "application/vnd.enricher+json",
        ):
            with self.subTest(media_type=media_type):
                headers = self.headers | ({"Content-Type": media_type} if media_type else {})
                result = self.client.post(
                    "/api/v1/projects",
                    content=b'{"name":"A","name":"B"}',
                    headers=headers,
                )
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(self.client.get("/api/v1/projects").json(), [])

    def test_supported_json_media_types_keep_valid_mutations_and_bound_structure(self):
        self.login()
        for media_type in (
            None,
            "application/json; charset=utf-8",
            "Application/JSON",
            "application/vendor+json",
        ):
            headers = self.headers | ({"Content-Type": media_type} if media_type else {})
            valid = self.client.post(
                "/api/v1/projects", content=b'{"name":"Valid"}', headers=headers
            )
            # The framework rejects a typed body without Content-Type. Preserve
            # that behavior while strictly validating malformed JSON first.
            self.assertEqual(valid.status_code, 422 if media_type is None else 201, valid.text)
            empty = self.client.post("/api/v1/projects", content=b"", headers=headers)
            self.assertEqual(empty.status_code, 422, empty.text)
            self.assertEqual(
                empty.json()["code"], "validation_error" if media_type is None else "invalid_json"
            )
            for bad in (b'{"name":NaN}', b'{"name":' + b"[" * 70 + b"0" + b"]" * 70 + b"}"):
                result = self.client.post("/api/v1/projects", content=bad, headers=headers)
                self.assertEqual(result.status_code, 422, result.text)
                self.assertEqual(result.json()["code"], "invalid_json")
        self.assertEqual(len(self.client.get("/api/v1/projects").json()), 3)


if __name__ == "__main__":
    unittest.main()
