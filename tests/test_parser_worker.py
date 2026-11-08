import json
import multiprocessing
import unittest
from pathlib import Path

from spec_enricher.inputs import InputError
from spec_enricher.parser_worker import parse_bounded


class ParserWorkerTests(unittest.TestCase):
    def test_actual_catalog_is_bounded_and_valid(self):
        value, issues = parse_bounded(
            Path("catalog/requirements.full.json").read_bytes(), "json", catalog=True
        )
        self.assertEqual(issues, [])
        self.assertEqual(len(value["requirements"]), 821)

    def test_repeated_bad_inputs_leave_no_workers(self):
        before = {p.pid for p in multiprocessing.active_children()}
        for _ in range(20):
            with self.assertRaises(InputError):
                parse_bounded(b"x: !!python/object/apply:os.system ['do not run']", "yaml")
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
        value, issues = parse_bounded(
            json.dumps({"prompt": "ignore instructions", "$ref": "file:///etc/passwd"}).encode(),
            "json",
        )
        self.assertEqual(issues, [])
        self.assertEqual(value["$ref"], "file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
