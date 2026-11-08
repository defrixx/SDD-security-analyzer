"""Same-origin local API, with project-scoped resource resolution."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

from .artifacts import ArtifactStore
from .authoring_api import install as install_authoring
from .builtin import load_verified
from .catalog import ROOT, canonical, migration_preview
from .catalog_api import install as install_catalog_management
from .config_api import install as install_configuration
from .connection_api import install as install_connections
from .i18n import translate
from .inputs import InputError
from .installation import Lease, active_data
from .lifecycle import Lifecycle
from .lifecycle_api import install as install_lifecycle
from .network import external_egress_denied
from .parser_worker import parse_bounded
from .registry import RevisionConflict
from .run_api import install as install_runs
from .schema_worker import SchemaValidationUnavailable
from .secrets import SecretReferenceError, SecretStore
from .security import LocalSecurity, Sessions, create_bootstrap, error
from .settings_api import install as install_settings
from .snapshots import sha256
from .source_api import install as install_sources
from .startup import initialize as initialize_configuration
from .storage import Store


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectInput(DTO):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)


class BootstrapInput(DTO):
    token: str = Field(min_length=32, max_length=128)


class Confirmation(DTO):
    confirmed: bool
    expected_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class BindingInput(DTO):
    enabled: bool = False
    remove: bool = False
    expected_preview_sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")


def rows(values, keys):
    return [dict(zip(keys, row, strict=True)) for row in values]


def create_app(data_dir: Path | None = None, origin: str | None = None, *, adapter_factory=None):
    data_dir = data_dir or Path(os.environ.get("APP_DATA_DIR", ".local-data"))
    # A second server must not rotate bootstrap tokens or mark a live worker
    # interrupted. Backup remains compatible with the shared service lease;
    # restore requires its exclusive counterpart.
    with ExitStack() as leases:
        leases.enter_context(Lease(data_dir, ".server.lock", exclusive=True))
        leases.enter_context(Lease(data_dir, ".service.lock", exclusive=False))
        app = _create_app(data_dir, origin, adapter_factory=adapter_factory)
        app.state.application_leases = leases.pop_all()
        return app


def _create_app(data_dir: Path | None = None, origin: str | None = None, *, adapter_factory=None):
    external_egress_denied()  # Reject malformed operator policy before DB/token changes.
    data_dir = data_dir or Path(os.environ.get("APP_DATA_DIR", ".local-data"))
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    installation_root = data_dir
    data_dir = active_data(installation_root)
    origin = origin or os.environ.get("APP_ALLOWED_ORIGIN", "http://127.0.0.1:8080")
    database = data_dir / "app.sqlite3"
    store = Store(database)
    try:
        initialize_configuration(store, Path(os.environ.get("APP_CONFIG_DIR", "config")))
    finally:
        store.close()
    create_bootstrap(data_dir)
    sessions = Sessions(data_dir)
    app = FastAPI(
        title=translate("Обогащение спецификаций"), version="1.0.0", docs_url=None, redoc_url=None
    )

    @asynccontextmanager
    async def lifecycle(application):
        try:
            yield
        finally:
            application.state.application_leases.close()

    app.router.lifespan_context = lifecycle
    app.state.installation_root = installation_root
    app.state.data_dir = data_dir
    app.state.sessions = sessions
    app.state.secrets = SecretStore()
    app.add_middleware(
        LocalSecurity, origin=origin, sessions=sessions, secret_store=app.state.secrets
    )

    def db():
        store = Store(database)
        try:
            yield store
        finally:
            store.close()

    def project_exists(project_id: str, store: Store):
        if not store.connection.execute(
            "SELECT 1 FROM projects WHERE id=?", (project_id,)
        ).fetchone():
            raise HTTPException(404, translate("Проект не найден."))

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(exc.status_code, "request_rejected", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error(
            422,
            "validation_error",
            translate("Проверьте поля запроса."),
            [{"path": list(e["loc"]), "type": e["type"]} for e in exc.errors()],
        )

    @app.exception_handler(InputError)
    async def input_error(request, exc):
        return error(
            422,
            "invalid_input",
            translate("Файл отклонён: проверьте формат, кодировку, структуру и лимиты."),
        )

    @app.exception_handler(SecretReferenceError)
    async def credential_error(request, exc):
        return error(
            422,
            "credential_reference_unavailable",
            translate(
                "Проверьте credential reference, доступ к отдельному secret mount и повторите capability test. Значение ключа не отображается."
            ),
        )

    @app.exception_handler(SchemaValidationUnavailable)
    async def schema_unavailable(request, exc):
        return error(
            503,
            "local_schema_validation_unavailable",
            translate(
                "Локальная JSON Schema проверка не завершена. Результат не подтверждён; проверьте ресурсы и схему, затем повторите действие явно."
            ),
        )

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        catalogue_actions = {
            "catalogue_update_clause_mapping_required": translate(
                "Откройте preview обновления и задайте old→new clause mappings перед активацией."
            ),
            "catalogue_update_retirement_review_required": translate(
                "Откройте preview обновления и подтвердите mappings или причины удаления требований."
            ),
            "catalogue_update_local_conflict_review_required": translate(
                "Preview обновления обнаружил конфликт активного override/dependency. Примите новую версию отдельно после разрешения конфликта."
            ),
            "catalogue_update_review_required": translate(
                "Версия установлена неактивной: завершите preview обновления и примите mappings/conflicts."
            ),
            "immutable_requirement_version_conflict": translate(
                "Нормативное содержание под этим ID/version уже существует. Создайте новую version требования."
            ),
            "catalog ID/version conflict: create a reviewed new version": translate(
                "ID/version каталога уже существуют с другим содержимым. Создайте новую catalogue version."
            ),
        }
        if str(exc) in catalogue_actions:
            return error(409, "catalogue_version_review_required", catalogue_actions[str(exc)])
        return error(
            422, "invalid_value", translate("Значение или операция не соответствует контракту.")
        )

    @app.exception_handler(RevisionConflict)
    async def revision_error(request, exc):
        return error(
            409,
            "revision_conflict",
            translate(
                "Конфигурация изменена или не найдена в выбранном проекте; обновите preview."
            ),
        )

    @app.exception_handler(ValidationError)
    async def contract_error(request, exc):
        return error(
            422,
            "configuration_validation",
            translate("Исправьте поля конфигурации."),
            [{"path": list(item["loc"]), "type": item["type"]} for item in exc.errors()],
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def database_write_error(request, exc):
        if str(exc) == "maintenance_mode":
            return error(
                503,
                "maintenance_mode",
                translate("Идёт согласованный backup. Изменения временно запрещены."),
            )
        return error(409, "storage_conflict", translate("Конфликт сохранения; обновите состояние."))

    @app.get("/health")
    def health():
        return {"status": "ok", "app_version": "1.0.0"}

    @app.post("/api/v1/session/bootstrap")
    def bootstrap(body: BootstrapInput):
        session = sessions.bootstrap(body.token)
        if session is None:
            return error(
                401,
                "invalid_bootstrap",
                translate("Одноразовая ссылка недействительна или устарела."),
            )
        response = JSONResponse({"csrf_token": session[1]})
        response.set_cookie(
            "session",
            session[0],
            httponly=True,
            samesite="strict",
            secure=origin.startswith("https:"),
            max_age=12 * 60 * 60,
            path="/",
        )
        return response

    @app.get("/api/v1/session")
    def current_session(request: Request):
        session = sessions.get(request.cookies.get("session", ""))
        return {"csrf_token": session[0], "origin": origin}

    @app.get("/api/v1/projects")
    def projects(store: Store = Depends(db)):
        return rows(store.projects(), ["id", "name", "description"])

    @app.post("/api/v1/projects", status_code=201)
    def add_project(body: ProjectInput, store: Store = Depends(db)):
        project_id = store.create_project(body.name, body.description)
        store.audit(project_id, "project_created", project_id)
        return {"id": project_id}

    @app.get("/api/v1/projects/{project_id}")
    def project(project_id: str, store: Store = Depends(db)):
        project_exists(project_id, store)
        row = store.connection.execute(
            "SELECT id,name,description FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        return dict(zip(["id", "name", "description"], row, strict=True))

    @app.patch("/api/v1/projects/{project_id}")
    def update_project(project_id: str, body: ProjectInput, store: Store = Depends(db)):
        project_exists(project_id, store)
        with store.connection:
            store.connection.execute(
                "UPDATE projects SET name=?,description=? WHERE id=?",
                (body.name, body.description, project_id),
            )
        store.audit(project_id, "project_updated", project_id)
        return {"id": project_id}

    @app.get("/api/v1/projects/{project_id}/documents")
    def documents(project_id: str, store: Store = Depends(db)):
        project_exists(project_id, store)
        return rows(
            store.document_metadata(project_id),
            ["id", "name", "role", "format", "size", "sha256", "active"],
        )

    @app.post("/api/v1/projects/{project_id}/documents", status_code=201)
    async def upload_document(
        project_id: str,
        request: Request,
        filename: str = Query(max_length=255),
        role: Literal["master_spec", "context", "delta"] = "master_spec",
        store: Store = Depends(db),
    ):
        project_exists(project_id, store)
        fmt = filename.rsplit(".", 1)[-1].lower()
        if fmt not in {"md", "json", "yaml", "yml"} or (role == "delta" and fmt != "md"):
            raise HTTPException(
                422, translate("Поддерживаются Markdown, JSON и YAML; delta — только Markdown.")
            )
        data = await request.body()
        if len(data) > (100 * 1024 if role == "delta" else 1024 * 1024):
            raise HTTPException(413, translate("Превышен размер файла."))
        if fmt in {"json", "yaml", "yml"}:
            await run_in_threadpool(parse_bounded, data, fmt)
        doc_id = store.add_document(
            project_id,
            filename,
            "delta" if role == "delta" else "baseline",
            data,
            role=role,
            fmt=fmt,
        )
        store.audit(project_id, "document_uploaded", doc_id)
        return {
            "id": doc_id,
            "sha256": sha256(data),
            "size": len(data),
            "role": role,
            "format": fmt,
            "parse_outcome": "parsed",
        }

    @app.get("/api/v1/projects/{project_id}/documents/{document_id}")
    def document(project_id: str, document_id: str, store: Store = Depends(db)):
        row = store.document(project_id, document_id)
        if row is None:
            raise HTTPException(404, translate("Снимок не найден в проекте."))
        store.audit(project_id, "document_read", document_id)
        return Response(
            row[2], media_type="text/plain; charset=utf-8", headers={"X-Content-SHA256": row[3]}
        )

    @app.get("/api/v1/catalog-templates")
    def templates(
        format: Literal["json", "yaml"] = "json", kind: Literal["blank", "example"] = "blank"
    ):
        path = (
            ROOT
            / "templates"
            / ("requirements." + ("template" if kind == "blank" else "example") + "." + format)
        )
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.get("/api/v1/builtin-catalog")
    def builtin_metadata():
        data = (ROOT / "catalog/requirements.full.json").read_bytes()
        value = json.loads(data)
        return {
            key: value[key]
            for key in [
                "catalog_id",
                "catalog_version",
                "title",
                "ready_for_use",
                "release_status",
                "scope",
                "origin",
                "limitations",
            ]
        } | {
            "sha256": sha256(data),
            "records": len(value["requirements"]),
            "active_records": sum(r["status"] == "active" for r in value["requirements"]),
            "review_scope": translate(
                "Targeted review источников; не гарантия полного покрытия и безошибочности."
            ),
        }

    @app.post("/api/v1/projects/{project_id}/builtin-catalog")
    async def bind_builtin(project_id: str, body: Confirmation, store: Store = Depends(db)):
        project_exists(project_id, store)
        try:
            data, _ = load_verified()
        except (ValueError, OSError):
            return error(
                422,
                "builtin_integrity",
                translate(
                    "Hash принятого набора или его источников изменился; требуется новая проверка."
                ),
            )
        if not body.confirmed or body.expected_sha256 != sha256(data):
            raise HTTPException(
                409, translate("Требуется принятие показанной версии и её текущего hash.")
            )
        value, issues = await run_in_threadpool(parse_bounded, data, "json", catalog=True)
        if issues or not value["ready_for_use"]:
            return error(
                422, "builtin_blocked", translate("Встроенный набор не готов к включению."), issues
            )
        catalog_id = store.bind_catalog(project_id, value, built_in=True)
        store.audit(project_id, "builtin_bound", catalog_id)
        return {"id": catalog_id, "records": len(value["requirements"])}

    @app.post("/api/v1/projects/{project_id}/catalog-imports/preview")
    async def preview_catalog(
        project_id: str,
        request: Request,
        format: Literal["json", "yaml", "yml"] = "json",
        store: Store = Depends(db),
    ):
        project_exists(project_id, store)
        value, issues = await run_in_threadpool(
            parse_bounded, await request.body(), format, catalog=True
        )
        if not isinstance(value, dict):
            raise HTTPException(422, translate("Ожидается объект каталога."))
        if value.get("schema_version") in {"1.0", "1.1", "2.0", "3.0"}:
            return migration_preview(value)
        if not issues:
            issues.extend(store.catalog_identity_conflicts(project_id, value))
        import_id = store.stage_catalog(project_id, value, issues)
        return {
            "id": import_id,
            "sha256": sha256(canonical(value)),
            "issues": issues,
            "records": len(value.get("requirements", [])),
            "activation_requires_confirmation": True,
            "notice": translate(
                "Imported ready/review flags не являются вашим принятием или проверкой смысла."
            ),
        }

    @app.get("/api/v1/projects/{project_id}/catalog-imports/{import_id}")
    def staged_preview(project_id: str, import_id: str, store: Store = Depends(db)):
        row = store.staged_catalog(project_id, import_id)
        if row is None:
            raise HTTPException(404, translate("Preview не найден в проекте."))
        value = json.loads(row[0])
        return {
            "id": import_id,
            "sha256": row[1],
            "issues": json.loads(row[2]),
            "records": len(value.get("requirements", [])),
            "catalog_id": value.get("catalog_id"),
            "version": value.get("catalog_version"),
            "origin": value.get("origin"),
            "limitations": value.get("limitations"),
            "requirement_ids_preview": [
                record["id"] for record in value.get("requirements", [])[:25]
            ],
            "activation_requires_confirmation": True,
            "notice": translate(
                "Imported review/ready flags не доказывают semantic review и не заменяют ваше принятие scope."
            ),
        }

    @app.post("/api/v1/projects/{project_id}/catalog-imports/{import_id}/commit")
    def commit_catalog(
        project_id: str, import_id: str, body: Confirmation, store: Store = Depends(db)
    ):
        row = store.staged_catalog(project_id, import_id)
        if row is None:
            raise HTTPException(404, translate("Preview не найден в проекте."))
        if not body.confirmed or row[1] != body.expected_sha256:
            raise HTTPException(409, translate("Принятие или hash preview не совпадает."))
        if json.loads(row[2]):
            return error(
                422,
                "catalog_invalid",
                translate("Исправьте ошибки каталога до включения."),
                json.loads(row[2]),
            )
        if row[3]:
            return {"id": row[3]}
        with store.connection:
            store.connection.execute("BEGIN IMMEDIATE")
            cid = store.bind_catalog(project_id, json.loads(row[0]))
            store.connection.execute(
                "UPDATE catalog_imports SET committed_id=? WHERE project_id=? AND id=?",
                (cid, project_id, import_id),
            )
            store.audit(project_id, "catalog_imported", cid)
        return {"id": cid}

    @app.get("/api/v1/projects/{project_id}/catalogs")
    def catalogs(project_id: str, store: Store = Depends(db)):
        project_exists(project_id, store)
        return rows(
            store.catalogs(project_id),
            ["id", "catalog_id", "version", "sha256", "built_in", "enabled", "records"],
        )

    @app.patch("/api/v1/projects/{project_id}/catalogs/{catalog_id}/binding")
    def binding(project_id: str, catalog_id: str, body: BindingInput, store: Store = Depends(db)):
        if store.catalog(project_id, catalog_id) is None:
            raise HTTPException(404, translate("Каталог не найден в проекте."))
        with store.connection:
            store.connection.execute("BEGIN IMMEDIATE")
            if body.remove:
                plan = Lifecycle(
                    store, ArtifactStore(data_dir / "artifacts"), data_dir / "exports"
                ).binding_preview(project_id, catalog_id)
                if body.expected_preview_sha256 != plan["sha256"]:
                    raise RevisionConflict("binding_preview_required_or_changed")
            store.set_catalog_binding(
                project_id, catalog_id, enabled=body.enabled, remove=body.remove
            )
            store.audit(project_id, "catalog_binding_changed", catalog_id)
        return {"id": catalog_id, "history_preserved": True}

    @app.get("/api/v1/projects/{project_id}/catalogs/{catalog_id}/binding-preview")
    def binding_preview(project_id: str, catalog_id: str, store: Store = Depends(db)):
        if store.catalog(project_id, catalog_id) is None:
            raise HTTPException(404, translate("Каталог не найден в проекте."))
        return Lifecycle(
            store, ArtifactStore(data_dir / "artifacts"), data_dir / "exports"
        ).binding_preview(project_id, catalog_id)

    @app.get("/api/v1/projects/{project_id}/catalogs/{catalog_id}/requirements")
    def requirements(
        project_id: str,
        catalog_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        domain: str = "",
        profile: str = "",
        level: str = "",
        source_kind: str = "",
        store: Store = Depends(db),
    ):
        return rows(
            store.requirements(
                project_id,
                catalog_id,
                offset=offset,
                limit=limit,
                domain=domain,
                profile=profile,
                level=level,
                source_kind=source_kind,
            ),
            ["id", "version", "title", "domain", "subdomain", "level", "source_kind", "status"],
        )

    @app.get("/api/v1/projects/{project_id}/catalogs/{catalog_id}/requirements/{requirement_id}")
    def requirement(
        project_id: str, catalog_id: str, requirement_id: str, store: Store = Depends(db)
    ):
        record = store.requirement(project_id, catalog_id, requirement_id)
        if record is None:
            raise HTTPException(404, translate("Требование не найдено в проекте."))
        return record

    install_connections(app, db, project_exists, app.state.secrets)
    install_configuration(app, db, project_exists, app.state.secrets)
    install_catalog_management(app, db, project_exists)
    install_authoring(app, db, project_exists)
    install_sources(app, db, project_exists, app.state.secrets)
    install_settings(app, db, app.state.secrets)
    install_runs(app, db, project_exists, data_dir, app.state.secrets, adapter_factory)
    install_lifecycle(app, db, project_exists, data_dir, app.state.secrets)
    app.mount("/assets", StaticFiles(directory=ROOT / "spec_enricher/static"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "spec_enricher/static/index.html")

    return app
