import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from spec_enricher.api import create_app

from spec_enricher.artifacts import ArtifactStore
from spec_enricher.catalog import canonical
from spec_enricher.review_reporting import ReportExportInput, export_report, review_report
from spec_enricher.runs import RunConflict, RunRepository
from spec_enricher.storage import Store


class ReviewReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "app.db")
        self.addCleanup(self.store.close)
        self.project = self.store.create_project("Review")
        self.other = self.store.create_project("Other")
        self.repo = RunRepository(self.store, ArtifactStore(self.root / "artifacts"))
        self.manifest = {"documents": [], "catalogs": [], "configuration_refs": [],
                         "request": {"mode": "review_documents"},
                         "source_snapshots": [{"id": "pinned-source", "raw_sha256": "a" * 64}]}
        self.run, _ = self.repo.create(self.project, "review", {}, self.manifest)

    def ready(self):
        output = {"matrix": [{"requirement_id": "R1", "version": "1.0",
                               "document": {"assessment": "unknown", "applicability": "unknown",
                                            "incomplete": True}}],
                  "findings": [{"id": "finding-1", "type": "uncertain_applicability",
                                "assessment": "unknown", "explanation": "Need design evidence",
                                "evidence_refs": [{"document_id": "source", "sha256": "b" * 64}]}],
                  "questions": ["How is access checked?"], "incomplete": True}
        self.repo.add_artifact(self.project, self.run, "stage.P6.output", canonical(output), {})

    def test_not_ready_and_scope_and_no_comparison(self):
        with self.assertRaises(RunConflict):
            review_report(self.repo, self.project, self.run)
        with self.assertRaises(LookupError):
            review_report(self.repo, self.other, self.run)
        self.ready()
        report = review_report(self.repo, self.project, self.run)
        self.assertEqual(report["unknown_requirements"], 1)
        self.assertTrue(report["incomplete"])
        self.assertEqual(report["matrix"][0]["assessment"]["assessment"], "unknown")
        self.assertNotIn("baseline", report["matrix"][0])
        self.assertNotIn("change_effect", report["findings"][0])
        self.assertEqual(report["source_snapshots"], self.manifest["source_snapshots"])
        self.assertEqual(report["implementation_verification"], "not_verified")

    def test_independent_export_replay_revision_secret_guard(self):
        self.ready()
        request = ReportExportInput(expected_review_revision=0, idempotency_key="download")
        checked = []
        result = export_report(self.repo, self.project, self.run, request, self.root / "exports",
                               validate_content=checked.append)
        self.assertEqual(len(checked), 3)
        artifact = self.repo.artifact(self.project, self.run, result["artifact_id"], body=True)
        with zipfile.ZipFile(io.BytesIO(artifact["body"])) as archive:
            self.assertEqual(set(archive.namelist()), {"review.json", "review.md", "run-manifest.json"})
            report = json.loads(archive.read("review.json"))
            self.assertEqual(report["implementation_verification"], "not_verified")
            self.assertIn(b"Unknown or incomplete is not a pass", archive.read("review.md"))
        self.assertTrue(export_report(self.repo, self.project, self.run, request, self.root / "exports")["replayed"])
        with self.assertRaises(RunConflict):
            export_report(self.repo, self.project, self.run,
                          ReportExportInput(expected_review_revision=1, idempotency_key="changed"), self.root / "exports")
        def reject(_):
            raise ValueError("known_credential")
        with self.assertRaises(ValueError):
            export_report(self.repo, self.project, self.run,
                          ReportExportInput(expected_review_revision=0, idempotency_key="secret"),
                          self.root / "exports", validate_content=reject)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM exports").fetchone()[0], 1)

    def test_export_detects_review_change_during_generation(self):
        self.ready()
        def change_revision(_):
            with self.store.connection:
                self.store.connection.execute("UPDATE runs SET review_revision=1 WHERE id=?", (self.run,))
        with self.assertRaises(RunConflict):
            export_report(self.repo, self.project, self.run,
                          ReportExportInput(expected_review_revision=0, idempotency_key="race"),
                          self.root / "exports", validate_content=change_revision)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM exports").fetchone()[0], 0)

    def test_authenticated_api_report_download_and_project_isolation(self):
        # A separate installation gives the API the canonical database filename.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = "http://127.0.0.1:8080"
            app = create_app(root, origin)
            with TestClient(app, base_url=origin) as client:
                headers = {"Origin": origin}
                login = client.post("/api/v1/session/bootstrap",
                                    json={"token": (root / "bootstrap.token").read_text()}, headers=headers)
                headers["X-CSRF-Token"] = login.json()["csrf_token"]
                store = Store(root / "app.sqlite3")
                try:
                    project, other = store.create_project("One"), store.create_project("Two")
                    repo = RunRepository(store, ArtifactStore(root / "artifacts"))
                    run, _ = repo.create(project, "api", {}, self.manifest)
                    prefix = f"/api/v1/projects/{project}/runs/{run}"
                    self.assertEqual(client.get(prefix + "/report").status_code, 409)
                    repo.add_artifact(project, run, "stage.P6.output",
                                      canonical({"matrix": [], "findings": [], "questions": []}), {})
                    self.assertEqual(client.get(prefix + "/report").status_code, 200)
                    self.assertEqual(client.get(f"/api/v1/projects/{other}/runs/{run}/report").status_code, 404)
                    body = {"expected_review_revision": 0, "idempotency_key": "api-export"}
                    self.assertEqual(client.post(prefix + "/report/exports", json=body).status_code, 403)
                    response = client.post(prefix + "/report/exports", json=body, headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    export_id = response.json()["id"]
                    download = client.get(prefix + "/exports/" + export_id)
                    self.assertEqual(download.status_code, 200)
                    self.assertIn("attachment", download.headers["content-disposition"])
                    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                        self.assertIn("review.json", archive.namelist())
                    self.assertEqual(client.get(f"/api/v1/projects/{other}/runs/{run}/exports/{export_id}").status_code, 404)
                finally:
                    store.close()
