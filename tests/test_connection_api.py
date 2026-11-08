import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient
from test_connections import Handler

from spec_enricher.api import create_app

ORIGIN = "http://127.0.0.1:8080"


class ConnectionAPITests(unittest.TestCase):
    def test_recursive_regex_from_untrusted_mcp_schema_is_a_controlled_failure(self):
        self.server.tool_schema = {
            "type": "object",
            "properties": {"text": {"type": "string", "pattern": "(" * 1200 + "a" + ")" * 1200}},
            "additionalProperties": False,
        }
        prefix = f"/api/v1/projects/{self.a}/connections"
        saved = self.client.post(
            prefix,
            json={
                "name": "Untrusted schema recursion",
                "kind": "mcp",
                "adapter": "streamable_http",
                "endpoint": f"http://127.0.0.1:{self.server.server_port}/mcp",
                "local_addresses": ["127.0.0.1"],
            },
            headers=self.headers,
        )
        saved.raise_for_status()
        response = self.client.post(f"{prefix}/{saved.json()['id']}/test", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()["result"]
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["network"], "reachable")
        self.assertEqual(report["auth"], "accepted")
        self.assertEqual(report["discovery"], "failed")
        self.assertEqual(report["tools"], [])

    def test_mcp_discovery_cannot_persist_a_json_escaped_credential(self):
        secret = 'SYNTHETIC_TOOL_LIST_"_slash_\\_SECRET_694713'
        saved = self.client.post(
            f"/api/v1/projects/{self.a}/secrets", json={"value": secret}, headers=self.headers
        )
        saved.raise_for_status()
        profile = {
            "name": "Untrusted tool metadata",
            "kind": "mcp",
            "adapter": "streamable_http",
            "endpoint": f"http://127.0.0.1:{self.server.server_port}/mcp",
            "local_addresses": ["127.0.0.1"],
            "auth": "bearer",
            "secret_ref": saved.json()["secret_ref"],
            "enabled": True,
        }
        prefix = f"/api/v1/projects/{self.a}/connections"
        connection = self.client.post(prefix, json=profile, headers=self.headers)
        connection.raise_for_status()
        self.server.tool_description = secret
        for sse in (False, True):
            with self.subTest(sse=sse):
                self.server.sse = sse
                response = self.client.post(
                    f"{prefix}/{connection.json()['id']}/test", headers=self.headers
                )
                response.raise_for_status()
                report = response.json()["result"]
                self.app.state.secrets.reject_known(response.content)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["network"], "reachable")
                self.assertEqual(report["auth"], "accepted")
                self.assertEqual(report["discovery"], "failed")
                self.assertEqual(report["tools"], [])
                for path in self.root.glob("app.sqlite3*"):
                    self.app.state.secrets.reject_known(path.read_bytes())

    def test_model_discovery_cannot_persist_a_known_credential_in_its_report(self):
        secret = 'SYNTHETIC_MODEL_LIST_"_slash_\\_SECRET_834571'
        saved = self.client.post(
            f"/api/v1/projects/{self.a}/secrets", json={"value": secret}, headers=self.headers
        )
        saved.raise_for_status()
        profile = {
            "name": "Untrusted model metadata",
            "endpoint": f"http://127.0.0.1:{self.server.server_port}/v1",
            "local_addresses": ["127.0.0.1"],
            "model": "synthetic-model",
            "auth": "bearer",
            "secret_ref": saved.json()["secret_ref"],
            "enabled": True,
        }
        prefix = f"/api/v1/projects/{self.a}/connections"
        connection = self.client.post(prefix, json=profile, headers=self.headers)
        connection.raise_for_status()
        self.server.model_catalog = {"data": [{"id": "synthetic-model"}, {"id": secret}]}
        response = self.client.post(
            f"{prefix}/{connection.json()['id']}/test", headers=self.headers
        )
        response.raise_for_status()
        report = response.json()["result"]
        self.assertNotIn(secret, report["model_ids"])
        self.app.state.secrets.reject_known(response.content)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["network"], "reachable")
        self.assertEqual(report["auth"], "accepted")
        self.assertEqual(report["models"], "failed")
        self.assertEqual(report["inference"], "not_tested")
        self.assertEqual(report["schema"], "not_tested")
        self.assertEqual(report["model_ids"], [])
        self.assertEqual(len(self.server.requests), 0)
        for path in self.root.glob("app.sqlite3*"):
            self.app.state.secrets.reject_known(path.read_bytes())

    def test_bad_model_catalog_is_not_reported_as_an_inference_response(self):
        self.server.model_catalog = {"data": [{"id": None}]}
        profile = {
            "name": "Invalid model catalogue",
            "endpoint": f"http://127.0.0.1:{self.server.server_port}/v1",
            "local_addresses": ["127.0.0.1"],
            "model": "synthetic-model",
            "enabled": True,
        }
        prefix = f"/api/v1/projects/{self.a}/connections"
        connection = self.client.post(prefix, json=profile, headers=self.headers)
        connection.raise_for_status()
        response = self.client.post(
            f"{prefix}/{connection.json()['id']}/test", headers=self.headers
        )
        response.raise_for_status()
        result = response.json()["result"]
        self.assertEqual(result["network"], "reachable")
        self.assertEqual(result["auth"], "accepted")
        self.assertEqual(result["models"], "failed")
        self.assertEqual(result["inference"], "not_tested")
        self.assertEqual(result["schema"], "not_tested")
        self.assertEqual(result["model_ids"], [])
        self.assertEqual(len(self.server.requests), 0)

    def test_explicit_fenced_prompt_json_reports_normalization_and_keeps_profile(self):
        self.server.model_content = '```json\n{"ok":true}\n```'
        profile = {
            "name": "Explicit degraded synthetic",
            "endpoint": f"http://127.0.0.1:{self.server.server_port}/v1",
            "local_addresses": ["127.0.0.1"],
            "model": "synthetic-model",
            "output_mode": "prompt_json",
            "enabled": True,
            "data_scopes": ["delta"],
        }
        prefix = f"/api/v1/projects/{self.a}/connections"
        connection = self.client.post(prefix, json=profile, headers=self.headers).json()
        tested = self.client.post(f"{prefix}/{connection['id']}/test", headers=self.headers)
        report = tested.json()["result"]
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["schema"], "passed")
        self.assertEqual(report["format_normalization"], "single_json_code_fence")
        saved = self.client.get(f"{prefix}/{connection['id']}").json()
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["content"]["output_mode"], "prompt_json")
        self.assertEqual(saved["granted_scopes"], [])
        self.assertEqual(len(self.server.requests), 1)
        self.assertNotIn("response_format", self.server.requests[0])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.app = create_app(self.root, ORIGIN)
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.client.__enter__()
        token = (self.root / "bootstrap.token").read_text()
        result = self.client.post(
            "/api/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
        )
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}
        self.a = self.client.post(
            "/api/v1/projects", json={"name": "A"}, headers=self.headers
        ).json()["id"]
        self.b = self.client.post(
            "/api/v1/projects", json={"name": "B"}, headers=self.headers
        ).json()["id"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.invalid_output = False
        self.server.sse = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def test_actual_capability_and_immutable_revision_grants(self):
        profile = {
            "name": "Local synthetic",
            "endpoint": f"http://127.0.0.1:{self.server.server_port}/v1",
            "local_addresses": ["127.0.0.1"],
            "model": "synthetic-model",
            "enabled": True,
            "data_scopes": ["delta", "baseline_fragments", "catalog"],
        }
        prefix = f"/api/v1/projects/{self.a}/connections"
        result = self.client.post(prefix, json=profile, headers=self.headers)
        self.assertEqual(result.status_code, 201, result.text)
        record = result.json()
        cid = record["id"]
        tested = self.client.post(f"{prefix}/{cid}/test", headers=self.headers)
        self.assertEqual(tested.json()["result"]["status"], "passed")
        self.assertEqual(len(self.server.requests), 1)
        granted = self.client.post(
            f"{prefix}/{cid}/grants",
            json={
                "confirmed": True,
                "revision": 1,
                "expected_sha256": record["sha256"],
                "data_scopes": profile["data_scopes"],
            },
            headers=self.headers,
        )
        self.assertEqual(granted.status_code, 200, granted.text)
        self.assertEqual(
            self.client.get(f"{prefix}/{cid}").json()["granted_scopes"], profile["data_scopes"]
        )
        changed = profile | {"model": "different-model"}
        updated = self.client.put(
            f"{prefix}/{cid}",
            json={"expected_revision": 1, "profile": changed},
            headers=self.headers,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        current = self.client.get(f"{prefix}/{cid}").json()
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["granted_scopes"], [])
        self.assertIsNone(current["capability_report"])
        old = self.client.get(f"{prefix}/{cid}?revision=1").json()
        self.assertEqual(old["content"]["model"], "synthetic-model")
        self.assertTrue(old["granted_scopes"])
        conflict = self.client.put(
            f"{prefix}/{cid}",
            json={"expected_revision": 1, "profile": changed},
            headers=self.headers,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{self.b}/connections/{cid}").status_code, 404
        )

    def test_secret_value_never_in_sqlite_config_or_response(self):
        secret = "synthetic-never-persist-this-secret-98765"
        result = self.client.post(
            f"/api/v1/projects/{self.a}/secrets", json={"value": secret}, headers=self.headers
        )
        self.assertEqual(result.status_code, 201)
        self.assertNotIn(secret, result.text)
        profile = {
            "name": "Auth profile",
            "endpoint": f"http://127.0.0.1:{self.server.server_port}/v1",
            "local_addresses": ["127.0.0.1"],
            "model": "synthetic-model",
            "auth": "bearer",
            "secret_ref": result.json()["secret_ref"],
        }
        result = self.client.post(
            f"/api/v1/projects/{self.a}/connections", json=profile, headers=self.headers
        )
        self.assertEqual(result.status_code, 201, result.text)
        self.assertNotIn(secret, result.text)
        for path in self.root.glob("app.sqlite3*"):
            self.assertNotIn(secret.encode(), path.read_bytes())
        unsafe = profile | {"secret_ref": secret}
        self.assertEqual(
            self.client.post(
                f"/api/v1/projects/{self.a}/connections", json=unsafe, headers=self.headers
            ).status_code,
            422,
        )

    def test_external_policy_blocks_before_any_network_and_raw_credentials_rejected(self):
        profile = {
            "name": "External",
            "endpoint": "https://no-network.invalid/v1",
            "model": "synthetic-model",
        }
        result = self.client.post(
            f"/api/v1/projects/{self.a}/connections", json=profile, headers=self.headers
        )
        cid = result.json()["id"]
        response = self.client.post(
            f"/api/v1/projects/{self.a}/connections/{cid}/test", headers=self.headers
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.server.requests, [])
        for endpoint in [
            "https://user:password@example.com/v1",
            "https://example.com/v1?token=abc",
        ]:
            unsafe = profile | {"endpoint": endpoint}
            self.assertEqual(
                self.client.post(
                    f"/api/v1/projects/{self.a}/connections", json=unsafe, headers=self.headers
                ).status_code,
                422,
            )

    def test_malformed_mcp_schema_is_controlled_validation_error(self):
        profile = {
            "name": "MCP",
            "kind": "mcp",
            "adapter": "streamable_http",
            "endpoint": f"http://127.0.0.1:{self.server.server_port}/mcp",
            "local_addresses": ["127.0.0.1"],
            "allowed_tools": {
                "read_document": {"read_only_confirmed": True, "input_schema": {"type": "shell"}}
            },
        }
        response = self.client.post(
            f"/api/v1/projects/{self.a}/connections", json=profile, headers=self.headers
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
