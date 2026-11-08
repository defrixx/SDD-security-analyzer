"""Actual handoff input bytes through the API; no LLM and no oracle access."""

import json
import tempfile
import unittest
from pathlib import Path

from handoff_fixture_plan import REQUIREMENT_IDS, catalogue, inputs
from real_harness import RealHarness

ROOT = Path(__file__).resolve().parents[1]


class HandoffIngestionTests(unittest.TestCase):
    def test_exact_current_records_and_supplied_inputs_are_project_scoped(self):
        full = json.loads((ROOT / "catalog/requirements.full.json").read_bytes())
        policy, data = catalogue(ROOT), inputs(ROOT)
        current = {r["id"]: r for r in full["requirements"]}
        self.assertEqual(set(REQUIREMENT_IDS), {r["id"] for r in policy["requirements"]})
        for record in policy["requirements"]:
            self.assertEqual(record, current[record["id"]])
        self.assertEqual(policy["scope"], "user_catalogue")
        self.assertTrue(all("expected" not in name and "oracle" not in name for name in data))
        with (
            tempfile.TemporaryDirectory() as directory,
            RealHarness(
                Path(directory), "http://host.docker.internal:1234/v1", "unused-no-model-call"
            ) as harness,
        ):
            a, b = harness.project("Actual input A"), harness.project("Actual input B")
            cid = harness.catalog(a, policy)
            for name, raw in data.items():
                pid = b if name == "project-b/master.md" else a
                role = (
                    "delta"
                    if ".delta." in name
                    else "context"
                    if name.startswith("documents/")
                    else "master_spec"
                )
                doc = harness.document(pid, Path(name).name, raw, role)
                response = harness.response(f"/projects/{pid}/documents/{doc}")
                self.assertEqual(response.content, raw, name)
                other = a if pid == b else b
                self.assertEqual(
                    harness.response(f"/projects/{other}/documents/{doc}").status_code, 404
                )
            self.assertEqual(harness.api(f"/projects/{b}/connections"), [])
            self.assertEqual(harness.api(f"/projects/{a}/connections"), [])
            preview = harness.api(f"/projects/{a}/catalogs/{cid}/binding-preview")
            harness.api(
                f"/projects/{a}/catalogs/{cid}/binding",
                {"remove": True, "expected_preview_sha256": preview["sha256"]},
                method="PATCH",
            )
            self.assertEqual(
                harness.response(f"/projects/{b}/catalogs/{cid}/coverage").status_code, 404
            )
            self.assertEqual(
                harness.response(f"/projects/{a}/catalogs/{cid}/coverage").status_code, 200
            )


if __name__ == "__main__":
    unittest.main()
