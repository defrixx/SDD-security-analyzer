"""Reviewable settings transfer, with no documents, credentials or automatic egress."""

from __future__ import annotations

import json
import time
import uuid

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .catalog import canonical
from .i18n import translate
from .registry import RevisionConflict
from .settings import export_settings, import_settings, parse_bundle, preview_settings
from .stage_contracts import Contract
from .storage import Store


class ExportSelection(Contract):
    project_ids: list[str] = Field(min_length=1, max_length=100)
    include_catalogues: bool = False
    sanitized: bool = False


class ExportDownload(ExportSelection):
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    confirmed: bool


class SettingsCommit(Contract):
    preview_id: str
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    confirmed: bool


def install(app, db, secrets):
    @app.post("/api/v1/settings/export/preview")
    def export_preview(body: ExportSelection, store: Store = Depends(db)):
        bundle = export_settings(
            store,
            body.project_ids,
            include_catalogues=body.include_catalogues,
            sanitized=body.sanitized,
        )
        secrets.reject_known(canonical(bundle))
        return {
            "preview": preview_settings(bundle),
            "bundle": bundle,
            "contains_raw_documents": False,
            "contains_secret_values": False,
            "notice": "Endpoints, paths and authored templates may reveal internal information. This JSON is not encrypted.",
        }

    @app.post("/api/v1/settings/export")
    def download(body: ExportDownload, store: Store = Depends(db)):
        bundle = export_settings(
            store,
            body.project_ids,
            include_catalogues=body.include_catalogues,
            sanitized=body.sanitized,
        )
        plan = preview_settings(bundle)
        if not body.confirmed or plan["sha256"] != body.expected_sha256:
            raise RevisionConflict("settings_export_changed")
        data = canonical(bundle)
        secrets.reject_known(data)
        for pid in body.project_ids:
            store.audit(pid, "settings_exported_without_credentials_or_documents", plan["sha256"])
        return Response(
            data,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="settings-bundle.json"'},
        )

    @app.post("/api/v1/settings/import/preview")
    async def import_preview(request: Request, store: Store = Depends(db)):
        raw = await request.body()
        secrets.reject_known(raw)
        bundle = await run_in_threadpool(parse_bundle, raw)
        secrets.reject_known(canonical(bundle))
        plan = preview_settings(bundle)
        with store.connection:
            store.connection.execute(
                "DELETE FROM settings_previews WHERE expires_at<?", (time.time(),)
            )
            previous = store.connection.execute(
                "SELECT id FROM settings_previews WHERE sha256=?", (plan["sha256"],)
            ).fetchone()
            pid = previous[0] if previous else str(uuid.uuid4())
            if previous is None:
                if (
                    store.connection.execute("SELECT count(*) FROM settings_previews").fetchone()[0]
                    >= 20
                ):
                    raise HTTPException(
                        409,
                        translate(
                            "Слишком много settings previews. Очистите их перед новым импортом."
                        ),
                    )
                store.connection.execute(
                    "INSERT INTO settings_previews VALUES(?,?,?,?)",
                    (pid, plan["sha256"], canonical(bundle), time.time() + 3600),
                )
        return plan | {
            "preview_id": pid,
            "expires_in_seconds": 3600,
            "existing_import": store.connection.execute(
                "SELECT 1 FROM settings_imports WHERE bundle_sha256=?", (plan["sha256"],)
            ).fetchone()
            is not None,
        }

    @app.post("/api/v1/settings/import/commit")
    def commit(body: SettingsCommit, store: Store = Depends(db)):
        row = store.connection.execute(
            "SELECT sha256,content,expires_at FROM settings_previews WHERE id=?", (body.preview_id,)
        ).fetchone()
        if (
            row is None
            or row[2] < time.time()
            or row[0] != body.expected_sha256
            or not body.confirmed
        ):
            raise RevisionConflict("settings_preview_missing_expired_or_changed")
        bundle = json.loads(row[1])
        secrets.reject_known(row[1])
        return import_settings(store, bundle)

    @app.get("/api/v1/settings/imports")
    def receipts(store: Store = Depends(db)):
        return [
            {"sha256": row[0], "receipt": json.loads(row[1]), "created_at": row[2]}
            for row in store.connection.execute(
                "SELECT bundle_sha256,receipt,created_at FROM settings_imports ORDER BY rowid DESC LIMIT 100"
            )
        ]

    @app.delete("/api/v1/settings/import/previews")
    def clear_previews(store: Store = Depends(db)):
        with store.connection:
            store.connection.execute("DELETE FROM settings_previews")
        return {"cleared": True, "committed_projects_unchanged": True}
