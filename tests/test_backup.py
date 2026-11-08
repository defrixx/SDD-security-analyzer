import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spec_enricher.artifacts import ArtifactStore
from spec_enricher.backup import create_backup, file_hash, maintenance, restore_backup
from spec_enricher.connections import ConnectionProfile
from spec_enricher.installation import Lease, active_data
from spec_enricher.registry import Registry
from spec_enricher.runs import RunRepository
from spec_enricher.storage import SCHEMA_VERSION, Store


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.installation = self.root / "original"
        self.installation.mkdir()
        self.backups = self.root / "backups"
        self.output = self.backups / "synthetic.tar.gz"
        store = Store(self.installation / "app.sqlite3")
        self.pid = store.create_project("A", "Synthetic recovery project")
        self.other = store.create_project("B", "")
        self.document = store.add_document(
            self.pid, "same.md", "baseline", b"PRIVATE SYNTHETIC A\n"
        )
        registry = Registry(store)
        self.config = registry.save(
            self.pid,
            "connection",
            ConnectionProfile(
                name="Restored connection",
                endpoint="http://127.0.0.1:1234/v1",
                local_addresses=["127.0.0.1"],
                model="synthetic",
                enabled=True,
                data_scopes=["delta"],
            ).model_dump(),
        )
        registry.report(self.pid, self.config["id"], 1, {"status": "passed"})
        registry.grant(self.pid, self.config["id"], 1, self.config["sha256"], ["delta"])
        repo = RunRepository(store, ArtifactStore(self.installation / "artifacts"))
        self.run, _ = repo.create(
            self.pid, "recovery", {}, {"documents": [], "catalogs": [], "configuration_refs": []}
        )
        self.artifact = repo.add_artifact(
            self.pid, self.run, "synthetic.md", b"IMMUTABLE ARTIFACT", {}
        )
        repo.set_status(self.pid, self.run, "succeeded")
        store.close()
        (self.installation / "bootstrap.token").write_text("SESSION_TOKEN_NOT_IN_BACKUP")
        (self.installation / "arbitrary-unreferenced.txt").write_text("UNREFERENCED_NOT_IN_BACKUP")

    def tearDown(self):
        self.temporary.cleanup()

    def backup(self):
        return create_backup(self.installation, self.output, self.backups)

    def test_consistent_restore_history_hashes_and_disabled_new_heads(self):
        result = self.backup()
        self.assertFalse(result["manifest"]["encrypted"])
        with tarfile.open(self.output) as archive:
            self.assertNotIn("bootstrap.token", archive.getnames())
            self.assertNotIn("arbitrary-unreferenced.txt", archive.getnames())
            self.assertFalse(any("secrets" in name for name in archive.getnames()))
        target = self.root / "restored"
        restored = restore_backup(target, self.output, self.backups)
        self.assertTrue(restored["restored"])
        active = active_data(target)
        self.assertNotEqual(active, target)
        store = Store(active / "app.sqlite3")
        self.assertEqual(
            json.loads(
                store.connection.execute(
                    "SELECT value FROM instance_settings WHERE key='bootstrap_configuration'"
                ).fetchone()[0]
            )["status"],
            "restored_requires_explicit_import",
        )
        from spec_enricher.startup import initialize

        with patch(
            "spec_enricher.startup._read",
            side_effect=AssertionError("Restore must skip startup import"),
        ):
            initialize(store, self.root / "config")
        self.assertEqual(store.document(self.pid, self.document)[2], b"PRIVATE SYNTHETIC A\n")
        self.assertIsNone(store.document(self.other, self.document))
        registry = Registry(store)
        self.assertEqual(
            registry.get(self.pid, "connection", self.config["id"], 1)["sha256"],
            self.config["sha256"],
        )
        head = registry.get(self.pid, "connection", self.config["id"])
        self.assertFalse(head["content"]["enabled"])
        self.assertEqual(head["granted_scopes"], [])
        self.assertIsNone(head["capability_report"])
        self.assertEqual(
            ArtifactStore(active / "artifacts").read(self.pid, self.artifact["sha256"]),
            b"IMMUTABLE ARTIFACT",
        )
        store.close()
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.backup()

    def rewrite(self, filename, *, manifest_change=None, extra=None, corrupt=False):
        destination = self.backups / filename
        with tarfile.open(self.output) as original, tarfile.open(destination, "w:gz") as archive:
            for member in original:
                content = original.extractfile(member).read()
                if member.name == "manifest.json" and manifest_change:
                    value = json.loads(content)
                    manifest_change(value)
                    content = json.dumps(value).encode()
                elif corrupt and member.name == "database.sqlite3":
                    content = content[:-1] + bytes([content[-1] ^ 1])
                entry = tarfile.TarInfo(member.name)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            if extra is not None:
                archive.addfile(extra)
        return destination

    def test_unsafe_future_corrupt_duplicate_and_incomplete_rejected_before_switch(self):
        self.backup()
        link = tarfile.TarInfo("linked")
        link.type = tarfile.SYMTYPE
        link.linkname = "/tmp/escaped"
        traversal = tarfile.TarInfo("../escaped")
        bad = [
            self.rewrite("corrupt.tar.gz", corrupt=True),
            self.rewrite(
                "future.tar.gz",
                manifest_change=lambda x: x.update(schema_version=SCHEMA_VERSION + 1),
            ),
            self.rewrite("link.tar.gz", extra=link),
            self.rewrite("traversal.tar.gz", extra=traversal),
            self.rewrite(
                "duplicate.tar.gz", manifest_change=lambda x: x["files"].append(x["files"][0])
            ),
        ]
        for index, source in enumerate(bad):
            target = self.root / ("rejected-" + str(index))
            with self.subTest(source=source.name), self.assertRaises(ValueError):
                restore_backup(target, source, self.backups)
            self.assertFalse((target / "CURRENT").exists())
        self.assertFalse((self.root / "escaped").exists())

    def test_service_lock_maintenance_and_interrupted_atomic_switch(self):
        self.backup()
        with Lease(self.installation, ".service.lock", exclusive=False):
            with self.assertRaises(ValueError):
                restore_backup(self.installation, self.output, self.backups, replace_existing=True)
        store = Store(self.installation / "app.sqlite3")
        maintenance(store, True)
        with self.assertRaises(sqlite3.IntegrityError):
            store.create_project("Blocked during backup")
        maintenance(store, False)
        store.close()
        before = file_hash(self.installation / "app.sqlite3")
        with patch(
            "spec_enricher.backup.switch_data", side_effect=OSError("synthetic interruption")
        ):
            with self.assertRaises(OSError):
                restore_backup(self.installation, self.output, self.backups, replace_existing=True)
        self.assertEqual(active_data(self.installation), self.installation)
        store = Store(self.installation / "app.sqlite3")
        self.assertEqual(store.document(self.pid, self.document)[2], b"PRIVATE SYNTHETIC A\n")
        store.close()
        self.assertTrue(list(self.backups.glob("pre-restore-*.tar.gz")))
        self.assertEqual(before[0], file_hash(self.installation / "app.sqlite3")[0])
