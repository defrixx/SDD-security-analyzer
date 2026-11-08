"""Explicit selective export, immutable original and append-only review decisions."""

from __future__ import annotations

import difflib
import io
import json
import uuid
import zipfile
from pathlib import Path

from pydantic import Field

from .artifacts import ArtifactStore
from .catalog import canonical, digest
from .i18n import translate
from .runs import RunConflict, RunRepository, now
from .snapshots import Patch, apply, sha256
from .stage_contracts import Contract


class ExportInput(Contract):
    expected_review_revision: int = Field(ge=0)
    expected_original_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    accepted_patch_ids: list[str] = Field(max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=100)


class ProposalPreviewInput(Contract):
    expected_review_revision: int = Field(ge=0)
    expected_original_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    selected_patch_ids: list[str] = Field(max_length=100)
    edited_replacements: dict[str, str] = Field(default_factory=dict, max_length=100)


def selected_document_scope(repo, project_id, run_id, original, proposal, proposed):
    """Keep P8's complete-candidate assessment separate from a selective export."""
    patches = [
        Patch(item["id"], item["start"], item["end"], item["original"], item["replacement"])
        for item in proposal["patches"]
    ]
    candidate_hash = sha256(apply(original, sha256(original), patches, {p.id for p in patches}))
    recorded = proposal.get("candidate_artifact", {}).get("sha256")
    if recorded is not None and recorded != candidate_hash:
        raise RunConflict("candidate_hash_conflict")
    completed = bool(repo.list_artifacts(project_id, run_id, "review.json"))
    selected_hash = sha256(proposed)
    applies = completed and selected_hash == candidate_hash
    return {
        "generated_candidate_sha256": candidate_hash,
        "assessed_candidate_sha256": candidate_hash if completed else None,
        "selected_document_sha256": selected_hash,
        "p8_completed": completed,
        "p8_applies_to_selected_document": applies,
        "requires_new_analysis": not applies,
        "selected_document_assessment": None
        if applies
        else {
            "assessment": "not_assessed",
            "reason": "selection_differs_from_assessed_candidate"
            if completed
            else "P8_not_completed",
            "implementation_verification": "not_verified",
        },
    }


def preview_proposal(
    repo: RunRepository, project_id: str, run_id: str, request: ProposalPreviewInput
):
    run = repo.get(project_id, run_id)
    if run is None or run["review_revision"] != request.expected_review_revision:
        raise RunConflict("review_revision_conflict")
    proposal = latest_output(repo, project_id, run_id, "stage.P7.output")
    if proposal is None:
        raise ValueError("proposal_not_ready")
    original = repo.store.document(project_id, run["manifest"]["request"]["delta_document_id"])[2]
    if (
        sha256(original) != request.expected_original_sha256
        or proposal["original_sha256"] != request.expected_original_sha256
    ):
        raise RunConflict("original_hash_conflict")
    known = {item["id"] for item in proposal["patches"]}
    if (
        set(request.selected_patch_ids) - known
        or set(request.edited_replacements) - set(request.selected_patch_ids)
        or any(len(text.encode()) > 32000 for text in request.edited_replacements.values())
    ):
        raise ValueError("invalid_patch_preview_selection")
    patches = [
        Patch(
            item["id"],
            item["start"],
            item["end"],
            item["original"],
            request.edited_replacements.get(item["id"], item["replacement"]),
        )
        for item in proposal["patches"]
    ]
    proposed = apply(
        original, request.expected_original_sha256, patches, set(request.selected_patch_ids)
    )
    scope = selected_document_scope(repo, project_id, run_id, original, proposal, proposed)
    return {
        "original": original.decode(),
        "proposed": proposed.decode(),
        "diff": unified(original, proposed).decode(),
        "proposed_sha256": sha256(proposed),
        "selected_patch_ids": request.selected_patch_ids,
        "review_revision": run["review_revision"],
        "requires_new_analysis": bool(request.edited_replacements)
        or scope["requires_new_analysis"],
        "proposal_assessment_scope": scope,
    }


def latest_output(repo: RunRepository, project_id: str, run_id: str, kind: str):
    items = repo.list_artifacts(project_id, run_id, kind)
    if not items:
        return None
    return json.loads(repo.artifact(project_id, run_id, items[-1]["id"], body=True)["body"])


def unified(original: bytes, proposed: bytes):
    lines = difflib.unified_diff(
        original.decode().splitlines(keepends=True),
        proposed.decode().splitlines(keepends=True),
        fromfile="delta.original.md",
        tofile="delta.proposed.md",
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    ).encode()


def export_bundle(
    repo: RunRepository,
    project_id: str,
    run_id: str,
    request: ExportInput,
    export_root: Path,
    *,
    validate_content=None,
):
    with repo.artifacts.namespace(project_id):
        return _export_bundle(
            repo, project_id, run_id, request, export_root, validate_content=validate_content
        )


