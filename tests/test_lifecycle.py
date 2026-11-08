import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from spec_enricher.artifacts import ArtifactStore
from spec_enricher.lifecycle import Lifecycle
from spec_enricher.registry import RevisionConflict
from spec_enricher.runs import RunRepository
from spec_enricher.storage import Store


class LifecycleTests(unittest.TestCase):
    def test_project_purge_collects_crash_orphans_and_later_logical_backup_has_no_deleted_payload(
        self,
    ):
        orphan = self.files.put(self.a, b"Unreferenced artifact A")
        export = ArtifactStore(self.root / "exports").put(self.a, b"Unreferenced export A")
        pending = ".pending-" + str(uuid.uuid4())
        temporary_path = self.root / "artifacts" / self.a / pending
        temporary_path.write_bytes(b"Crash-interrupted A")
        preview = self.lifecycle.preview(self.a, "project", self.a)
        self.assertEqual(preview["counts"]["abandoned_temporary_files"], 1)
        self.assertIn(orphan, preview["artifact_hashes"])
        self.assertIn(export, preview["export_hashes"])
        result = self.lifecycle.purge(self.a, "project", self.a, preview["sha256"])
        self.assertEqual(result["status"], "complete")
        self.assertFalse(temporary_path.exists())
        self.assertFalse((self.root / "exports" / self.a / export).exists())
        self.assertFalse((self.root / "artifacts" / self.a / orphan).exists())
        plan = self.store.connection.execute(
            "SELECT plan FROM purge_jobs WHERE id=?", (result["id"],)
        ).fetchone()[0]
        self.assertNotIn(orphan, plan)
        logical = sqlite3.connect(self.root / "logical-backup")
        self.store.connection.backup(logical)
        logical.close()
        self.assertNotIn(b"Private A", (self.root / "logical-backup").read_bytes())
        self.assertIsNotNone(self.store.document(self.b, self.other))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "db")
        self.a, self.b = self.store.create_project("A"), self.store.create_project("B")
        self.files = ArtifactStore(self.root / "artifacts")
        self.repo = RunRepository(self.store, self.files)
        self.lifecycle = Lifecycle(self.store, self.files, self.root / "exports")
        self.doc = self.store.add_document(self.a, "a.md", "baseline", b"Private A")
        self.other = self.store.add_document(self.b, "b.md", "baseline", b"Private B")

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def run_with_doc(self, key):
        rid, _ = self.repo.create(
            self.a,
            key,
            {},
            {
                "documents": [{"id": self.doc, "role": "master_spec"}],
                "catalogs": [],
                "configuration_refs": [],
            },
        )
        return rid

    def purge(self, kind, oid):
        preview = self.lifecycle.preview(self.a, kind, oid)
        return self.lifecycle.purge(self.a, kind, oid, preview["sha256"])

    def test_references_active_worker_and_stale_preview_guard_project_isolation(self):
        rid = self.run_with_doc("first")
        preview = self.lifecycle.preview(self.a, "project", self.a)
        self.assertTrue(preview["active_runs"])
        with self.assertRaises(RevisionConflict):
            self.lifecycle.purge(self.a, "project", self.a, preview["sha256"])
        self.repo.set_status(self.a, rid, "succeeded")
        plan = self.lifecycle.preview(self.a, "document", self.doc)
        self.assertEqual(plan["dependent_run_ids"], [rid])
        with self.assertRaises(RevisionConflict):
            self.lifecycle.purge(self.a, "document", self.doc, plan["sha256"])
        with self.assertRaises(LookupError):
            self.lifecycle.preview(self.b, "document", self.doc)
        plan = self.lifecycle.preview(self.a, "run", rid)
        self.store.audit(self.a, "synthetic_concurrent_change", rid)
        with self.assertRaises(RevisionConflict):
            self.lifecycle.purge(self.a, "run", rid, plan["sha256"])
        self.assertEqual(self.purge("run", rid)["status"], "complete")
        self.assertEqual(self.purge("document", self.doc)["status"], "complete")
        self.assertIsNotNone(self.store.document(self.b, self.other))

    def test_deduplicated_hash_reference_gc_retry_and_foreign_scope(self):
        first = self.run_with_doc("first")
        shared = self.repo.add_artifact(self.a, first, "synthetic", b"shared immutable A", {})
        self.repo.set_status(self.a, first, "succeeded")
        second = self.run_with_doc("second")
        self.repo.add_artifact(self.a, second, "synthetic", b"shared immutable A", {})
        unique = self.repo.add_artifact(self.a, second, "synthetic", b"only second", {})
        self.repo.set_status(self.a, second, "succeeded")
        other_hash = self.files.put(self.b, b"B preserved")
        # Simulate disk permissions/full filesystem after transactional metadata
        # deletion. A pending receipt is explicit, retry is scoped and idempotent.
        with patch.object(self.lifecycle, "_unlink", side_effect=OSError("synthetic disk failure")):
            pending = self.purge("run", second)
        self.assertEqual(pending["status"], "pending")
        with self.assertRaises(RevisionConflict):
            self.lifecycle.preview(self.a, "project", self.a)
        with self.assertRaises(LookupError):
            self.lifecycle.retry(self.b, pending["id"])
        self.assertEqual(self.lifecycle.retry(self.a, pending["id"])["status"], "complete")
        self.assertEqual(self.files.read(self.a, shared["sha256"]), b"shared immutable A")
        self.assertFalse((self.root / "artifacts" / self.a / unique["sha256"]).exists())
        self.assertEqual(self.files.read(self.b, other_hash), b"B preserved")
        self.assertEqual(self.lifecycle.retry(self.a, pending["id"])["status"], "complete")
        self.assertEqual(self.purge("project", self.a)["status"], "complete")
        self.assertEqual(self.store.projects()[0][0], self.b)
        self.assertEqual(self.files.read(self.b, other_hash), b"B preserved")
        self.assertEqual(self.store.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_catalogue_disable_remove_readd_and_purge_keep_history_until_explicit(self):
        catalogue = json.loads(Path("templates/requirements.example.json").read_text())
        cid = self.store.bind_catalog(self.a, catalogue)
        rid, _ = self.repo.create(
            self.a,
            "catalogue",
            {},
            {"documents": [], "catalogs": [{"id": cid}], "configuration_refs": []},
        )
        self.repo.set_status(self.a, rid, "succeeded")
        before = self.lifecycle.binding_preview(self.a, cid)
        self.assertEqual(before["pinned_run_count"], 1)
        self.store.set_catalog_binding(self.a, cid, remove=True)
        self.assertIsNotNone(self.store.catalog(self.a, cid))
        self.assertTrue(self.lifecycle.preview(self.a, "catalog", cid)["blockers"])
        self.store.set_catalog_binding(self.a, cid, enabled=True)
        self.assertEqual(self.store.catalogs(self.a)[0][5], 1)
        self.purge("run", rid)
        self.store.set_catalog_binding(self.a, cid, enabled=False)
        self.assertEqual(self.purge("catalog", cid)["status"], "complete")
        self.assertIsNone(self.store.catalog(self.a, cid))

    def test_gc_symlink_never_follows_outside_and_maintenance_blocks_commit(self):
        rid = self.run_with_doc("first")
        artifact = self.repo.add_artifact(self.a, rid, "synthetic", b"private", {})
        self.repo.set_status(self.a, rid, "succeeded")
        path = self.root / "artifacts" / self.a / artifact["sha256"]
        path.unlink()
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        path.symlink_to(outside)
        result = self.purge("run", rid)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(outside.read_bytes(), b"untouched")
        path.unlink()
        self.assertEqual(self.lifecycle.retry(self.a, result["id"])["status"], "complete")
        plan = self.lifecycle.preview(self.a, "project", self.a)
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE instance_settings SET value='true' WHERE key='maintenance'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.lifecycle.purge(self.a, "project", self.a, plan["sha256"])
        self.assertIsNotNone(self.store.document(self.a, self.doc))
