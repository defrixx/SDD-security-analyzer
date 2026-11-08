"""Explicit source scope consent, real read tests and immutable ingestion snapshots."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import Field

from .i18n import translate
from .registry import Registry, RevisionConflict
from .security import error
from .sources import DEFAULT_EXCLUSIONS, SourceDefinition, collect, ingest
from .stage_contracts import Contract
from .storage import Store


class SourceWrite(Contract):
    expected_revision: int = Field(ge=1)
    source: SourceDefinition


class SourceConsent(Contract):
    revision: int = Field(ge=1)
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    confirmed: bool


def install(app, db, project_exists, secrets):
    root = Path(os.environ.get("APP_SOURCE_DIR", "/sources"))
    app.state.source_root = root

    @app.get("/api/v1/source-defaults")
    def defaults():
        return {
            "mounted_root": "/sources",
            "default_exclusions": DEFAULT_EXCLUSIONS,
            "freshness": "pinned_manual_refresh",
            "crawl_depth": 0,
            "notice": translate(
                "Выберите только корень внутри read-only /sources mount. Secret/QA/oracle directories и symlinks не читаются. Ничего не сканируется до явного принятия scope."
            ),
        }

    @app.post("/api/v1/projects/{project_id}/sources", status_code=201)
    def create(project_id: str, body: SourceDefinition, store: Store = Depends(db)):
        project_exists(project_id, store)
        return Registry(store).save(project_id, "source", body.model_dump())

    @app.get("/api/v1/projects/{project_id}/sources")
    def sources(project_id: str, store: Store = Depends(db)):
        project_exists(project_id, store)
        return Registry(store).entries(project_id, "source")

    @app.put("/api/v1/projects/{project_id}/sources/{source_id}")
    def update(project_id: str, source_id: str, body: SourceWrite, store: Store = Depends(db)):
        record = Registry(store).save(
            project_id,
            "source",
            body.source.model_dump(),
            object_id=source_id,
            expected_revision=body.expected_revision,
        )
        if not body.source.enabled:
            with store.connection:
                store.connection.execute(
                    "UPDATE documents SET active=0 WHERE project_id=? AND id IN (SELECT document_id FROM source_snapshots WHERE project_id=? AND source_id=?)",
                    (project_id, project_id, source_id),
                )
        return record

    @app.post("/api/v1/projects/{project_id}/sources/{source_id}/grants")
    def grant(project_id: str, source_id: str, body: SourceConsent, store: Store = Depends(db)):
        record = Registry(store).get(project_id, "source", source_id, body.revision)
        if record is None or not body.confirmed or record["sha256"] != body.expected_sha256:
            raise RevisionConflict("source_scope_preview_changed")
        with store.connection:
            store.connection.execute(
                "INSERT INTO egress_grants(project_id,kind,id,revision,config_sha256,scopes) VALUES(?,'source',?,?,?,?) ON CONFLICT(project_id,kind,id,revision) DO UPDATE SET config_sha256=excluded.config_sha256,scopes=excluded.scopes",
                (project_id, source_id, body.revision, body.expected_sha256, '["source_queries"]'),
            )
        store.audit(project_id, "source_read_scope_accepted", source_id)
        return {"granted_scopes": ["source_queries"]}

    @app.delete("/api/v1/projects/{project_id}/sources/{source_id}/grants")
    def revoke(project_id: str, source_id: str, store: Store = Depends(db)):
        if Registry(store).get(project_id, "source", source_id) is None:
            raise HTTPException(404, translate("Источник не найден в проекте."))
        with store.connection:
            store.connection.execute(
                "DELETE FROM egress_grants WHERE project_id=? AND kind='source' AND id=?",
                (project_id, source_id),
            )
        store.audit(project_id, "source_scope_revoked", source_id)
        return {"revoked_all_revisions": True}

    def read(project_id, source_id, body, store, *, test):
        registry = Registry(store)
        record = registry.get(project_id, "source", source_id, body.revision)
        if record is None:
            raise HTTPException(404, translate("Источник не найден в проекте."))
        if not body.confirmed or record["sha256"] != body.expected_sha256:
            raise RevisionConflict("source_read_preview_changed")
        store.audit(project_id, "source_test_read" if test else "source_ingestion_read", source_id)
        try:
            record, source, files, summary = collect(
                registry,
                secrets,
                project_id,
                source_id,
                body.revision,
                app.state.source_root,
                test=test,
            )
            report = {
                "status": "passed"
                if not any(item["validation_issues"] for item in files)
                else "blocked_catalog_validation",
                "network": "local_files" if source.kind == "local_folder" else "reachable",
                "auth": "accepted" if source.kind != "local_folder" else "not_applicable",
                "read": "passed",
                "summary": summary,
                "preview": files[0]["derived"].decode("utf-8-sig")[:1500],
                "validation_issues": [
                    issue for item in files for issue in item["validation_issues"]
                ],
                "scope": source.model_dump(exclude={"secret_ref"}),
                "synthetic": False,
                "test_reads_real_selected_source": True,
            }
            snapshots = []
            if not test:
                with store.connection:
                    store.connection.execute("BEGIN IMMEDIATE")
                    # Previous bytes remain immutable; only the default active selection changes.
                    old = store.connection.execute(
                        "SELECT document_id FROM source_snapshots WHERE project_id=? AND source_id=? AND document_id IS NOT NULL",
                        (project_id, source_id),
                    ).fetchall()
                    for row in old:
                        store.connection.execute(
                            "UPDATE documents SET active=0 WHERE project_id=? AND id=?",
                            (project_id, row[0]),
                        )
                    snapshots = ingest(store, record, source, project_id, files)
            registry.report(project_id, source_id, body.revision, report, kind="source")
            return {
                "report": report,
                "snapshots": snapshots,
                "catalog_activation": "explicit_preview_and_commit_required",
            }
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "code", None) or (
                "source_read_or_validation_failed"
                if type(exc).__name__ not in {"ValueError", "MCPError"}
                else str(exc)
            )
            # Reports contain controlled error codes, never exception input/body/credentials.
            allowed = (
                reason
                if isinstance(reason, str)
                and len(reason) < 100
                and all(c.isalnum() or c in "_-" for c in reason)
                else "source_read_or_validation_failed"
            )
            report = {
                "status": "failed",
                "read": "failed",
                "error": allowed,
                "test_reads_real_selected_source": True,
            }
            registry.report(project_id, source_id, body.revision, report, kind="source")
            return error(
                422,
                "source_read_failed",
                translate(
                    "Источник не прочитан; проверьте scope, mount/path, encoding, schema, сеть и credentials. Snapshot не активирован."
                ),
                [{"reason": allowed}],
            )

    @app.post("/api/v1/projects/{project_id}/sources/{source_id}/test")
    def test_source(
        project_id: str, source_id: str, body: SourceConsent, store: Store = Depends(db)
    ):
        return read(project_id, source_id, body, store, test=True)

    @app.post("/api/v1/projects/{project_id}/sources/{source_id}/ingest")
    def ingest_source(
        project_id: str, source_id: str, body: SourceConsent, store: Store = Depends(db)
    ):
        return read(project_id, source_id, body, store, test=False)

    @app.get("/api/v1/projects/{project_id}/source-snapshots")
    def snapshots(
        project_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
        store: Store = Depends(db),
    ):
        project_exists(project_id, store)
        return [
            {
                "id": row[0],
                "source_id": row[1],
                "source_revision": row[2],
                "uri": row[3],
                "raw_sha256": row[4],
                "document_id": row[5],
                "catalog_import_id": row[6],
                "metadata": json.loads(row[7]),
                "created_at": row[8],
            }
            for row in store.connection.execute(
                "SELECT id,source_id,source_revision,uri,raw_sha256,document_id,catalog_import_id,metadata,created_at FROM source_snapshots WHERE project_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            )
        ]

    @app.get("/api/v1/projects/{project_id}/source-snapshots/{snapshot_id}/raw")
    def raw(project_id: str, snapshot_id: str, store: Store = Depends(db)):
        row = store.connection.execute(
            "SELECT raw_content FROM source_snapshots WHERE project_id=? AND id=?",
            (project_id, snapshot_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, translate("Snapshot не найден в проекте."))
        secrets.reject_known(row[0])
        return Response(
            row[0],
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="source-original.bin"'},
        )
