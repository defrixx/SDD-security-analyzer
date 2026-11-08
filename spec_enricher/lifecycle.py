"""Explicit, reference-checked deletion. Database intent precedes retryable scoped GC."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path

from .artifacts import ArtifactStore
from .catalog import canonical, digest
from .i18n import translate
from .registry import RevisionConflict

ACTIVE = ("queued", "running", "waiting_approval")
PROJECT_TABLES = (
    "documents",
    "catalog_versions",
    "catalog_bindings",
    "catalog_imports",
    "catalog_updates",
    "registry_versions",
    "registry_heads",
    "capability_reports",
    "egress_grants",
    "source_snapshots",
    "runs",
    "artifacts",
    "findings",
    "review_decisions",
    "model_cache",
    "exports",
    "requirement_drafts",
    "project_requirement_overrides",
    "audit_events",
)


def require_no_pending_purge(store, project_id):
    if store.connection.execute(
        "SELECT 1 FROM purge_jobs WHERE project_id=? AND status!='complete' LIMIT 1",
        (project_id,),
    ).fetchone():
        raise RevisionConflict("pending_project_purge_requires_retry")


class Lifecycle:
    def __init__(self, store, artifacts: ArtifactStore, export_root: Path):
        self.store, self.artifacts, self.export_root = store, artifacts, export_root

    def binding_preview(self, project_id, catalog_id):
        catalog = self.store.catalog(project_id, catalog_id)
        if catalog is None:
            raise LookupError("catalog_not_found")
        targets = {r["id"] for r in catalog["requirements"]}
        dependents = []
        for row in self.store.connection.execute(
            "SELECT r.requirement_id,r.version,r.content FROM requirement_index r JOIN catalog_bindings b ON r.project_id=b.project_id AND r.catalog_version_id=b.catalog_version_id WHERE r.project_id=? AND b.enabled=1 AND r.catalog_version_id!=? ORDER BY r.requirement_id",
            (project_id, catalog_id),
        ):
            for dependency in json.loads(row[2]).get("dependencies", []):
                if dependency["requirement_id"] in targets:
                    dependents.append(
                        {"requirement_id": row[0], "version": row[1], "dependency": dependency}
                    )
        plan = {
            "project_id": project_id,
            "catalog_version_id": catalog_id,
            "catalog_sha256": digest(catalog),
            "dependent_requirements": dependents,
            "pinned_run_count": self.store.connection.execute(
                "SELECT count(*) FROM run_catalogs WHERE project_id=? AND catalog_version_id=?",
                (project_id, catalog_id),
            ).fetchone()[0],
            "binding": self.store.connection.execute(
                "SELECT enabled FROM catalog_bindings WHERE project_id=? AND catalog_version_id=?",
                (project_id, catalog_id),
            ).fetchone(),
            "history_preserved": True,
            "notice": translate(
                "Удаляется только active binding. Snapshots/reviews, собственные требования и документы сохраняются. Отсутствующие dependencies требуют явного выбора policy перед новым run."
            ),
        }
        return plan | {"sha256": digest(plan)}

    def preview(self, project_id: str, kind: str, object_id: str):
        db = self.store.connection
        if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise LookupError("project_not_found")
        require_no_pending_purge(self.store, project_id)
        # A worker may be between its last call and final persistence. Block all
        # project purges until it reaches a terminal state, including waiting runs.
        active = db.execute(
            "SELECT id,status FROM runs WHERE project_id=? AND status IN (?,?,?) ORDER BY id",
            (project_id, *ACTIVE),
        ).fetchall()
        counts, blockers, dependencies = {}, [], []
        pending_artifacts, pending_exports = [], []
        if active:
            blockers.append("cancel_project_runs_and_wait_for_terminal_state")
        if kind == "project":
            if object_id != project_id:
                raise LookupError("project_scope_mismatch")
            for table in PROJECT_TABLES:
                counts[table] = db.execute(
                    f"SELECT count(*) FROM {table} WHERE project_id=?", (project_id,)
                ).fetchone()[0]
            artifacts = db.execute(
                "SELECT DISTINCT sha256 FROM artifacts WHERE project_id=? ORDER BY sha256",
                (project_id,),
            ).fetchall()
            exports = db.execute(
                "SELECT DISTINCT bundle_sha256 FROM exports WHERE project_id=? ORDER BY bundle_sha256",
                (project_id,),
            ).fetchall()
            # Include abandoned atomic-write files only by known safe hash names.
            with self.artifacts.directory(project_id) as directory:
                pending_artifacts = sorted(
                    name
                    for name in os.listdir(directory)
                    if re.fullmatch(r"\.pending-[a-f0-9-]{36}", name)
                )
                artifacts = [
                    (value,)
                    for value in sorted(
                        {r[0] for r in artifacts}
                        | {
                            name
                            for name in os.listdir(directory)
                            if re.fullmatch(r"[a-f0-9]{64}", name)
                        }
                    )
                ]
            with ArtifactStore(self.export_root).directory(project_id) as directory:
                names = os.listdir(directory)
                exports = [
                    (value,)
                    for value in sorted(
                        {r[0] for r in exports}
                        | {value for value in names if re.fullmatch(r"[a-f0-9]{64}", value)}
                    )
                ]
                pending_exports = sorted(
                    value for value in names if re.fullmatch(r"\.pending-[a-f0-9-]{36}", value)
                )
            counts["abandoned_temporary_files"] = len(pending_artifacts) + len(pending_exports)
        elif kind == "run":
            if not db.execute(
                "SELECT 1 FROM runs WHERE project_id=? AND id=?", (project_id, object_id)
            ).fetchone():
                raise LookupError("run_not_found")
            for table in (
                "artifacts",
                "findings",
                "review_decisions",
                "exports",
                "stages",
                "requirement_results",
            ):
                counts[table] = db.execute(
                    f"SELECT count(*) FROM {table} WHERE project_id=? AND run_id=?",
                    (project_id, object_id),
                ).fetchone()[0]
            counts["runs"] = 1
            artifacts = db.execute(
                "SELECT DISTINCT sha256 FROM artifacts WHERE project_id=? AND run_id=? ORDER BY sha256",
                (project_id, object_id),
            ).fetchall()
            exports = db.execute(
                "SELECT DISTINCT bundle_sha256 FROM exports WHERE project_id=? AND run_id=? ORDER BY bundle_sha256",
                (project_id, object_id),
            ).fetchall()
        elif kind in {"document", "catalog"}:
            table = "documents" if kind == "document" else "catalog_versions"
            column = "document_id" if kind == "document" else "catalog_version_id"
            ref_table = "run_documents" if kind == "document" else "run_catalogs"
            if not db.execute(
                f"SELECT 1 FROM {table} WHERE project_id=? AND id=?", (project_id, object_id)
            ).fetchone():
                raise LookupError("snapshot_not_found")
            dependencies = [
                r[0]
                for r in db.execute(
                    f"SELECT run_id FROM {ref_table} WHERE project_id=? AND {column}=? ORDER BY run_id",
                    (project_id, object_id),
                )
            ]
            if dependencies:
                blockers.append("snapshot_is_pinned_by_remaining_runs")
            if kind == "catalog":
                if db.execute(
                    "SELECT 1 FROM catalog_bindings WHERE project_id=? AND catalog_version_id=? AND enabled=1",
                    (project_id, object_id),
                ).fetchone():
                    blockers.append("disable_or_remove_catalog_binding_first")
                if db.execute(
                    "SELECT built_in FROM catalog_versions WHERE project_id=? AND id=?",
                    (project_id, object_id),
                ).fetchone()[0]:
                    # Deleting a project copy cannot delete the read-only image bundle.
                    counts["read_only_image_bundle_deleted"] = 0
                imports = db.execute(
                    "SELECT id FROM catalog_imports WHERE project_id=? AND committed_id=?",
                    (project_id, object_id),
                ).fetchall()
                counts["catalog_imports"] = len(imports)
                counts["source_snapshots"] = db.execute(
                    "SELECT count(*) FROM source_snapshots s JOIN catalog_imports i ON s.project_id=i.project_id AND s.catalog_import_id=i.id WHERE i.project_id=? AND i.committed_id=?",
                    (project_id, object_id),
                ).fetchone()[0]
            else:
                counts["source_snapshots"] = db.execute(
                    "SELECT count(*) FROM source_snapshots WHERE project_id=? AND document_id=?",
                    (project_id, object_id),
                ).fetchone()[0]
            counts[table] = 1
            artifacts, exports = [], []
        else:
            raise ValueError("unsupported_purge_kind")
        plan = {
            "project_id": project_id,
            "kind": kind,
            "object_id": object_id,
            "counts": counts,
            "active_runs": [{"id": r[0], "status": r[1]} for r in active],
            "dependent_run_ids": dependencies,
            "blockers": blockers,
            "artifact_hashes": [r[0] for r in artifacts],
            "export_hashes": [r[0] for r in exports],
            "pending_artifact_files": pending_artifacts,
            "pending_export_files": pending_exports,
            "notice": translate(
                "Потеря воспроизводимости выбранной истории. Внешние backups, migration backups и выгруженные пользователем копии сохраняются. Secure erase носителя не обещается. Shared templates/user catalogues и другие проекты сохраняются."
            ),
        }
        # Any edit/review/source refresh after preview invalidates confirmation.
        plan["state_sha256"] = self._state(project_id)
        return plan | {"sha256": digest(plan)}

    def _state(self, project_id):
        db = self.store.connection
        # Hash private bytes locally, never put those bytes in the preview/job.
        value = hashlib.sha256()
        for table in PROJECT_TABLES:
            value.update(table.encode())
            for record in db.execute(
                f"SELECT * FROM {table} WHERE project_id=? ORDER BY rowid", (project_id,)
            ):
                for cell in record:
                    encoded = cell if isinstance(cell, bytes) else canonical(cell)
                    value.update(len(encoded).to_bytes(8, "big"))
                    value.update(encoded)
        return value.hexdigest()

    def purge(self, project_id, kind, object_id, expected_sha256):
        with self.artifacts.namespace(project_id, exclusive=True):
            db = self.store.connection
            try:
                db.execute("BEGIN IMMEDIATE")
                plan = self.preview(project_id, kind, object_id)
                if plan["sha256"] != expected_sha256:
                    raise RevisionConflict("purge_preview_changed")
                if plan["blockers"]:
                    raise RevisionConflict("purge_blocked_by_references_or_active_worker")
                job = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO purge_jobs(id,project_id,kind,object_id,plan,status) VALUES(?,?,?,?,?,'pending')",
                    (job, project_id, kind, object_id, json.dumps(plan)),
                )
                if kind == "project":
                    db.execute("DELETE FROM source_snapshots WHERE project_id=?", (project_id,))
                    db.execute("DELETE FROM exports WHERE project_id=?", (project_id,))
                    db.execute("DELETE FROM runs WHERE project_id=?", (project_id,))
                    db.execute("DELETE FROM projects WHERE id=?", (project_id,))
                elif kind == "run":
                    db.execute(
                        "DELETE FROM exports WHERE project_id=? AND run_id=?",
                        (project_id, object_id),
                    )
                    db.execute(
                        "DELETE FROM runs WHERE project_id=? AND id=?", (project_id, object_id)
                    )
                elif kind == "document":
                    db.execute(
                        "DELETE FROM source_snapshots WHERE project_id=? AND document_id=?",
                        (project_id, object_id),
                    )
                    db.execute(
                        "DELETE FROM documents WHERE project_id=? AND id=?", (project_id, object_id)
                    )
                else:
                    db.execute(
                        "DELETE FROM source_snapshots WHERE project_id=? AND catalog_import_id IN (SELECT id FROM catalog_imports WHERE project_id=? AND committed_id=?)",
                        (project_id, project_id, object_id),
                    )
                    db.execute(
                        "DELETE FROM catalog_imports WHERE project_id=? AND committed_id=?",
                        (project_id, object_id),
                    )
                    db.execute(
                        "DELETE FROM catalog_versions WHERE project_id=? AND id=?",
                        (project_id, object_id),
                    )
                if kind != "project":
                    self.store.audit(project_id, "history_purge_committed", job)
                db.commit()
            except (ValueError, LookupError, OSError, sqlite3.Error):
                db.rollback()
                raise
            return self._collect(project_id, job, plan)

    def retry(self, project_id, job_id):
        with self.artifacts.namespace(project_id, exclusive=True):
            row = self.store.connection.execute(
                "SELECT plan,status FROM purge_jobs WHERE project_id=? AND id=?",
                (project_id, job_id),
            ).fetchone()
            if row is None:
                raise LookupError("purge_job_not_found_in_project")
            if row[1] == "complete":
                return {"id": job_id, "status": "complete", "database_deleted": True}
            return self._collect(project_id, job_id, json.loads(row[0]))

    def _collect(self, project_id, job, plan):
        db = self.store.connection
        error: str | None
        try:
            with self.artifacts.directory(project_id) as directory:
                for checksum in plan["artifact_hashes"]:
                    if not db.execute(
                        "SELECT 1 FROM artifacts WHERE project_id=? AND sha256=? LIMIT 1",
                        (project_id, checksum),
                    ).fetchone():
                        self._unlink(directory, checksum)
                for filename in plan.get("pending_artifact_files", []):
                    self._unlink(directory, filename, temporary=True)
                os.fsync(directory)
            export_store = ArtifactStore(self.export_root)
            with export_store.directory(project_id) as directory:
                for checksum in plan["export_hashes"]:
                    if not db.execute(
                        "SELECT 1 FROM exports WHERE project_id=? AND bundle_sha256=? LIMIT 1",
                        (project_id, checksum),
                    ).fetchone():
                        self._unlink(directory, checksum)
                for filename in plan.get("pending_export_files", []):
                    self._unlink(directory, filename, temporary=True)
                os.fsync(directory)
        except (OSError, ValueError):
            status, error = "pending", "scoped_file_cleanup_failed_retry_required"
        else:
            status, error = "complete", None
        with db:
            db.execute(
                "UPDATE purge_jobs SET status=?,error_code=? WHERE project_id=? AND id=?",
                (status, error, project_id, job),
            )
            if status == "complete":
                receipt = {key: plan[key] for key in ("project_id", "kind", "object_id", "counts")}
                db.execute(
                    "UPDATE purge_jobs SET plan=? WHERE project_id=? AND id=?",
                    (json.dumps(receipt), project_id, job),
                )
        return {"id": job, "status": status, "error_code": error, "database_deleted": True}

    @staticmethod
    def _unlink(directory, checksum, *, temporary=False):
        pattern = r"\.pending-[a-f0-9-]{36}" if temporary else r"[a-f0-9]{64}"
        if not re.fullmatch(pattern, checksum):
            raise ValueError("invalid_gc_hash")
        try:
            info = os.stat(checksum, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("gc_nonregular_file_rejected")
        os.unlink(checksum, dir_fd=directory)
