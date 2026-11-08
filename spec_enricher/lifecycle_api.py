"""Destructive operations are separate from disable/remove-binding operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import Field

from .artifacts import ArtifactStore
from .catalog import digest
from .i18n import translate
from .lifecycle import Lifecycle
from .registry import RevisionConflict
from .stage_contracts import Contract
from .storage import Store


class PurgeSelection(Contract):
    kind: Literal["project", "run", "document", "catalog"]
    object_id: str = Field(min_length=1, max_length=100)


class PurgeConfirmation(PurgeSelection):
    confirmed: Literal[True]
    confirmation: Literal["DELETE HISTORY"]
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class RegistryDelete(Contract):
    confirmed: Literal[True]
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class BundleVisibility(Contract):
    hidden: bool


def install(app, db, project_exists, data_dir, secrets):
    def repository(store):
        return Lifecycle(
            store,
            ArtifactStore(data_dir / "artifacts"),
            Path(os.environ.get("APP_EXPORT_DIR", str(data_dir / "exports"))),
        )

    def checked_preview(project_id, body, store):
        project_exists(project_id, store)
        try:
            return repository(store).preview(project_id, body.kind, body.object_id)
        except LookupError:
            raise HTTPException(404, translate("Объект не найден в проекте.")) from None

    @app.post("/api/v1/projects/{project_id}/purge/preview")
    def preview(project_id: str, body: PurgeSelection, store: Store = Depends(db)):
        return checked_preview(project_id, body, store)

    @app.post("/api/v1/projects/{project_id}/purge/commit")
    def purge(project_id: str, body: PurgeConfirmation, store: Store = Depends(db)):
        project_exists(project_id, store)
        # Threads may still unwind after terminal status. Their filesystem lease
        # also serializes GC, and this guard avoids accepting premature deletion.
        for rid, thread in list(app.state.run_threads.items()):
            if (
                thread.is_alive()
                and store.connection.execute(
                    "SELECT 1 FROM runs WHERE project_id=? AND id=?", (project_id, rid)
                ).fetchone()
            ):
                raise RevisionConflict("worker_still_stopping")
        try:
            result = repository(store).purge(
                project_id, body.kind, body.object_id, body.expected_sha256
            )
        except LookupError:
            raise HTTPException(404, translate("Объект не найден в проекте.")) from None
        if body.kind == "project":
            secrets.clear_project(project_id)
        return result

    @app.get("/api/v1/projects/{project_id}/purge/jobs")
    def jobs(project_id: str, store: Store = Depends(db)):
        # After project deletion the scoped receipt remains available for retry.
        return [
            dict(
                zip(
                    ("id", "kind", "object_id", "status", "error_code", "created_at"),
                    row,
                    strict=True,
                )
            )
            for row in store.connection.execute(
                "SELECT id,kind,object_id,status,error_code,created_at FROM purge_jobs WHERE project_id=? ORDER BY rowid DESC",
                (project_id,),
            )
        ]

    @app.post("/api/v1/projects/{project_id}/purge/jobs/{job_id}/retry")
    def retry(project_id: str, job_id: str, store: Store = Depends(db)):
        try:
            return repository(store).retry(project_id, job_id)
        except LookupError:
            raise HTTPException(404, translate("Задание не найдено в проекте.")) from None

    @app.get("/api/v1/purge/jobs")
    def incomplete_jobs(store: Store = Depends(db)):
        return [
            dict(zip(("id", "project_id", "kind", "object_id", "error_code"), row, strict=True))
            for row in store.connection.execute(
                "SELECT id,project_id,kind,object_id,error_code FROM purge_jobs WHERE status!='complete' ORDER BY rowid"
            )
        ]

    def version_preview(store, owner, revision):
        row = store.connection.execute(
            "SELECT sha256 FROM user_catalogue_versions WHERE user_catalogue_id=? AND revision=?",
            (owner, revision),
        ).fetchone()
        if row is None:
            raise HTTPException(404, translate("Версия user catalogue не найдена."))
        bindings = [
            dict(zip(("project_id", "catalog_version_id"), item, strict=True))
            for item in store.connection.execute(
                "SELECT c.project_id,c.id FROM catalog_versions c JOIN catalog_bindings b ON c.project_id=b.project_id AND c.id=b.catalog_version_id WHERE c.sha256=? AND b.enabled=1 ORDER BY c.project_id,c.id",
                (row[0],),
            )
        ]
        plan = {
            "user_catalogue_id": owner,
            "revision": revision,
            "content_sha256": row[0],
            "active_bindings": bindings,
            "can_delete": not bindings,
            "notice": translate(
                "Удаляется только установленная версия instance registry. Frozen project copies, run history и drafts сохраняются; этот номер revision не используется повторно."
            ),
        }
        return plan | {"sha256": digest(plan)}

    @app.get("/api/v1/user-catalogues/{owner}/versions/{revision}/delete-preview")
    def delete_version_preview(owner: str, revision: int, store: Store = Depends(db)):
        return version_preview(store, owner, revision)

    @app.post("/api/v1/user-catalogues/{owner}/versions/{revision}/delete")
    def delete_version(owner: str, revision: int, body: RegistryDelete, store: Store = Depends(db)):
        try:
            store.connection.execute("BEGIN IMMEDIATE")
            plan = version_preview(store, owner, revision)
            if not plan["can_delete"] or plan["sha256"] != body.expected_sha256:
                raise RevisionConflict("registry_version_needed_or_preview_changed")
            store.connection.execute(
                "DELETE FROM user_catalogue_versions WHERE user_catalogue_id=? AND revision=?",
                (owner, revision),
            )
            store.connection.commit()
        except (RevisionConflict, HTTPException):
            store.connection.rollback()
            raise
        return {"deleted": True, "historical_project_copies_preserved": True}

    @app.get("/api/v1/builtin/visibility")
    def visibility(store: Store = Depends(db)):
        row = store.connection.execute(
            "SELECT value FROM instance_settings WHERE key='builtin_hidden'"
        ).fetchone()
        return {"hidden": row is not None and row[0] == "true", "physically_deletable": False}

    @app.put("/api/v1/builtin/visibility")
    def set_visibility(body: BundleVisibility, store: Store = Depends(db)):
        with store.connection:
            store.connection.execute(
                "INSERT INTO instance_settings VALUES('builtin_hidden',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(body.hidden),),
            )
        return {
            "hidden": body.hidden,
            "physically_deleted": False,
            "existing_bindings_and_history_preserved": True,
        }
