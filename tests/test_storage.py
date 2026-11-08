import sqlite3
import tempfile
import unittest
from pathlib import Path

from spec_enricher.snapshots import sha256
from spec_enricher.storage import Store


class StorageTests(unittest.TestCase):
    def test_isolation_and_immutable_bytes_across_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            store = Store(path)
            a = store.create_project("A")
            b = store.create_project("B")
            data = "Документ A\r\n".encode()
            doc = store.add_document(a, "same.md", "baseline", data)
            store.add_document(b, "same.md", "baseline", b"document B")
            self.assertIsNone(store.document(b, doc))
            with self.assertRaises(sqlite3.IntegrityError):
                store.add_document("missing", "x.md", "baseline", b"x")
            store.close()
            store = Store(path)
            self.assertEqual(store.document(a, doc)[2:], (data, sha256(data)))
            self.assertEqual(len(store.documents(a)), 1)
            store.close()

    def test_size_and_encoding_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            project = store.create_project("A")
            with self.assertRaises(ValueError):
                store.add_document(project, "x.md", "delta", b"x" * (100 * 1024 + 1))
            with self.assertRaises(UnicodeDecodeError):
                store.add_document(project, "x.md", "baseline", b"\xff")
            self.assertEqual(store.documents(project), [])
            store.close()


if __name__ == "__main__":
    unittest.main()
