"""Catalogue coverage, domain preview and explicit scoped update transactions."""

from __future__ import annotations

import json

from fastapi import Depends, HTTPException
from pydantic import Field, model_validator

from .builtin import load_verified
from .catalog import canonical, digest
from .catalog_management import coverage, domain_subset, update_preview
from .i18n import translate
from .parser_worker import parse_bounded
from .registry import RevisionConflict
from .stage_contracts import Contract
from .storage import Store


class Domains(Contract):
    domains: list[str] = Field(default_factory=list, max_length=50)


class ClauseTarget(Contract):
    requirement_id: str = Field(min_length=1, max_length=200)
    clause_id: str = Field(min_length=1, max_length=200)


class ClauseMapping(Contract):
    old_requirement_id: str = Field(min_length=1, max_length=200)
    old_clause_id: str = Field(min_length=1, max_length=200)
    targets: list[ClauseTarget] = Field(min_length=1, max_length=100)


class RecordMapping(Contract):
    old_requirement_id: str = Field(min_length=1, max_length=200)
    target_requirement_ids: list[str] = Field(min_length=1, max_length=100)


class UpdateSelection(Contract):
    previous_catalog_version_id: str
    candidate_catalog_version_id: str | None = None
    import_id: str | None = None
    clause_mappings: list[ClauseMapping] = Field(default_factory=list, max_length=10000)
    record_mappings: list[RecordMapping] = Field(default_factory=list, max_length=5000)
    retirements: dict[str, str] = Field(default_factory=dict, max_length=5000)

    @model_validator(mode="after")
    def one_candidate(self):
        if (self.candidate_catalog_version_id is None) == (self.import_id is None):
            raise ValueError("choose_one_scoped_candidate")
        if any(not reason.strip() or len(reason) > 2000 for reason in self.retirements.values()):
            raise ValueError("retirement_requires_bounded_reason")
        return self


class UpdateCommit(UpdateSelection):
    confirmed: bool
    activate: bool = False
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")


