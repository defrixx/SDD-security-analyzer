import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from spec_enricher.api import create_app
from spec_enricher.connections import ConnectionProfile
from spec_enricher.storage import Store


class StartupConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.config = self.root / "config"
        self.config.mkdir()
        self.file = self.config / "settings-bundle.json"
        self.environment = patch.dict(os.environ, {"APP_CONFIG_DIR": str(self.config)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def bundle(self, name="Bootstrap project"):
        return {
            "schema_version": 1,
            "app_version": "1.0.0",
            "sanitized": False,
            "projects": [
                {
                    "alias": "initial",
                    "name": name,
                    "configuration": [
                        {
                            "kind": "connection",
                            "id": "portable-model",
                            "revision": 1,
                            "content": ConnectionProfile(
                                name="Explicit local model",
                                endpoint="http://127.0.0.1:1234/v1",
                                local_addresses=["127.0.0.1"],
                                model="synthetic",
                                enabled=True,
                                auth="bearer",
                                secret_ref="session:00000000-0000-4000-8000-000000000000",
                                data_scopes=["delta"],
                            ).model_dump(),
                        }
                    ],
                    "catalogues": [],
                }
            ],
            "operator_preferences": {"allow_external_egress": True},
        }

    def write(self, name="Bootstrap project"):
        self.file.write_text(json.dumps(self.bundle(name)))

    def start(self):
        return TestClient(create_app(self.data), base_url="http://127.0.0.1:8080")

    def test_first_start_imports_disabled_and_saved_settings_win_on_restart(self):
        self.write()
        original = self.file.read_bytes()
        with patch(
            "spec_enricher.network.Transport.request", side_effect=AssertionError("No egress")
        ):
            with self.start():
                pass
        store = Store(self.data / "app.sqlite3")
        try:
            projects = store.projects()
            self.assertEqual(len(projects), 1)
            from spec_enricher.registry import Registry

            model = Registry(store).entries(projects[0][0], "connection")[0]
            self.assertFalse(model["content"]["enabled"])
            self.assertEqual(model["content"]["auth"], "none")
            self.assertIsNone(model["content"]["secret_ref"])
            self.assertEqual(model["granted_scopes"], [])
            self.assertEqual(
                store.connection.execute("SELECT count(*) FROM documents").fetchone()[0], 0
            )
            with store.connection:
                store.connection.execute("UPDATE projects SET name='Saved UI name'")
        finally:
            store.close()
        self.assertEqual(self.file.read_bytes(), original)
        self.file.write_text("invalid changed startup config must not be read again")
        with self.start():
            pass
        store = Store(self.data / "app.sqlite3")
        try:
            self.assertEqual([row[1] for row in store.projects()], ["Saved UI name"])
        finally:
            store.close()

    def test_empty_first_start_does_not_import_later_file(self):
        with self.start():
            pass
        self.write()
        with self.start():
            pass
        store = Store(self.data / "app.sqlite3")
        try:
            self.assertEqual(store.projects(), [])
        finally:
            store.close()

    def test_v14_upgrade_preserves_existing_projects_and_skips_bootstrap(self):
        self.data.mkdir()
        store = Store(self.data / "app.sqlite3")
        original = store.create_project("Existing project")
        with store.connection:
            store.connection.execute(
                "DELETE FROM instance_settings WHERE key='bootstrap_configuration'"
            )
            store.connection.execute("PRAGMA user_version=14")
        store.close()
        self.write()
        with self.start():
            pass
        store = Store(self.data / "app.sqlite3")
        try:
            self.assertEqual(store.projects(), [(original, "Existing project", "")])
            backup = sqlite3.connect(next((self.data / "migration-backups").glob("*.sqlite3")))
            try:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 14)
                self.assertEqual(
                    backup.execute("SELECT name FROM projects").fetchone()[0], "Existing project"
                )
            finally:
                backup.close()
        finally:
            store.close()

    def test_invalid_bootstrap_is_redacted_atomic_and_retryable(self):
        self.file.write_text('{"PRIVATE_UNTRUSTED_VALUE":1,"PRIVATE_UNTRUSTED_VALUE":2}')
        with self.assertRaises(ValueError) as failure:
            self.start()
        self.assertIn("bootstrap_configuration_rejected", str(failure.exception))
        self.assertNotIn("PRIVATE_UNTRUSTED_VALUE", str(failure.exception))
        store = Store(self.data / "app.sqlite3")
        try:
            self.assertEqual(store.projects(), [])
        finally:
            store.close()
        self.write()
        with self.start():
            pass
        store = Store(self.data / "app.sqlite3")
        try:
            self.assertEqual(len(store.projects()), 1)
        finally:
            store.close()

    def test_symlink_fifo_and_oversized_bootstrap_are_rejected_without_reading(self):
        target = self.root / "outside.json"
        target.write_text("PRIVATE_OUTSIDE_FILE")
        self.file.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "bootstrap_configuration_rejected"):
            self.start()
        self.file.unlink()
        os.mkfifo(self.file)
        with self.assertRaisesRegex(ValueError, "bootstrap_configuration_rejected"):
            self.start()
        self.file.unlink()
        with self.file.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "bootstrap_configuration_rejected"):
            self.start()

    def test_crash_after_import_commit_recovers_receipt_without_reimport(self):
        from spec_enricher.startup import _record

        self.write()

        def fail_completion(store, value):
            if value["status"] == "imported":
                raise OSError("synthetic crash after import commit")
            _record(store, value)

        with patch("spec_enricher.startup._record", side_effect=fail_completion):
            with self.assertRaisesRegex(ValueError, "bootstrap_configuration_rejected"):
                self.start()
        self.write("Changed file after crash")
        with self.start():
            pass
        store = Store(self.data / "app.sqlite3")
        try:
            self.assertEqual([row[1] for row in store.projects()], ["Bootstrap project"])
            self.assertEqual(
                store.connection.execute("SELECT count(*) FROM settings_imports").fetchone()[0], 1
            )
        finally:
            store.close()

    def test_changed_pinned_bundle_after_preparation_failure_is_rejected(self):
        self.write()
        with patch(
            "spec_enricher.startup.import_settings",
            side_effect=ValueError("synthetic unavailable worker"),
        ):
            with self.assertRaisesRegex(ValueError, "bootstrap_configuration_rejected"):
                self.start()
        self.write("Changed during initialization")
        with self.assertRaisesRegex(ValueError, "bootstrap_configuration_rejected"):
            self.start()
        self.write()
        with self.start():
            pass
