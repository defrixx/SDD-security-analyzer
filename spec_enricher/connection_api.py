"""Connection, secret-reference, egress-policy and actual test endpoints."""

from __future__ import annotations

import ipaddress
import json

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .connections import ConnectionProfile, DataScope
from .i18n import translate
from .llm import LLMAdapter
from .mcp import MCPBroker, MCPError
from .network import NetworkError, endpoint_parts, external_egress_denied, resolve_host
from .registry import Registry, RevisionConflict
from .security import error
from .storage import Store


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SecretInput(Input):
    value: str = Field(min_length=1, max_length=4096)


class ProfileUpdate(Input):
    expected_revision: int = Field(ge=1)
    profile: ConnectionProfile


class GrantInput(Input):
    confirmed: bool
    revision: int = Field(ge=1)
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    data_scopes: list[DataScope] = Field(max_length=5)


class CallInput(Input):
    revision: int = Field(ge=1)
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict


class OperatorPolicyInput(Input):
    allow_external_egress: bool
    confirmed: bool


class ResolveInput(Input):
    endpoint: str = Field(min_length=8, max_length=2048)


def install(app, db, project_exists, secret_store):
    @app.post("/api/v1/connection-diagnostics/resolve")
    def resolve(body: ResolveInput, store: Store = Depends(db)):
        parts, port = endpoint_parts(body.endpoint)
        local_host = parts.hostname in {"localhost", "host.docker.internal"}
        try:
            literal = ipaddress.ip_address(parts.hostname)
            local_host = not literal.is_global
        except ValueError:
            pass
        if not local_host and not Registry(store).external_egress_allowed():
            return error(
                403,
                "egress_policy_blocked",
                translate("Внешний DNS lookup запрещён политикой установки."),
            )
        try:
            addresses = resolve_host(parts.hostname, port)
        except NetworkError:
            return error(
                422,
                "dns_failed",
                translate(
                    "Backend не смог разрешить адрес. В Docker localhost относится к контейнеру; для LM Studio на хосте проверьте host.docker.internal."
                ),
            )
        return {
            "host": parts.hostname,
            "port": port,
            "addresses": addresses,
            "permission_granted": False,
            "notice": translate(
                "Это результат DNS из backend. Доступ не разрешён автоматически; private IP нужно явно принять только для данного endpoint."
            ),
        }

    @app.get("/api/v1/operator/network-policy")
    def policy(store: Store = Depends(db)):
        row = store.connection.execute(
            "SELECT value FROM instance_settings WHERE key='allow_external_egress'"
        ).fetchone()
        return {
            "allow_external_egress": Registry(store).external_egress_allowed(),
            "saved_allow_external_egress": row is not None and row[0] == "true",
            "operator_locked": external_egress_denied(),
            "notice": translate(
                "Дополнительно требуется отдельное принятие destination/data scope каждого проекта."
            ),
        }

    @app.put("/api/v1/operator/network-policy")
    def update_policy(body: OperatorPolicyInput, store: Store = Depends(db)):
        if not body.confirmed:
            raise HTTPException(409, translate("Подтвердите изменение политики внешних обращений."))
        if body.allow_external_egress and external_egress_denied():
            raise HTTPException(
                403,
                translate(
                    "Внешние обращения запрещены настройками запуска сервиса. Изменение из интерфейса недоступно."
                ),
            )
        with store.connection:
            store.connection.execute(
                "UPDATE instance_settings SET value=? WHERE key='allow_external_egress'",
                ("true" if body.allow_external_egress else "false",),
            )
        return {"allow_external_egress": body.allow_external_egress}

    @app.post("/api/v1/projects/{project_id}/secrets", status_code=201)
    def put_secret(project_id: str, body: SecretInput, store: Store = Depends(db)):
        project_exists(project_id, store)
        reference = secret_store.put(project_id, body.value)
        store.audit(project_id, "session_secret_added", reference)
        return {
            "secret_ref": reference,
            "storage": "session_only",
            "notice": translate(
                "Значение не сохраняется в конфигурации и исчезнет после перезапуска."
            ),
        }

    @app.delete("/api/v1/projects/{project_id}/secrets/{reference}")
    def remove_secret(project_id: str, reference: str, store: Store = Depends(db)):
        project_exists(project_id, store)
        secret_store.remove(project_id, reference)
        store.audit(project_id, "session_secret_removed", reference)
        return {"removed": True}

    @app.post("/api/v1/projects/{project_id}/connections", status_code=201)
    def create_connection(project_id: str, body: ConnectionProfile, store: Store = Depends(db)):
        project_exists(project_id, store)
        return Registry(store).save(project_id, "connection", body.model_dump())

    @app.put("/api/v1/projects/{project_id}/connections/{connection_id}")
    def update_connection(
        project_id: str, connection_id: str, body: ProfileUpdate, store: Store = Depends(db)
    ):
        try:
            return Registry(store).save(
                project_id,
                "connection",
                body.profile.model_dump(),
                object_id=connection_id,
                expected_revision=body.expected_revision,
            )
        except RevisionConflict:
            return error(
                409,
                "revision_conflict",
                translate("Профиль изменён или не найден в выбранном проекте; обновите preview."),
            )

    @app.get("/api/v1/projects/{project_id}/connections")
    def connections(project_id: str, store: Store = Depends(db)):
        project_exists(project_id, store)
        return Registry(store).entries(project_id, "connection")

    @app.get("/api/v1/projects/{project_id}/connections/{connection_id}")
    def connection(
        project_id: str, connection_id: str, revision: int | None = None, store: Store = Depends(db)
    ):
        record = Registry(store).get(project_id, "connection", connection_id, revision)
        if record is None:
            raise HTTPException(404, translate("Профиль не найден в проекте."))
        return record

    @app.post("/api/v1/projects/{project_id}/connections/{connection_id}/test")
    def test_connection(project_id: str, connection_id: str, store: Store = Depends(db)):
        registry = Registry(store)
        record = registry.get(project_id, "connection", connection_id)
        if record is None:
            raise HTTPException(404, translate("Профиль не найден в проекте."))
        try:
            _, profile = registry.require_egress(
                project_id, connection_id, record["revision"], set(), synthetic=True
            )
        except ValueError:
            return error(
                403,
                "egress_policy_blocked",
                translate(
                    "Внешние обращения запрещены настройками установки. Синтетический тест не выполнен."
                ),
            )
        adapter = (
            LLMAdapter(project_id, profile, secret_store)
            if profile.kind == "llm"
            else MCPBroker(
                project_id,
                profile,
                secret_store,
                authorize=lambda: registry.require_egress(
                    project_id, connection_id, record["revision"], set(), synthetic=True
                ),
            )
        )
        report = adapter.capability_test()
        registry.report(project_id, connection_id, record["revision"], report)
        return {"id": connection_id, "revision": record["revision"], "result": report}

    @app.post("/api/v1/projects/{project_id}/connections/{connection_id}/reload-credentials")
    def reload_credentials(project_id: str, connection_id: str, store: Store = Depends(db)):
        registry = Registry(store)
        record = registry.get(project_id, "connection", connection_id)
        if record is None:
            raise HTTPException(404, translate("Профиль не найден в проекте."))
        profile = ConnectionProfile.model_validate(record["content"])
        references = list(profile.secret_headers.values()) + (
            [profile.secret_ref] if profile.secret_ref else []
        )
        files = [reference for reference in references if reference.startswith("file:")]
        for reference in files:
            secret_store.reload_file(project_id, reference)
        for related_id, revision, payload in store.connection.execute(
            "SELECT id,revision,content FROM registry_versions WHERE project_id=? AND kind='connection'",
            (project_id,),
        ):
            config = json.loads(payload)
            used = list(config.get("secret_headers", {}).values()) + [config.get("secret_ref")]
            if set(files) & set(used):
                registry.report(
                    project_id,
                    related_id,
                    revision,
                    {"status": "failed", "error": "credentials_reloaded_test_required"},
                )
        store.audit(project_id, "credentials_reloaded", connection_id)
        return {"reloaded_references": files, "capability_test_required": True}

    @app.post("/api/v1/projects/{project_id}/connections/{connection_id}/grants")
    def grant(project_id: str, connection_id: str, body: GrantInput, store: Store = Depends(db)):
        if not body.confirmed:
            raise HTTPException(
                409, translate("Подтвердите destination и классы передаваемых данных.")
            )
        try:
            Registry(store).grant(
                project_id, connection_id, body.revision, body.expected_sha256, body.data_scopes
            )
        except RevisionConflict:
            return error(
                409,
                "revision_conflict",
                translate("Конфигурация изменилась; требуется новое принятие."),
            )
        return {"granted_scopes": body.data_scopes, "revision": body.revision}

    @app.delete("/api/v1/projects/{project_id}/connections/{connection_id}/grants")
    def revoke(project_id: str, connection_id: str, store: Store = Depends(db)):
        if Registry(store).get(project_id, "connection", connection_id) is None:
            raise HTTPException(404, translate("Подключение не найдено в проекте."))
        with store.connection:
            store.connection.execute(
                "DELETE FROM egress_grants WHERE project_id=? AND kind='connection' AND id=?",
                (project_id, connection_id),
            )
        store.audit(project_id, "connection_scope_revoked_all_revisions", connection_id)
        return {"revoked_all_revisions": True}

    @app.post("/api/v1/projects/{project_id}/connections/{connection_id}/tools/call")
    def call_tool(project_id: str, connection_id: str, body: CallInput, store: Store = Depends(db)):
        registry = Registry(store)
        try:
            _, profile = registry.require_egress(
                project_id, connection_id, body.revision, {"source_queries"}
            )
        except ValueError:
            return error(
                403,
                "egress_scope_blocked",
                translate(
                    "Нужно включить профиль, проверить подключение и принять destination/source_queries scope."
                ),
            )
        if profile.kind != "mcp":
            raise HTTPException(422, translate("Профиль не является MCP."))
        try:
            result = MCPBroker(
                project_id,
                profile,
                secret_store,
                authorize=lambda: registry.require_egress(
                    project_id, connection_id, body.revision, {"source_queries"}
                ),
            ).call(body.tool, body.arguments)
        except (MCPError, NetworkError, ValueError):
            store.audit(project_id, "mcp_call_rejected", connection_id)
            return error(
                422,
                "mcp_call_rejected",
                translate(
                    "Вызов отклонён: проверьте allowlist, schema/argument scope, budgets и ответ сервера."
                ),
            )
        store.audit(project_id, "mcp_tool_read", connection_id)
        return result
