"""Explicit draft preview/commit, portable catalogue files and user-wide reuse."""

from __future__ import annotations

import json
import uuid
from typing import Literal

import yaml
from fastapi import Depends, HTTPException
from fastapi.responses import Response
from pydantic import Field

from .authoring import Authoring, DraftInput, new_fields
from .catalog import ROOT, canonical, digest, semantic_hash
from .i18n import translate
from .registry import RevisionConflict
from .stage_contracts import Contract
from .storage import Store


class CatalogName(Contract):
    name: str = Field(min_length=1, max_length=200)


class SelectedExport(Contract):
    requirement_ids: list[str] = Field(min_length=1, max_length=5000)
    format: Literal["json", "yaml"] = "json"


class CommitDraft(Contract):
    expected_revision: int = Field(ge=1)
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    confirmed: bool
    activate: bool = False
    replace_previous_bindings: bool = False


class UserBinding(Contract):
    user_catalogue_id: str
    revision: int = Field(ge=1)
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    confirmed: bool


class ProjectRanking(Contract):
    requirement_id: str
    version: str
    expected_revision: int = Field(ge=0)
    ranking: Literal["unranked", "critical", "high", "normal", "low"]
    reason: str = Field(min_length=1, max_length=2000)


def install(app, db, project_exists):
    @app.get("/api/v1/projects/{project_id}/requirement-rankings")
    def rankings(project_id: str, store: Store = Depends(db)):
        project_exists(project_id, store)
        return [
            {
                "requirement_id": row[0],
                "version": row[1],
                "revision": row[2],
                "ranking": row[3],
                "reason": row[4],
            }
            for row in store.connection.execute(
                "SELECT r.requirement_id,r.version,r.revision,r.ranking,r.reason FROM project_requirement_overrides r WHERE r.project_id=? AND r.revision=(SELECT max(o.revision) FROM project_requirement_overrides o WHERE o.project_id=r.project_id AND o.requirement_id=r.requirement_id AND o.version=r.version)",
                (project_id,),
            )
        ]

    @app.put("/api/v1/projects/{project_id}/requirement-ranking")
    def ranking(project_id: str, body: ProjectRanking, store: Store = Depends(db)):
        project_exists(project_id, store)
        with store.connection:
            store.connection.execute("BEGIN IMMEDIATE")
            if not store.connection.execute(
                "SELECT 1 FROM requirement_index WHERE project_id=? AND requirement_id=? AND version=?",
                (project_id, body.requirement_id, body.version),
            ).fetchone():
                raise HTTPException(404, translate("Требование не найдено в выбранном проекте."))
            previous = store.connection.execute(
                "SELECT coalesce(max(revision),0) FROM project_requirement_overrides WHERE project_id=? AND requirement_id=? AND version=?",
                (project_id, body.requirement_id, body.version),
            ).fetchone()[0]
            if previous != body.expected_revision:
                raise RevisionConflict("ranking_revision_conflict")
            store.connection.execute(
                "INSERT INTO project_requirement_overrides(project_id,requirement_id,version,revision,ranking,reason) VALUES(?,?,?,?,?,?)",
                (
                    project_id,
                    body.requirement_id,
                    body.version,
                    previous + 1,
                    body.ranking,
                    body.reason,
                ),
            )
        return {"revision": previous + 1, "upstream_severity_unchanged": True}

    @app.get(
        "/api/v1/projects/{project_id}/catalogs/{catalog_id}/requirements/{requirement_id}/reference"
    )
    def reference(
        project_id: str, catalog_id: str, requirement_id: str, store: Store = Depends(db)
    ):
        record = store.requirement(project_id, catalog_id, requirement_id)
        if record is None:
            raise HTTPException(404, translate("Требование не найдено в проекте."))
        return {
            "requirement_id": record["id"],
            "version": record["version"],
            "catalog_version_id": catalog_id,
            "target_semantic_sha256": semantic_hash(record),
            "original_origin": record["origin"],
            "clause_mappings": {clause["id"]: [clause["id"]] for clause in record["clauses"]},
        }

    @app.get("/api/v1/requirement-form/defaults")
    def initial():
        return {
            "fields": new_fields(),
            "implementation_verification": "not_verified",
            "unknown": "Unspecified architectural facts stay unknown; incomplete mandatory structure stays draft.",
        }

    @app.get("/api/v1/requirement-templates/{kind}/{format}")
    def template(kind: Literal["blank", "example"], format: Literal["json", "yaml"]):
        filename = "requirements." + ("template" if kind == "blank" else "example") + "." + format
        return Response(
            (ROOT / "templates" / filename).read_bytes(),
            media_type="application/json" if format == "json" else "application/yaml",
            headers={
                "Content-Disposition": 'attachment; filename="' + filename + '"',
                "X-Template-Status": "draft-not-activated",
            },
        )

    @app.get("/api/v1/projects/{project_id}/catalogs/{catalog_id}/export")
    def export_catalog(
        project_id: str,
        catalog_id: str,
        format: Literal["json", "yaml"] = "json",
        requirement_id: str = "",
        store: Store = Depends(db),
    ):
        value = store.catalog(project_id, catalog_id)
        if value is None:
            raise HTTPException(404, translate("Каталог не найден в проекте."))
        if requirement_id:
            value["requirements"] = [r for r in value["requirements"] if r["id"] == requirement_id]
            if not value["requirements"]:
                raise HTTPException(404, translate("Требование не найдено."))
            value["limitations"] += [
                "Selected subset; dependencies may require the original catalogue."
            ]
        body = (
            canonical(value)
            if format == "json"
            else yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode()
        )
        return Response(
            body,
            media_type="application/json" if format == "json" else "application/yaml",
            headers={"Content-Disposition": 'attachment; filename="requirements.' + format + '"'},
        )

    @app.post("/api/v1/projects/{project_id}/catalogs/{catalog_id}/selected-export")
    def selected_export(
        project_id: str, catalog_id: str, body: SelectedExport, store: Store = Depends(db)
    ):
        value = store.catalog(project_id, catalog_id)
        if value is None:
            raise HTTPException(404, translate("Каталог не найден в проекте."))
        selected = set(body.requirement_ids)
        if len(selected) != len(body.requirement_ids):
            raise ValueError("duplicate_selected_requirement")
        records = {record["id"]: record for record in value["requirements"]}
        if selected - records.keys():
            raise HTTPException(404, translate("Требование не найдено в выбранном snapshot."))
        value["requirements"] = [records[rid] for rid in sorted(selected)]
        value["limitations"] += [
            "Explicit selected policy export, not runtime evidence. Omitted dependency targets must be supplied separately before activation."
        ]
        omitted = sorted(
            {
                dep["requirement_id"]
                for record in value["requirements"]
                for dep in record.get("dependencies", [])
            }
            - selected
        )
        serialized = (
            canonical(value).decode()
            if body.format == "json"
            else yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        )
        return {
            "catalog_snapshot_id": catalog_id,
            "sha256": digest(value),
            "selected_versions": [
                {"id": record["id"], "version": record["version"]}
                for record in value["requirements"]
            ],
            "record_fields": sorted({key for record in value["requirements"] for key in record}),
            "omitted_dependency_targets": omitted,
            "includes_project_documents": False,
            "includes_runtime_evidence": False,
            "includes_secrets": False,
            "format": body.format,
            "content": serialized,
        }

    @app.post("/api/v1/user-catalogues", status_code=201)
    def create_user_catalogue(body: CatalogName, store: Store = Depends(db)):
        cid = str(uuid.uuid4())
        with store.connection:
            store.connection.execute(
                "INSERT INTO user_catalogues(id,name) VALUES(?,?)", (cid, body.name)
            )
        return {"id": cid, "name": body.name}

    @app.get("/api/v1/user-catalogues")
    def user_catalogues(store: Store = Depends(db)):
        return [
            {"id": row[0], "name": row[1], "latest_revision": row[2]}
            for row in store.connection.execute(
                "SELECT u.id,u.name,(SELECT max(v.revision) FROM user_catalogue_versions v WHERE u.id=v.user_catalogue_id) FROM user_catalogues u ORDER BY u.rowid"
            )
        ]

    @app.get("/api/v1/user-catalogues/{catalogue_id}/versions")
    def user_versions(catalogue_id: str, store: Store = Depends(db)):
        return [
            {
                "revision": row[0],
                "sha256": row[1],
                "created_at": row[2],
                "content": json.loads(row[3]),
            }
            for row in store.connection.execute(
                "SELECT revision,sha256,created_at,content FROM user_catalogue_versions WHERE user_catalogue_id=? ORDER BY revision DESC",
                (catalogue_id,),
            )
        ]

    @app.post("/api/v1/projects/{project_id}/user-catalogue-bindings")
    def bind_user(project_id: str, body: UserBinding, store: Store = Depends(db)):
        project_exists(project_id, store)
        row = store.connection.execute(
            "SELECT content,sha256 FROM user_catalogue_versions WHERE user_catalogue_id=? AND revision=?",
            (body.user_catalogue_id, body.revision),
        ).fetchone()
        if row is None or row[1] != body.expected_sha256 or not body.confirmed:
            raise RevisionConflict("user_catalogue_preview_changed")
        return {
            "catalog_id": store.bind_catalog(project_id, json.loads(row[0])),
            "pinned_revision": body.revision,
            "documents_copied": False,
        }

    def owner(scope, owner_id, store):
        if scope == "project":
            project_exists(owner_id, store)
        elif not store.connection.execute(
            "SELECT 1 FROM user_catalogues WHERE id=?", (owner_id,)
        ).fetchone():
            raise HTTPException(404, translate("Пользовательский каталог не найден."))

    @app.get("/api/v1/requirement-drafts/{scope}/{owner_id}")
    def draft_list(
        scope: Literal["project", "user_catalogue"], owner_id: str, store: Store = Depends(db)
    ):
        owner(scope, owner_id, store)
        column = "project_id" if scope == "project" else "user_catalogue_id"
        rows = store.connection.execute(
            f"SELECT DISTINCT id FROM requirement_drafts WHERE scope=? AND {column}=? ORDER BY rowid",
            (scope, owner_id),
        ).fetchall()
        return [Authoring(store).get(scope, owner_id, row[0]) for row in rows]

    @app.post("/api/v1/requirement-drafts/{scope}/{owner_id}", status_code=201)
    def create_draft(
        scope: Literal["project", "user_catalogue"],
        owner_id: str,
        body: DraftInput,
        store: Store = Depends(db),
    ):
        owner(scope, owner_id, store)
        if body.expected_revision is not None:
            raise ValueError("new_draft_has_no_revision")
        return Authoring(store).save(scope, owner_id, body)

    @app.put("/api/v1/requirement-drafts/{scope}/{owner_id}/{draft_id}")
    def update_draft(
        scope: Literal["project", "user_catalogue"],
        owner_id: str,
        draft_id: str,
        body: DraftInput,
        store: Store = Depends(db),
    ):
        owner(scope, owner_id, store)
        if Authoring(store).get(scope, owner_id, draft_id) is None:
            raise HTTPException(404, translate("Draft не найден в выбранном scope."))
        return Authoring(store).save(scope, owner_id, body, draft_id)

    @app.get("/api/v1/requirement-drafts/{scope}/{owner_id}/{draft_id}/preview")
    def draft_preview(
        scope: Literal["project", "user_catalogue"],
        owner_id: str,
        draft_id: str,
        store: Store = Depends(db),
    ):
        value = Authoring(store).get(scope, owner_id, draft_id)
        if value is None:
            raise HTTPException(404, translate("Draft не найден в выбранном scope."))
        return Authoring(store).preview(value)

    @app.post("/api/v1/requirement-drafts/{scope}/{owner_id}/{draft_id}/commit")
    def commit(
        scope: Literal["project", "user_catalogue"],
        owner_id: str,
        draft_id: str,
        body: CommitDraft,
        store: Store = Depends(db),
    ):
        with store.connection:
            store.connection.execute("BEGIN IMMEDIATE")
            draft = Authoring(store).get(scope, owner_id, draft_id)
            if draft is None:
                raise HTTPException(404, translate("Draft не найден в выбранном scope."))
            if not body.confirmed or draft["revision"] != body.expected_revision:
                raise RevisionConflict("draft_changed_or_not_approved")
            return Authoring(store).commit(
                draft,
                body.expected_sha256,
                activate=body.activate,
                replace_bindings=body.replace_previous_bindings,
            )
