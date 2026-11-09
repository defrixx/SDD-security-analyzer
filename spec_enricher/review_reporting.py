"""Independent, pinned documentary reports; never mutate source documents."""

from __future__ import annotations

import io
import json
import uuid
import zipfile

from pydantic import Field

from .artifacts import ArtifactStore
from .catalog import canonical, digest
from .exporting import latest_output
from .runs import RunConflict, now
from .stage_contracts import Contract


class ReportExportInput(Contract):
    expected_review_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=100)


def review_report(repo, project_id, run_id):
    run = repo.get(project_id, run_id)
    if run is None:
        raise LookupError("run_not_found")
    manifest = run["manifest"]
    if manifest.get("request", {}).get("mode") != "review_documents":
        raise RunConflict("document_review_mode_required")
    output = latest_output(repo, project_id, run_id, "stage.P6.output")
    if output is None:
        raise RunConflict("report_not_ready")
    # Read immutable P6, not mutable documents or today's catalog.
    matrix = []
    for row in output.get("matrix", []):
        assessment = row.get("document", row.get("documentary_assessment", row.get("proposed", row.get("assessment"))))
        matrix.append({"requirement_id": row["requirement_id"], "version": row.get("version"),
                       "assessment": assessment})
    findings = output.get("findings", [])
    for finding in findings:
        finding["assessment"] = finding.pop("proposed_assessment", finding.get("assessment"))
        finding.pop("baseline_assessment", None)
        finding.pop("change_effect", None)
        finding["implementation_verification"] = "not_verified"
    decisions = repo.store.connection.execute(
        "SELECT target_id,decision,comment,revision FROM review_decisions WHERE project_id=? AND run_id=? AND target_kind='finding' ORDER BY revision",
        (project_id, run_id),
    ).fetchall()
    by_id = {finding["id"]: finding for finding in findings}
    for target, state, comment, revision in decisions:
        if target in by_id:
            by_id[target].update(review_state=state, review_comment=comment, review_revision=revision)
    unknown = sum(isinstance(row["assessment"], dict) and row["assessment"].get("assessment") in {"unknown", "not_assessed"} for row in matrix)
    expected_requirements = len(manifest.get("requirement_ownership", {}))
    incomplete = bool(output.get("incomplete")) or len(matrix) < expected_requirements or any(
        not isinstance(row["assessment"], dict) or row["assessment"].get("incomplete", False)
        for row in matrix
    )
    return {
        "schema_version": "1.0", "mode": "review_documents", "scope": "selected_documents",
        "project_id": project_id, "run_id": run_id, "run_status": run["status"],
        "review_revision": run["review_revision"], "matrix": matrix, "findings": findings,
        "review_decisions_supported": False,
        "scope_confirmations": output.get("scope_confirmations", []),
        "questions": list(dict.fromkeys(output.get("questions", []))),
        "incomplete": incomplete, "unknown_requirements": unknown,
        "coverage": {"rows": len(matrix), "active_requirements": expected_requirements},
        "implementation_verification": "not_verified",
        "notice": "Documentary assessment only. Unknown or incomplete is not a pass. The implementation has not been verified. Model output requires human review.",
        "documents": manifest.get("documents", []), "catalogs": manifest.get("catalogs", []),
        "source_snapshots": manifest.get("source_snapshots", []),
        "manifest_sha256": digest(manifest), "limitations": manifest.get("limitations", []),
    }


def report_markdown(report):
    lines = ["# Document security review", "", report["notice"], "",
             f"Run: {report['run_id']}; status: {report['run_status']}; review revision: {report['review_revision']}",
             f"Incomplete: {report['incomplete']}; unknown/not assessed: {report['unknown_requirements']}",
             "", "## Requirements", ""]
    for row in report["matrix"]:
        assessment = row["assessment"] or {}
        lines.append(f"- {row['requirement_id']} ({row['version']}): {assessment.get('assessment', 'unknown')}; applicability: {assessment.get('applicability', 'unknown')}")
    lines += ["", "## Findings", ""]
    for finding in report["findings"]:
        lines += [f"- {finding['id']}: {finding.get('type', '')}; {finding.get('review_state', 'unreviewed')}",
                  "  " + str(finding.get("explanation", "")),
                  "  " + str(finding.get("suggested_resolution", ""))]
    lines += ["", "## Questions", ""] + ["- " + str(q) for q in report["questions"]]
    lines += ["", "## Pinned sources", "", "Full source references, hashes and evidence are in review.json and run-manifest.json.",
              "Manifest SHA-256: " + report["manifest_sha256"]]
    for document in report["documents"]:
        lines.append("- " + json.dumps(document, ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode()


def export_report(repo, project_id, run_id, request, export_root, *, validate_content=None):
    with repo.artifacts.namespace(project_id):
        return _export_report(repo, project_id, run_id, request, export_root, validate_content)


def _export_report(repo, project_id, run_id, request, export_root, validate_content):
    checksum = digest({"kind": "document_review_report", **request.model_dump()})
    existing = repo.store.connection.execute(
        "SELECT id,request_sha256,metadata FROM exports WHERE project_id=? AND run_id=? AND request_key=?",
        (project_id, run_id, request.idempotency_key),
    ).fetchone()
    if existing:
        if checksum != existing[1]:
            raise RunConflict("export_idempotency_conflict")
        return {"id": existing[0], **json.loads(existing[2]), "replayed": True}
    report = review_report(repo, project_id, run_id)
    if report["review_revision"] != request.expected_review_revision:
        raise RunConflict("review_revision_conflict")
    export_id = str(uuid.uuid4())
    manifest = repo.get(project_id, run_id)["manifest"] | {
        "run_id": run_id, "export_id": export_id, "exported_at": now(),
        "review_revision": report["review_revision"], "export_kind": "document_review_report",
    }
    files = {"review.json": canonical(report), "review.md": report_markdown(report),
             "run-manifest.json": canonical(manifest)}
    if validate_content:
        for content in files.values():
            validate_content(content)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    bundle = buffer.getvalue()
    destination = ArtifactStore(export_root).put(project_id, bundle)
    artifact = repo.add_artifact(project_id, run_id, "export.report.zip", bundle,
                                 {"export_id": export_id, "review_revision": report["review_revision"]})
    metadata = {"artifact_id": artifact["id"], "sha256": destination,
                "filename": "document-review-" + export_id + ".zip", "files": list(files),
                "sensitive": True, "implementation_verification": "not_verified"}
    with repo.store.connection:
        repo.store.connection.execute("BEGIN IMMEDIATE")
        current = repo.store.connection.execute(
            "SELECT review_revision FROM runs WHERE project_id=? AND id=?", (project_id, run_id)
        ).fetchone()
        if current is None or current[0] != request.expected_review_revision:
            raise RunConflict("review_changed_during_export")
        repo.store.connection.execute(
            "INSERT INTO exports(project_id,id,run_id,review_revision,request_key,request_sha256,artifact_id,bundle_sha256,metadata) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, export_id, run_id, current[0], request.idempotency_key, checksum,
             artifact["id"], destination, json.dumps(metadata, ensure_ascii=False)),
        )
    repo.store.audit(project_id, "document_review_exported", export_id)
    return {"id": export_id, **metadata, "replayed": False}
