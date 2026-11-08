"""Versioned prompt/skill editing, diff, rollback and explicit fixture tests."""

from __future__ import annotations

import difflib
import json
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import Field

from .i18n import translate
from .llm import LLMAdapter, ModelError
from .network import NetworkError
from .pipeline_config import CONTRACTS, PromptDefinition, SkillDefinition, preview_prompt
from .prompts import defaults
from .registry import Registry
from .run_contracts import VersionRef
from .stage_contracts import Contract
from .storage import Store

Kind = Literal["prompt", "skill", "pipeline"]


class ConfigWrite(Contract):
    content: dict
    expected_revision: int | None = Field(default=None, ge=1)


class RollbackInput(Contract):
    expected_revision: int = Field(ge=1)
    source_revision: int = Field(ge=1)


class PromptPreview(Contract):
    content: PromptDefinition
    data: dict = Field(default_factory=lambda: {"synthetic": True})


class FixtureTest(Contract):
    connection: VersionRef
    revision: int = Field(ge=1)


def install(app, db, project_exists, secrets):
    @app.get("/api/v1/projects/{project_id}/registry/{kind}/{object_id}/revisions/{revision}")
    def pinned_config(
        project_id: str,
        kind: Literal["connection", "source", "prompt", "skill", "pipeline"],
        object_id: str,
        revision: int,
        store: Store = Depends(db),
    ):
        project_exists(project_id, store)
        value = Registry(store).get(project_id, kind, object_id, revision)
        if value is None:
            raise HTTPException(404, translate("Закреплённая revision не найдена в проекте."))
        return value

    @app.get("/api/v1/pipeline-defaults")
    def initial():
        return {
            "prompts": defaults(),
            "contracts": {
                stage: contract.model_json_schema() for stage, contract in CONTRACTS.items()
            },
            "allowed_variables": ["stage", "output_contract"],
            "data_transport": "Untrusted data stays in separate user message; not interpolated into system instruction.",
            "skills": "Declarative prompts only. No tools, commands or permission changes.",
        }

    @app.post("/api/v1/prompt-preview")
    def preview(body: PromptPreview):
        return preview_prompt(body.content.model_dump(), body.data)

    @app.get("/api/v1/projects/{project_id}/configuration/{kind}")
    def entries(project_id: str, kind: Kind, store: Store = Depends(db)):
        project_exists(project_id, store)
        return Registry(store).entries(project_id, kind)

    @app.post("/api/v1/projects/{project_id}/configuration/{kind}", status_code=201)
    def create(project_id: str, kind: Kind, body: ConfigWrite, store: Store = Depends(db)):
        project_exists(project_id, store)
        if body.expected_revision is not None:
            raise ValueError("new_configuration_has_no_expected_revision")
        return Registry(store).save(project_id, kind, body.content)

    @app.put("/api/v1/projects/{project_id}/configuration/{kind}/{object_id}")
    def update(
        project_id: str, kind: Kind, object_id: str, body: ConfigWrite, store: Store = Depends(db)
    ):
        return Registry(store).save(
            project_id,
            kind,
            body.content,
            object_id=object_id,
            expected_revision=body.expected_revision,
        )

    @app.get("/api/v1/projects/{project_id}/configuration/{kind}/{object_id}/versions")
    def versions(project_id: str, kind: Kind, object_id: str, store: Store = Depends(db)):
        rows = store.connection.execute(
            "SELECT revision,created_at FROM registry_versions WHERE project_id=? AND kind=? AND id=? ORDER BY revision DESC",
            (project_id, kind, object_id),
        ).fetchall()
        if not rows:
            raise HTTPException(404, translate("Конфигурация не найдена в проекте."))
        return [
            Registry(store).get(project_id, kind, object_id, revision) | {"created_at": created}
            for revision, created in rows
        ]

    @app.get("/api/v1/projects/{project_id}/configuration/{kind}/{object_id}/diff")
    def diff(
        project_id: str,
        kind: Kind,
        object_id: str,
        before: int,
        after: int,
        store: Store = Depends(db),
    ):
        registry = Registry(store)
        values = [
            registry.get(project_id, kind, object_id, revision) for revision in (before, after)
        ]
        if None in values:
            raise HTTPException(404, translate("Версия не найдена в проекте."))
        texts = [
            json.dumps(value["content"], ensure_ascii=False, indent=2).splitlines(keepends=True)
            for value in values
        ]
        return {
            "diff": "".join(
                difflib.unified_diff(texts[0], texts[1], fromfile=str(before), tofile=str(after))
            )
        }

    @app.post("/api/v1/projects/{project_id}/configuration/{kind}/{object_id}/rollback")
    def rollback(
        project_id: str, kind: Kind, object_id: str, body: RollbackInput, store: Store = Depends(db)
    ):
        registry = Registry(store)
        original = registry.get(project_id, kind, object_id, body.source_revision)
        if original is None:
            raise HTTPException(404, translate("Историческая версия не найдена."))
        return registry.save(
            project_id,
            kind,
            original["content"],
            object_id=object_id,
            expected_revision=body.expected_revision,
        )

    @app.post("/api/v1/projects/{project_id}/configuration/{kind}/{object_id}/test")
    def test(
        project_id: str,
        kind: Literal["prompt", "skill"],
        object_id: str,
        body: FixtureTest,
        store: Store = Depends(db),
    ):
        registry = Registry(store)
        record = registry.get(project_id, kind, object_id, body.revision)
        if record is None:
            raise HTTPException(404, translate("Версия не найдена в проекте."))
        _, profile = registry.require_egress(
            project_id, body.connection.id, body.connection.revision, set(), synthetic=True
        )
        if profile.kind != "llm":
            raise ValueError("fixture_test_requires_llm")
        content = record["content"]
        if kind == "skill":
            skill = SkillDefinition.model_validate(content)
            prompt = registry.get(
                project_id, "prompt", skill.prompt_ref.id, skill.prompt_ref.revision
            )
            if prompt is None:
                raise ValueError("skill_prompt_missing")
            content = prompt["content"]
        preview = preview_prompt(
            content,
            {
                "synthetic": True,
                "instruction": "No product or expected/oracle data supplied. Return an empty contract where allowed; unknown facts require questions.",
            },
        )
        messages = preview["messages"]
        messages[1]["content"] = json.dumps(messages[1]["content"])
        store.audit(project_id, "explicit_synthetic_configuration_test", object_id)
        try:
            result = LLMAdapter(project_id, profile, secrets).complete(
                messages, preview["output_schema"]
            )
            return {
                "status": "passed_schema_only",
                "semantic_quality": "not_assessed",
                "synthetic_only": True,
                "output": result.value,
                "usage": result.usage,
                "revision": body.revision,
            }
        except (ModelError, NetworkError, ValueError) as exc:
            return {
                "status": "failed",
                "synthetic_only": True,
                "error": getattr(exc, "code", "model_contract_rejected"),
                "revision": body.revision,
            }
