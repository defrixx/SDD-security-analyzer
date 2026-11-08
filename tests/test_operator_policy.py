import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from spec_enricher.api import create_app
from spec_enricher.connections import ConnectionProfile
from spec_enricher.network import Destination, NetworkError, Transport
from spec_enricher.registry import Registry
from spec_enricher.sources import SourceDefinition, permission
from spec_enricher.storage import Store

ORIGIN = "http://127.0.0.1:8080"


class OperatorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"APP_DENY_EXTERNAL_EGRESS": "false"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = TestClient(create_app(self.root), base_url=ORIGIN)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        token = (self.root / "bootstrap.token").read_text()
        login = self.client.post(
            "/api/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
        ).json()
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        self.store = Store(self.root / "app.sqlite3")
        self.addCleanup(self.store.close)
        self.registry = Registry(self.store)
        self.pid = self.store.create_project("Synthetic policy")

    def enable_saved_policy(self):
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE instance_settings SET value='true' WHERE key='allow_external_egress'"
            )

    def test_operator_env_dominates_saved_policy_and_cannot_be_lifted_by_ui(self):
        self.enable_saved_policy()
        with patch.dict(os.environ, {"APP_DENY_EXTERNAL_EGRESS": "true"}):
            policy = self.client.get("/api/v1/operator/network-policy").json()
            self.assertFalse(policy["allow_external_egress"])
            self.assertTrue(policy["operator_locked"])
            result = self.client.put(
                "/api/v1/operator/network-policy",
                json={"confirmed": True, "allow_external_egress": True},
                headers=self.headers,
            )
            self.assertEqual(result.status_code, 403)
            self.assertEqual(
                self.store.connection.execute(
                    "SELECT value FROM instance_settings WHERE key='allow_external_egress'"
                ).fetchone()[0],
                "true",
            )
        self.assertTrue(
            self.client.get("/api/v1/operator/network-policy").json()["allow_external_egress"]
        )

    def test_operator_false_never_grants_egress_and_true_blocks_dns_before_lookup(self):
        self.assertFalse(
            self.client.get("/api/v1/operator/network-policy").json()["allow_external_egress"]
        )
        self.enable_saved_policy()
        with (
            patch.dict(os.environ, {"APP_DENY_EXTERNAL_EGRESS": "1"}),
            patch(
                "spec_enricher.connection_api.resolve_host",
                side_effect=AssertionError("Must not resolve an external hostname"),
            ) as dns,
        ):
            response = self.client.post(
                "/api/v1/connection-diagnostics/resolve",
                json={"endpoint": "https://external.invalid/v1"},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 403)
            dns.assert_not_called()

    def test_connection_and_source_grants_cannot_override_operator_env(self):
        self.enable_saved_policy()
        profile = ConnectionProfile(
            name="Synthetic external",
            endpoint="https://external.invalid/v1",
            model="synthetic",
            enabled=True,
            data_scopes=["delta"],
        ).model_dump()
        connection = self.registry.save(self.pid, "connection", profile)
        source = self.registry.save(
            self.pid,
            "source",
            SourceDefinition(
                name="Synthetic external source",
                kind="https",
                uri="https://external.invalid/page",
                enabled=True,
            ).model_dump(),
        )
        granted = self.client.post(
            f"/api/v1/projects/{self.pid}/sources/{source['id']}/grants",
            json={"confirmed": True, "revision": 1, "expected_sha256": source["sha256"]},
            headers=self.headers,
        )
        self.assertEqual(granted.status_code, 200, granted.text)
        with patch.dict(os.environ, {"APP_DENY_EXTERNAL_EGRESS": "true"}):
            with self.assertRaises(ValueError):
                self.registry.require_egress(self.pid, connection["id"], 1, set(), synthetic=True)
            with self.assertRaises(ValueError):
                permission(self.registry, self.pid, source["id"], 1)
            # Configuration stays portable/editable while transmission is blocked.
            saved = self.registry.save(self.pid, "connection", profile | {"name": "Still editable"})
            self.assertIsNotNone(saved)

    def test_transport_hard_limit_precedes_dns_and_socket_without_blocking_local_profiles(self):
        with (
            patch.dict(os.environ, {"APP_DENY_EXTERNAL_EGRESS": "true"}),
            patch(
                "spec_enricher.network.resolve_host", side_effect=AssertionError("No DNS")
            ) as dns,
        ):
            for addresses in [(), ("8.8.8.8",)]:
                with self.assertRaisesRegex(NetworkError, "operator_external_egress_denied"):
                    Transport(Destination("https://external.invalid", addresses)).request("GET")
            dns.assert_not_called()
            local = Destination("http://127.0.0.1:1234/v1", ("127.0.0.1",))
            self.assertEqual(local.parts()[1], 1234)
        with (
            patch.dict(os.environ, {"APP_DENY_EXTERNAL_EGRESS": "true"}),
            patch("spec_enricher.connection_api.resolve_host", return_value=["127.0.0.1"]),
        ):
            local = self.client.post(
                "/api/v1/connection-diagnostics/resolve",
                json={"endpoint": "http://127.0.0.1:1234/v1"},
                headers=self.headers,
            )
            self.assertEqual(local.status_code, 200)
            self.assertFalse(local.json()["permission_granted"])

    def test_invalid_operator_env_refuses_start_without_rotating_existing_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with TestClient(create_app(root), base_url=ORIGIN):
                pass
            token = (root / "bootstrap.token").read_bytes()
            with patch.dict(os.environ, {"APP_DENY_EXTERNAL_EGRESS": "not-a-boolean"}):
                with self.assertRaisesRegex(ValueError, "invalid_operator_egress_policy"):
                    create_app(root)
            self.assertEqual((root / "bootstrap.token").read_bytes(), token)
            with TestClient(create_app(root), base_url=ORIGIN) as client:
                self.assertEqual(client.get("/health").status_code, 200)
