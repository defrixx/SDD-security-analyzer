"""Persistent run lifecycle, immutable stage artifacts and project scope."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from .artifacts import ArtifactStore
from .catalog import canonical, digest
from .storage import Store


def now() -> str:
    return datetime.now(UTC).isoformat()


class RunConflict(ValueError):
    pass


class RunRepository:
    def __init__(self, store: Store, artifacts: ArtifactStore):
        self.store, self.artifacts = store, artifacts

    def create(
        self, project_id: str, request_key: str, request: dict, manifest: dict
    ) -> tuple[str, bool]:
        checksum = digest(request)
        existing = self.store.connection.execute(
            "SELECT id,request_sha256 FROM runs WHERE project_id=? AND request_key=?",
            (project_id, request_key),
        ).fetchone()
        if existing:
            from .run_contracts import matches_request_checksum
            if not matches_request_checksum(existing[1], request):
                raise RunConflict("idempotency_conflict")
            return existing[0], False
        run_id = str(uuid.uuid4())
        try:
            with self.store.connection:
                from .lifecycle import require_no_pending_purge

                require_no_pending_purge(self.store, project_id)
                self.store.connection.execute(
                    "INSERT INTO runs(project_id,id,status,manifest,request_key,request_sha256) VALUES(?,?,'queued',?,?,?)",
                    (
                        project_id,
                        run_id,
                        json.dumps(manifest, ensure_ascii=False),
                        request_key,
                        checksum,
                    ),
                )
                self.store.connection.executemany(
                    "INSERT INTO run_documents VALUES(?,?,?,?)",
                    [
                        (project_id, run_id, item["id"], item["role"])
                        for item in manifest["documents"]
                    ],
                )
                self.store.connection.executemany(
                    "INSERT INTO run_catalogs VALUES(?,?,?)",
                    [(project_id, run_id, item["id"]) for item in manifest["catalogs"]],
                )
                self.store.connection.executemany(
                    "INSERT INTO run_configs VALUES(?,?,?,?,?)",
                    [
                        (project_id, run_id, item["kind"], item["id"], item["revision"])
                        for item in manifest["configuration_refs"]
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise RunConflict("active_run_or_scope_conflict") from exc
        try:
            config = self.add_artifact(
                project_id, run_id, "configuration.snapshot", canonical(manifest), {}
            )
        except (OSError, ValueError, sqlite3.Error):
            self.set_status(project_id, run_id, "failed", "snapshot_write_failed")
            raise
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE runs SET configuration_snapshot_ref=? WHERE project_id=? AND id=?",
                (config["id"], project_id, run_id),
            )
        self.store.audit(project_id, "run_created", run_id)
        return run_id, True

    def get(self, project_id: str, run_id: str):
        row = self.store.connection.execute(
            "SELECT status,manifest,configuration_snapshot_ref,cancel_requested,review_revision,created_at,started_at,finished_at,error_code FROM runs WHERE project_id=? AND id=?",
            (project_id, run_id),
        ).fetchone()
        if row is None:
            return None
        return dict(
            zip(
                [
                    "status",
                    "manifest",
                    "configuration_snapshot_ref",
                    "cancel_requested",
                    "review_revision",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "error_code",
                ],
                [row[0], json.loads(row[1]), *row[2:]],
                strict=True,
            )
        ) | {"id": run_id, "project_id": project_id}

    def list_runs(self, project_id: str, limit: int = 50):
        ids = self.store.connection.execute(
            "SELECT id FROM runs WHERE project_id=? ORDER BY rowid DESC LIMIT ?",
            (project_id, min(limit, 100)),
        ).fetchall()
        return [
            {key: value for key, value in self.get(project_id, row[0]).items() if key != "manifest"}
            for row in ids
        ]

    def set_status(self, project_id: str, run_id: str, status: str, error_code: str | None = None):
        if status not in {
            "queued",
            "running",
            "waiting_approval",
            "succeeded",
            "partial",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise ValueError("invalid run state")
        with self.store.connection:
            if status == "running":
                self.store.connection.execute(
                    "UPDATE runs SET status=?,started_at=coalesce(started_at,?) WHERE project_id=? AND id=?",
                    (status, now(), project_id, run_id),
                )
            else:
                self.store.connection.execute(
                    "UPDATE runs SET status=?,finished_at=?,error_code=? WHERE project_id=? AND id=?",
                    (
                        status,
                        None if status in {"queued", "waiting_approval"} else now(),
                        error_code,
                        project_id,
                        run_id,
                    ),
                )

    def cancelled(self, project_id: str, run_id: str) -> bool:
        row = self.store.connection.execute(
            "SELECT cancel_requested FROM runs WHERE project_id=? AND id=?", (project_id, run_id)
        ).fetchone()
        return row is None or bool(row[0])

    def cancel(self, project_id: str, run_id: str):
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE runs SET cancel_requested=1 WHERE project_id=? AND id=? AND status IN ('queued','running','waiting_approval')",
                (project_id, run_id),
            )
            self.store.connection.execute(
                "UPDATE runs SET status='cancelled',finished_at=? WHERE project_id=? AND id=? AND status='waiting_approval'",
                (now(), project_id, run_id),
            )
        self.store.audit(project_id, "run_cancel_requested", run_id)

    def fill_unfinished_results(self, project_id, run_id, reason):
        from .evaluation import matrix_row

        run = self.get(project_id, run_id)
        if run is None:
            raise LookupError("run_not_found_in_project")
        with self.store.connection:
            for rid, cid in run["manifest"].get("requirement_ownership", {}).items():
                record = self.store.requirement(project_id, cid, rid)
                if record is None:
                    raise ValueError("pinned_requirement_missing")
                row = json.dumps(matrix_row(record, {}, {}, reason=reason), ensure_ascii=False)
                self.store.connection.execute(
                    "INSERT INTO requirement_results VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id,run_id,requirement_id) DO NOTHING",
                    (project_id, run_id, rid, cid, record["version"], row, row, "unresolved"),
                )

    def interrupt_abandoned(self):
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE runs SET status='interrupted',finished_at=?,error_code='process_restarted' WHERE status IN ('queued','running')",
                (now(),),
            )

    def checkpoint(self, project_id: str, run_id: str):
        completed = {}
        for stage in self.stages(project_id, run_id):
            if stage["status"] not in {"succeeded", "partial"}:
                continue
            for ref in reversed(stage["output_artifact_refs"]):
                artifact = self.artifact(project_id, run_id, ref)
                if (
                    artifact is not None
                    and artifact["kind"] == "stage." + stage["stage_id"] + ".output"
                ):
                    completed[stage["stage_id"]] = artifact
                    break
        prefix = []
        run = self.get(project_id, run_id)
        review = run["manifest"]["request"].get("mode") == "review_documents"
        for number in range(7 if review else 9):
            if "P" + str(number) not in completed:
                break
            prefix.append(completed["P" + str(number)])
        from .call_control import pending_call

        pending = pending_call(self, project_id, run_id)
        return {
            "mode": "review_documents" if review else "enrich_changes",
            "next_stage": len(prefix),
            "outputs": prefix,
            "sha256": digest({"review_contract_version": "review-documents.v1",
                              "completed": [item["sha256"] for item in prefix],
                              "pending_call": pending}) if review else (
                digest({"completed": [item["sha256"] for item in prefix], "pending_call": pending})
                if pending else digest([item["sha256"] for item in prefix])
            ),
            "pending_call": pending,
        }

    def add_artifact(self, project_id: str, run_id: str, kind: str, content: bytes, metadata: dict):
        with self.artifacts.namespace(project_id):
            if self.get(project_id, run_id) is None:
                raise ValueError("run not found in project")
            checksum = self.artifacts.put(project_id, content)
            artifact_id = str(uuid.uuid4())
            with self.store.connection:
                self.store.connection.execute(
                    "INSERT INTO artifacts(project_id,id,run_id,kind,sha256,byte_size,metadata) VALUES(?,?,?,?,?,?,?)",
                    (
                        project_id,
                        artifact_id,
                        run_id,
                        kind,
                        checksum,
                        len(content),
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
            return {
                "id": artifact_id,
                "kind": kind,
                "sha256": checksum,
                "byte_size": len(content),
                "metadata": metadata,
            }

    def artifact(self, project_id: str, run_id: str, artifact_id: str, *, body=False):
        if body:
            with self.artifacts.namespace(project_id):
                return self._artifact(project_id, run_id, artifact_id, body=True)
        return self._artifact(project_id, run_id, artifact_id)

    def _artifact(self, project_id: str, run_id: str, artifact_id: str, *, body=False):
        row = self.store.connection.execute(
            "SELECT kind,sha256,byte_size,metadata FROM artifacts WHERE project_id=? AND run_id=? AND id=?",
            (project_id, run_id, artifact_id),
        ).fetchone()
        if row is None:
            return None
        result = {
            "id": artifact_id,
            "kind": row[0],
            "sha256": row[1],
            "byte_size": row[2],
            "metadata": json.loads(row[3]),
        }
        if body:
            result["body"] = self.artifacts.read(project_id, row[1])
        return result

    def list_artifacts(self, project_id: str, run_id: str, kind: str | None = None):
        rows = self.store.connection.execute(
            "SELECT id FROM artifacts WHERE project_id=? AND run_id=? AND (? IS NULL OR kind=?) ORDER BY rowid",
            (project_id, run_id, kind, kind),
        ).fetchall()
        return [self.artifact(project_id, run_id, row[0]) for row in rows]

    def stage(
        self, project_id: str, run_id: str, stage_id: str, input_refs: list[str], limits: dict
    ):
        run = self.get(project_id, run_id)
        if run is None or any(self.artifact(project_id, run_id, ref) is None for ref in input_refs):
            raise ValueError("stage artifact scope")
        attempt_id = str(uuid.uuid4())
        envelope = {
            "schema_version": "1.0",
            "run_id": run_id,
            "stage_id": stage_id,
            "attempt_id": attempt_id,
            "input_artifact_refs": input_refs,
            "configuration_snapshot_ref": run["configuration_snapshot_ref"],
            "limits": limits,
            "status": "running",
            "output_artifact_refs": [],
            "warnings": [],
            "errors": [],
            "usage": {},
            "started_at": now(),
            "finished_at": None,
        }
        self.save_stage(project_id, envelope)
        return envelope

    def save_stage(self, project_id: str, envelope: dict):
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO stages VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,run_id,stage_id,attempt_id) DO UPDATE SET envelope=excluded.envelope,status=excluded.status",
                (
                    project_id,
                    envelope["run_id"],
                    envelope["stage_id"],
                    envelope["attempt_id"],
                    json.dumps(envelope, ensure_ascii=False),
                    envelope["status"],
                ),
            )

    def stages(self, project_id: str, run_id: str):
        rows = self.store.connection.execute(
            "SELECT envelope FROM stages WHERE project_id=? AND run_id=? ORDER BY rowid",
            (project_id, run_id),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def cache(self, project_id: str, cache_key: str, artifact_id: str):
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO model_cache VALUES(?,?,?) ON CONFLICT(project_id,cache_key) DO NOTHING",
                (project_id, cache_key, artifact_id),
            )

    def cached(self, project_id: str, cache_key: str):
        row = self.store.connection.execute(
            "SELECT a.id,a.run_id FROM model_cache c JOIN artifacts a ON a.project_id=c.project_id AND a.id=c.artifact_id WHERE c.project_id=? AND c.cache_key=?",
            (project_id, cache_key),
        ).fetchone()
        return (
            None
            if row is None
            else self.artifact(project_id, row[1], row[0], body=True) | {"source_run_id": row[1]}
        )