def _export_bundle(repo, project_id, run_id, request, export_root, *, validate_content=None):
    store = repo.store
    checksum = digest(request.model_dump())
    existing = store.connection.execute(
        "SELECT id,request_sha256,metadata FROM exports WHERE project_id=? AND run_id=? AND request_key=?",
        (project_id, run_id, request.idempotency_key),
    ).fetchone()
    if existing:
        if checksum != existing[1]:
            raise RunConflict("export_idempotency_conflict")
        return {"id": existing[0], **json.loads(existing[2]), "replayed": True}
    run = repo.get(project_id, run_id)
    if run is None or run["review_revision"] != request.expected_review_revision:
        raise RunConflict("review_revision_conflict")
    proposal = latest_output(repo, project_id, run_id, "stage.P7.output")
    if proposal is None:
        raise ValueError("proposal_not_ready")
    delta_id = run["manifest"]["request"]["delta_document_id"]
    original = store.document(project_id, delta_id)[2]
    if (
        sha256(original) != request.expected_original_sha256
        or proposal["original_sha256"] != request.expected_original_sha256
    ):
        raise RunConflict("original_hash_conflict")
    if len(request.accepted_patch_ids) != len(set(request.accepted_patch_ids)):
        raise ValueError("duplicate_patch_selection")
    selected = set(request.accepted_patch_ids)
    known = {patch["id"]: patch for patch in proposal["patches"]}
    if selected - set(known):
        raise ValueError("unknown_patch_selection")
    decisions = {}
    for row in store.connection.execute(
        "SELECT target_kind,target_id,decision,comment,revision FROM review_decisions WHERE project_id=? AND run_id=? ORDER BY revision",
        (project_id, run_id),
    ):
        decisions[(row[0], row[1])] = {"state": row[2], "comment": row[3], "revision": row[4]}
    if any(decisions.get(("patch", key), {}).get("state") != "accepted" for key in selected):
        raise RunConflict("selected_patch_requires_accepted_decision")
    patches = [
        Patch(item["id"], item["start"], item["end"], item["original"], item["replacement"])
        for item in proposal["patches"]
    ]
    proposed = apply(original, request.expected_original_sha256, patches, selected)
    scope = selected_document_scope(repo, project_id, run_id, original, proposal, proposed)
    report = latest_output(repo, project_id, run_id, "review.json") or {
        "matrix": [],
        "findings": [],
        "questions": ["P8 did not complete; this is a partial review."],
        "implementation_verification": "not_verified",
    }
    report.update(
        run_status=run["status"],
        review_revision=run["review_revision"],
        accepted_patch_ids=sorted(selected),
        omitted_patch_ids=sorted(set(known) - selected),
        original_sha256=sha256(original),
        proposed_sha256=sha256(proposed),
        proposal_assessment_scope=scope,
        requires_new_analysis=scope["requires_new_analysis"],
        document_status="draft_requires_new_analysis"
        if scope["requires_new_analysis"]
        else "draft_requires_review"
        if report.get("questions") or report.get("critique")
        else "proposed_design_not_runtime_verified",
    )
    for finding in report["findings"]:
        decision = decisions.get(("finding", finding["id"]))
        if decision:
            finding.update(review_state=decision["state"], review_comment=decision["comment"])
    lines = [
        "# Security review",
        "",
        translate("Реализация не проверялась. Документ — предлагаемая спецификация."),
        "",
        f"Run: {run_id}; status: {run['status']}; review revision: {run['review_revision']}",
        "",
        "## Findings",
        "",
    ]
    for finding in report["findings"]:
        ref = finding.get("requirement_ref") or {}
        lines.append(
            f"- {finding['id']}: {ref.get('id', 'delta')} · {finding['type']} · {finding['change_effect']} · {finding['review_state']}"
        )
    lines.extend(
        ["", "## Questions", ""]
        + ["- " + str(question) for question in report.get("questions", [])]
    )
    lines.extend(
        [
            "",
            "## Export selection",
            "",
            "Accepted: " + ", ".join(sorted(selected)),
            "Omitted: " + ", ".join(report["omitted_patch_ids"]),
            "",
            "P8 assessed candidate SHA-256: " + str(scope["assessed_candidate_sha256"]),
            "Selected document SHA-256: " + scope["selected_document_sha256"],
            "P8 applies to selected document: " + str(scope["p8_applies_to_selected_document"]),
            "Selected document requires new analysis: " + str(scope["requires_new_analysis"]),
        ]
    )
    export_id = str(uuid.uuid4())
    manifest = run["manifest"] | {
        "run_id": run_id,
        "export_id": export_id,
        "exported_at": now(),
        "review_revision": run["review_revision"],
        "selected_patch_ids": sorted(selected),
        "proposal_assessment_scope": scope,
    }
    files = {
        "delta.original.md": original,
        "delta.proposed.md": proposed,
        "review.json": canonical(report),
        "review.md": ("\n".join(lines) + "\n").encode(),
        "delta.patch": unified(original, proposed),
        "run-manifest.json": canonical(manifest),
    }
    if validate_content:
        for content in files.values():
            validate_content(content)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    bundle = buffer.getvalue()
    # Both roots are server-configured. Client filenames/paths are never used.
    destination = ArtifactStore(export_root).put(project_id, bundle)
    artifact = repo.add_artifact(
        project_id,
        run_id,
        "export.bundle.zip",
        bundle,
        {
            "export_id": export_id,
            "review_revision": run["review_revision"],
            "selected_patch_ids": sorted(selected),
        },
    )
    metadata = {
        "artifact_id": artifact["id"],
        "sha256": destination,
        "filename": "review-" + run_id + "-" + export_id + ".zip",
        "files": list(files),
        "accepted_patch_ids": sorted(selected),
        "requires_new_analysis": scope["requires_new_analysis"],
        "proposal_assessment_scope": scope,
        "sensitive": True,
    }
    with store.connection:
        current_revision = store.connection.execute(
            "SELECT review_revision FROM runs WHERE project_id=? AND id=?", (project_id, run_id)
        ).fetchone()[0]
        if current_revision != request.expected_review_revision:
            raise RunConflict("review_changed_during_export")
        store.connection.execute(
            "INSERT INTO exports(project_id,id,run_id,review_revision,request_key,request_sha256,artifact_id,bundle_sha256,metadata) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                project_id,
                export_id,
                run_id,
                current_revision,
                request.idempotency_key,
                checksum,
                artifact["id"],
                destination,
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
    store.audit(project_id, "review_exported", export_id)
    return {"id": export_id, **metadata, "replayed": False}
