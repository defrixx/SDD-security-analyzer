"""Exact-payload, per-request consent for step mode, persisted as run artifacts.

Only the authenticated resume API creates approval artifacts. Documents/models
cannot write this ledger. A numbered actual request consumes its exact approval;
retry/repair payloads require their own preview, including after crash/resume.
"""

from __future__ import annotations

import json

from .catalog import canonical, digest


class CallApprovalRequired(Exception):
    pass


def request_ordinal(repo, project_id, run_id, logical_key):
    return (
        1
        + repo.store.connection.execute(
            "SELECT count(*) FROM artifacts WHERE project_id=? AND run_id=? AND kind='model.request' AND json_extract(metadata,'$.logical_call_key')=?",
            (project_id, run_id, logical_key),
        ).fetchone()[0]
    )


def identity(repo, project_id, run_id, logical_key, payload):
    ordinal = request_ordinal(repo, project_id, run_id, logical_key)
    return ordinal, digest(
        {
            "logical_call_key": logical_key,
            "payload_sha256": digest(payload),
            "request_ordinal": ordinal,
        }
    )


def pending_call(repo, project_id, run_id, *, logical_key=None, body=False):
    for (artifact_id,) in repo.store.connection.execute(
        "SELECT id FROM artifacts WHERE project_id=? AND run_id=? AND kind='model.call_preview' ORDER BY rowid DESC",
        (project_id, run_id),
    ):
        artifact = repo.artifact(project_id, run_id, artifact_id)
        metadata = artifact["metadata"]
        key = metadata["logical_call_key"]
        if logical_key is not None and logical_key != key:
            continue
        if metadata["request_ordinal"] != request_ordinal(repo, project_id, run_id, key):
            continue
        if body:
            return json.loads(repo.artifact(project_id, run_id, artifact_id, body=True)["body"])
        return {"artifact_id": artifact_id, "sha256": artifact["sha256"], **metadata}
    return None


def require_approval(
    repo, project_id, run_id, logical_key, payload, retry_state, connection, stage
):
    ordinal, key = identity(repo, project_id, run_id, logical_key, payload)
    for (artifact_id,) in repo.store.connection.execute(
        "SELECT id FROM artifacts WHERE project_id=? AND run_id=? AND kind='model.call_approval' AND json_extract(metadata,'$.call_key')=? ORDER BY rowid DESC",
        (project_id, run_id, key),
    ):
        approved = json.loads(repo.artifact(project_id, run_id, artifact_id, body=True)["body"])
        preview = repo.artifact(project_id, run_id, approved["preview_artifact_id"], body=True)
        if preview is None or preview["sha256"] != approved["preview_sha256"]:
            raise ValueError("call_approval_preview_hash_conflict")
        frozen = json.loads(preview["body"])
        if frozen["call_key"] == key and frozen["payload_sha256"] == digest(payload):
            return ordinal, key
    metadata = {
        "logical_call_key": logical_key,
        "call_key": key,
        "request_ordinal": ordinal,
        "payload_sha256": digest(payload),
        "stage": stage,
        "destination": connection["content"]["endpoint"],
        "connection_id": connection["id"],
        "connection_revision": connection["revision"],
    }
    prior = pending_call(repo, project_id, run_id, logical_key=logical_key)
    if prior is None or prior["call_key"] != key:
        repo.add_artifact(
            project_id,
            run_id,
            "model.call_preview",
            canonical({**metadata, "payload": payload, "retry_state": retry_state}),
            metadata,
        )
    raise CallApprovalRequired("exact_payload_confirmation_required")


def approve(repo, project_id, run_id, preview_id, expected_sha256):
    current = pending_call(repo, project_id, run_id)
    if (
        current is None
        or current["artifact_id"] != preview_id
        or current["sha256"] != expected_sha256
    ):
        raise ValueError("call_preview_missing_or_changed")
    # Read/hash verification happens before accepting a reference as consent.
    preview = repo.artifact(project_id, run_id, preview_id, body=True)
    if preview["sha256"] != expected_sha256:
        raise ValueError("call_preview_hash_conflict")
    return repo.add_artifact(
        project_id,
        run_id,
        "model.call_approval",
        canonical(
            {
                "call_key": current["call_key"],
                "preview_artifact_id": preview_id,
                "preview_sha256": expected_sha256,
                "decision": "user_confirmed_exact_payload",
            }
        ),
        {
            "call_key": current["call_key"],
            "preview_artifact_id": preview_id,
            "preview_sha256": expected_sha256,
        },
    )
