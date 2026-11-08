import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from spec_enricher.storage import Store


class CatalogStorageTests(unittest.TestCase):
    def test_v12_migration_preserves_history_and_monotonic_registry_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            store = Store(path)
            with store.connection:
                store.connection.execute(
                    "INSERT INTO user_catalogues(id,name) VALUES('synthetic-owner','Own policies')"
                )
                store.connection.executemany(
                    "INSERT INTO user_catalogue_versions(user_catalogue_id,revision,content,sha256) VALUES('synthetic-owner',?,X'7b7d','synthetic-hash')",
                    [(2,), (9,)],
                )
            store.connection.executescript(
                "DROP TABLE catalog_updates; DROP INDEX requirement_identity_lookup; DROP TABLE purge_jobs; ALTER TABLE user_catalogues DROP COLUMN next_revision; PRAGMA user_version=12;"
            )
            store.close()
            migrated = Store(path)
            self.assertEqual(
                migrated.connection.execute(
                    "SELECT next_revision FROM user_catalogues WHERE id='synthetic-owner'"
                ).fetchone()[0],
                10,
            )
            self.assertEqual(migrated.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            migrated.close()
            backup = sqlite3.connect(
                next((Path(directory) / "migration-backups").glob("*.sqlite3"))
            )
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 12)
            self.assertEqual(
                backup.execute("SELECT max(revision) FROM user_catalogue_versions").fetchone()[0], 9
            )
            backup.close()

    def test_binding_idempotency_isolation_disable_history_and_conflict(self):
        catalog = json.loads(Path("templates/requirements.example.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "db")
            a, b = store.create_project("A"), store.create_project("B")
            cid = store.bind_catalog(a, catalog)
            self.assertEqual(cid, store.bind_catalog(a, catalog))
            self.assertEqual(len(store.catalogs(a)), 1)
            self.assertEqual(store.catalogs(b), [])
            self.assertIsNone(store.catalog(b, cid))
            self.assertEqual(store.requirements(b, cid), [])
            store.set_catalog_binding(a, cid, remove=True)
            self.assertIsNotNone(store.catalog(a, cid))
            self.assertIsNone(store.catalogs(a)[0][5])
            changed = copy.deepcopy(catalog)
            changed["title"] += " changed"
            with self.assertRaises(ValueError):
                store.bind_catalog(a, changed)
            self.assertEqual(store.catalog(a, cid), catalog)
            store.close()

    def test_v1_migration_preserves_document_and_rejects_future_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db"
            con = sqlite3.connect(path)
            con.executescript("""
              CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL);
              CREATE TABLE documents(project_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
                kind TEXT NOT NULL, content BLOB NOT NULL, sha256 TEXT NOT NULL,
                PRIMARY KEY(project_id,id), FOREIGN KEY(project_id) REFERENCES projects(id));
              INSERT INTO projects VALUES('old','Original','');
              INSERT INTO documents VALUES('old','doc','x.md','baseline',X'616263','hash-preserved');
              PRAGMA user_version=1;
            """)
            con.close()
            store = Store(path)
            self.assertEqual(store.document("old", "doc")[2:], (b"abc", "hash-preserved"))
            backup_files = list((Path(directory) / "migration-backups").glob("*.sqlite3"))
            self.assertEqual(len(backup_files), 1)
            backup = sqlite3.connect(backup_files[0])
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(backup.execute("SELECT content FROM documents").fetchone()[0], b"abc")
            backup.close()
            store.connection.execute("PRAGMA user_version=999")
            store.close()
            with self.assertRaises(ValueError):
                Store(path)