def install(app, db, project_exists):
    def candidate(store, project_id, body):
        if body.import_id:
            row = store.staged_catalog(project_id, body.import_id)
            if row is None:
                raise HTTPException(404, translate("Staged catalogue не найден в проекте."))
            if json.loads(row[2]):
                raise RevisionConflict("catalogue_candidate_has_validation_issues")
            return json.loads(row[0])
        value = store.catalog(project_id, body.candidate_catalog_version_id)
        if value is None:
            raise HTTPException(404, translate("Catalogue version не найдена в проекте."))
        return value

    def plan(store, project_id, body, value):
        try:
            return update_preview(
                store,
                project_id,
                body.previous_catalog_version_id,
                value,
                clause_mappings=[item.model_dump() for item in body.clause_mappings],
                record_mappings=[item.model_dump() for item in body.record_mappings],
                retirements=body.retirements,
            )
        except LookupError:
            raise HTTPException(
                404, translate("Предыдущая catalogue version не найдена в проекте.")
            ) from None

    @app.get("/api/v1/builtin-catalog/coverage")
    def builtin_coverage():
        _, value = load_verified()
        return coverage(value)

    @app.get("/api/v1/projects/{project_id}/catalogs/{catalog_id}/coverage")
    def catalog_coverage(project_id: str, catalog_id: str, store: Store = Depends(db)):
        value = store.catalog(project_id, catalog_id)
        if value is None:
            raise HTTPException(404, translate("Каталог не найден в проекте."))
        return coverage(value)

    @app.post("/api/v1/projects/{project_id}/builtin-catalog/preview")
    def preview_builtin(project_id: str, body: Domains, store: Store = Depends(db)):
        project_exists(project_id, store)
        _, base = load_verified()
        value, additions = domain_subset(base, body.domains)
        value, issues = parse_bounded(canonical(value), "json", catalog=True)
        if not issues:
            issues.extend(store.catalog_identity_conflicts(project_id, value))
        import_id = store.stage_catalog(project_id, value, issues)
        return {
            "id": import_id,
            "sha256": digest(value),
            "issues": issues,
            "records": len(value["requirements"]),
            "selected_domains": body.domains,
            "dependency_requirement_ids_added": additions,
            "coverage": coverage(value),
            "origin": value["origin"],
            "limitations": value["limitations"],
            "activation_requires_confirmation": True,
            "notice": "Offline verified bundle. Preview includes explicit dependency closure; no source command, git clone, crawl or skill installation. Accept this exact scope before activation.",
        }

    @app.post("/api/v1/projects/{project_id}/catalog-updates/preview")
    def preview_update(project_id: str, body: UpdateSelection, store: Store = Depends(db)):
        project_exists(project_id, store)
        value = candidate(store, project_id, body)
        return plan(store, project_id, body, value)

    @app.post("/api/v1/projects/{project_id}/catalog-updates/commit")
    def commit_update(project_id: str, body: UpdateCommit, store: Store = Depends(db)):
        project_exists(project_id, store)
        if not body.confirmed:
            raise RevisionConflict("explicit_update_confirmation_required")
        with store.connection:
            store.connection.execute("BEGIN IMMEDIATE")
            value = candidate(store, project_id, body)
            if body.candidate_catalog_version_id == body.previous_catalog_version_id:
                raise RevisionConflict("catalogue_update_requires_distinct_versions")
            preview = plan(store, project_id, body, value)
            if preview["previous_sha256"] == preview["candidate_sha256"]:
                raise RevisionConflict("catalogue_update_requires_distinct_versions")
            if (
                preview["sha256"] != body.expected_sha256
                or preview["requirement_version_reuse_conflicts"]
            ):
                raise RevisionConflict(
                    "catalogue_update_preview_changed_or_requirement_version_reused"
                )
            if body.activate and not preview["can_activate"]:
                raise RevisionConflict("resolve_update_mappings_and_active_overrides_first")
            cid = store.bind_catalog(project_id, value, activate=False)
            prior = store.connection.execute(
                "SELECT content FROM catalog_updates WHERE project_id=? AND catalog_version_id=?",
                (project_id, cid),
            ).fetchone()
            frozen = store.connection.execute(
                "SELECT 1 FROM run_catalogs WHERE project_id=? AND catalog_version_id=? LIMIT 1",
                (project_id, cid),
            ).fetchone()
            if (
                prior
                and json.loads(prior[0]).get("can_activate")
                and json.loads(prior[0]) != preview
            ):
                if frozen:
                    raise RevisionConflict("update_receipt_frozen_create_new_catalogue_version")
            store.connection.execute(
                "INSERT INTO catalog_updates(project_id,catalog_version_id,content,sha256) VALUES(?,?,?,?) ON CONFLICT(project_id,catalog_version_id) DO UPDATE SET content=excluded.content,sha256=excluded.sha256",
                (project_id, cid, json.dumps(preview, ensure_ascii=False), digest(preview)),
            )
            if body.activate:
                store.set_catalog_binding(project_id, cid, enabled=True)
                store.set_catalog_binding(
                    project_id, body.previous_catalog_version_id, enabled=False
                )
            if body.import_id:
                store.connection.execute(
                    "UPDATE catalog_imports SET committed_id=? WHERE project_id=? AND id=?",
                    (cid, project_id, body.import_id),
                )
            store.audit(
                project_id,
                "catalogue_update_activated"
                if body.activate
                else "catalogue_update_installed_inactive",
                cid,
            )
        return {
            "id": cid,
            "activated": body.activate,
            "history_preserved": True,
            "receipt": preview,
        }

    @app.get("/api/v1/projects/{project_id}/catalogs/{catalog_id}/update-receipt")
    def receipt(project_id: str, catalog_id: str, store: Store = Depends(db)):
        row = store.connection.execute(
            "SELECT content,sha256 FROM catalog_updates WHERE project_id=? AND catalog_version_id=?",
            (project_id, catalog_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, translate("Update/adoption receipt не найдена в проекте."))
        return {"content": json.loads(row[0]), "sha256": row[1]}
