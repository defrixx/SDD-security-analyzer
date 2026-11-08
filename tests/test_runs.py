import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spec_enricher.artifacts import ArtifactStore
from spec_enricher.runs import RunConflict, RunRepository
from spec_enricher.storage import Store


class RunTests(unittest.TestCase):
    def test_immutable_artifacts_scope_idempotency_crash_and_disk_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "app.db")
            a, b = store.create_project("A"), store.create_project("B")
            files = ArtifactStore(root / "artifacts")
            repo = RunRepository(store, files)
            manifest = {"documents": [], "catalogs": [], "configuration_refs": []}
            run, created = repo.create(a, "key-1", {"delta": "x"}, manifest)
            self.assertTrue(created)
            self.assertEqual(repo.create(a, "key-1", {"delta": "x"}, manifest), (run, False))
            with self.assertRaises(RunConflict):
                repo.create(a, "key-1", {"delta": "changed"}, manifest)
            with self.assertRaises(RunConflict):
                repo.create(b, "key-2", {}, manifest)
            item = repo.add_artifact(a, run, "synthetic", b"Original bytes\r\n", {})
            self.assertIsNone(repo.artifact(b, run, item["id"], body=True))
            self.assertEqual(
                repo.artifact(a, run, item["id"], body=True)["body"], b"Original bytes\r\n"
            )
            self.assertIsNone(repo.cached(b, "key"))
            repo.cache(a, "key", item["id"])
            self.assertEqual(repo.cached(a, "key")["body"], b"Original bytes\r\n")
            repo.interrupt_abandoned()
            self.assertEqual(repo.get(a, run)["status"], "interrupted")
            with (
                patch.object(files, "put", side_effect=OSError("synthetic disk full")),
                self.assertRaises(OSError),
            ):
                repo.create(a, "key-3", {}, manifest)
            failed = repo.list_runs(a)[0]
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error_code"], "snapshot_write_failed")
            store.close()

    def test_artifact_symlink_traversal_and_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "app.db")
            a, b = store.create_project("A"), store.create_project("B")
            files = ArtifactStore(root / "artifacts")
            checksum = files.put(a, b"A")
            self.assertEqual(files.put(a, b"A"), checksum)
            with self.assertRaises((ValueError, OSError)):
                files.read(a, "../../outside")
            os.symlink(root / "artifacts" / a, root / "artifacts" / b)
            with self.assertRaises(OSError):
                files.read(b, checksum)
            (root / "artifacts" / a / checksum).write_bytes(b"Changed")
            with self.assertRaises(ValueError):
                files.read(a, checksum)
            store.close()
