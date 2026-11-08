import tempfile
import unittest
from pathlib import Path

from spec_enricher.builtin import load_verified


class BuiltinTests(unittest.TestCase):
    def test_accepted_catalog_and_actual_cited_snapshots_match(self):
        _, value = load_verified()
        self.assertEqual(len(value["requirements"]), 821)

    def test_changed_catalog_does_not_reuse_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "catalog").mkdir()
            (root / "catalog/requirements.full.json").write_bytes(b'{"ready_for_use":true}')
            with self.assertRaises(ValueError):
                load_verified(root)
