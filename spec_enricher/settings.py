"""Portable configuration only; importing never grants data/network permissions."""

from __future__ import annotations

import json
import uuid
from typing import Literal

from pydantic import Field

from .catalog import canonical, digest
from .pipeline_config import CONTRACTS
from .prompts import defaults
from .registry import normalize
from .sources import DEFAULT_EXCLUSIONS
from .stage_contracts import Contract
from .storage import Store
from .workers import bounded

ANNOTATIONS = {
    "type": "object",
    "properties": {"annotations": {"type": "array", "items": {"type": "string"}}},
    "required": ["annotations"],
    "additionalProperties": False,
}


class ConfigVersion(Contract):
    kind: Literal["connection", "source", "prompt", "skill", "pipeline"]
    id: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1)
    content: dict
    unresolved: dict = Field(default_factory=dict)


class DatasetReference(Contract):
    catalog_id: str
    version: str
    schema_version: Literal["4.0"]
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    content: dict | None = None


class ProjectSettings(Contract):
    alias: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)
    configuration: list[ConfigVersion] = Field(max_length=5000)
    catalogues: list[DatasetReference] = Field(max_length=100)


class SettingsBundle(Contract):
    schema_version: Literal[1]
    app_version: Literal["1.0.0"]
    sanitized: bool
    projects: list[ProjectSettings] = Field(min_length=1, max_length=100)
    operator_preferences: dict = Field(default_factory=dict)
    redactions: list[str] = Field(default_factory=list, max_length=100)


def references(kind: str, value: dict):
    refs = []
    if kind == "skill":
        refs.append(("prompt", value["prompt_ref"]))
    elif kind == "source" and value.get("connection"):
        refs.append(("connection", value["connection"]))
    elif kind == "pipeline":
        if value.get("connection"):
            refs.append(("connection", value["connection"]))
        refs.extend(("connection", ref) for ref in value.get("stage_models", {}).values())
        refs.extend(("prompt", ref) for ref in value.get("prompt_refs", {}).values())
        refs.extend(("skill", ref) for ref in value.get("skill_refs", []))
        refs.extend(("source", ref) for ref in value.get("source_refs", []))
    return refs


def validate_bundle(value: dict):
    if type(value.get("schema_version")) is not int:
        raise ValueError("settings_schema_version_must_be_integer")
    bundle = SettingsBundle.model_validate(value)
    if len({project.alias for project in bundle.projects}) != len(bundle.projects):
        raise ValueError("duplicate_project_alias")
    for project in bundle.projects:
        ids: dict[tuple[str, str], list[int]] = {}
        versions: set[tuple[str, str, int]] = set()
        for entry in project.configuration:
            key = (entry.kind, entry.id, entry.revision)
            if key in versions:
                raise ValueError("duplicate_configuration_version")
            versions.add(key)
            ids.setdefault((entry.kind, entry.id), []).append(entry.revision)
            entry.content = normalize(entry.kind, entry.content)
        for revisions in ids.values():
            if sorted(revisions) != list(range(1, len(revisions) + 1)):
                raise ValueError("configuration_revision_gap")
        for entry in project.configuration:
            for kind, ref in references(entry.kind, entry.content):
                if (kind, ref["id"], ref["revision"]) not in versions:
                    raise ValueError("settings_reference_outside_exported_project")
        datasets = set()
        for catalogue in project.catalogues:
            dataset_key = (catalogue.catalog_id, catalogue.version)
            if dataset_key in datasets:
                raise ValueError("duplicate_dataset_version")
            datasets.add(dataset_key)
            if catalogue.content is not None:
                if (
                    digest(catalogue.content) != catalogue.sha256
                    or catalogue.content.get("catalog_id") != catalogue.catalog_id
                    or catalogue.content.get("catalog_version") != catalogue.version
                ):
                    raise ValueError("dataset_hash_or_identity_mismatch")
                from .catalog import validate

                if validate(catalogue.content):
                    raise ValueError("settings_catalogue_invalid")
    return bundle.model_dump()


def _parse_bundle(raw: bytes):
    from .inputs import parse

    value = parse(raw, "json", catalog=True)
    return validate_bundle(value)


def parse_bundle(raw: bytes):
    return bounded(_parse_bundle, raw, timeout=30)


def portable(kind: str, content: dict, *, sanitized: bool, number: int):
    result = json.loads(canonical(content))
    unresolved: dict[str, object] = {}
    if kind in {"connection", "source"}:
        if result.get("secret_ref") or result.get("secret_headers"):
            unresolved["credentials"] = {
                "auth": result.get("auth"),
                "secret_reference_alias": "credential-" + str(number),
                "secret_header_names": list(result.get("secret_headers", {})),
            }
        if kind == "connection":
            result.update(auth="none", secret_ref=None, secret_headers={})
        else:
            result.update(auth="none", secret_ref=None)
    if kind == "source" and result.get("kind") == "local_folder":
        unresolved["paths"] = "Remap /sources mount and explicitly review relative paths."
    if sanitized:
        result["name"] = kind + " " + str(number)
        if kind == "connection":
            result.update(
                endpoint="https://unresolved.invalid",
                local_addresses=[],
                headers={},
                allowed_tools={},
            )
            unresolved["endpoint_and_tools"] = (
                "Redacted. Choose destination and read-only tools before use."
            )
        elif kind == "source":
            result.update(
                uri="https://unresolved.invalid" if result["kind"] == "https" else ".",
                local_addresses=[],
                pages=[],
                paths=[],
                exclusions=list(DEFAULT_EXCLUSIONS),
                authority="user_selected_source",
                arguments={},
            )
            if result["kind"] == "mcp":
                result["tool"] = "unresolved_tool"
            unresolved["source_scope"] = "Redacted. Re-select mounted root/pages/tool arguments."
        elif kind == "prompt":
            result.update(
                template=defaults()[result["stage"]]["template"]
                if result["stage"] != "skill"
                else "Return observations as annotations only.",
                status="draft",
                changelog="Sanitized export reset template; review required.",
            )
            if result["stage"] == "skill":
                result["output_schema"] = ANNOTATIONS
            unresolved["template"] = "Reset to code-owned default; original free text excluded."
        elif kind == "skill":
            result.update(
                description="Sanitized declarative skill; review before use.",
                input_schema={"type": "object"},
            )
            result["output_schema"] = (
                CONTRACTS[result["stage"]].model_json_schema()
                if result["hook"] == "replace"
                else ANNOTATIONS
            )
            unresolved["skill_text_and_schemas"] = "Reset; re-review intended meaning."
    return normalize(kind, result), unresolved


def export_settings(
    store: Store, project_ids: list[str], *, include_catalogues=False, sanitized=False
):
    if not project_ids or len(project_ids) != len(set(project_ids)):
        raise ValueError("choose_distinct_projects")
    if sanitized and include_catalogues:
        raise ValueError("sanitized_export_uses_dataset_references_only")
    projects = []
    selected_bytes = 0
    for index, pid in enumerate(project_ids):
        row = store.connection.execute(
            "SELECT name,description FROM projects WHERE id=?", (pid,)
        ).fetchone()
        if row is None:
            raise ValueError("export_project_not_found")
        selected_bytes += store.connection.execute(
            "SELECT coalesce(sum(length(content)),0) FROM registry_versions WHERE project_id=?",
            (pid,),
        ).fetchone()[0]
        if include_catalogues:
            selected_bytes += store.connection.execute(
                "SELECT coalesce(sum(length(content)),0) FROM catalog_versions WHERE project_id=?",
                (pid,),
            ).fetchone()[0]
        if selected_bytes > 30 * 1024**2:
            raise ValueError("settings_bundle_size_limit_choose_fewer_projects")
        configs = []
        for number, item in enumerate(
            store.connection.execute(
                "SELECT kind,id,revision,content FROM registry_versions WHERE project_id=? ORDER BY kind,id,revision",
                (pid,),
            ).fetchall()
        ):
            kind, object_id, revision, data = item
            value, unresolved = portable(
                kind, json.loads(data), sanitized=sanitized, number=number + 1
            )
            configs.append(
                {
                    "kind": kind,
                    "id": object_id,
                    "revision": revision,
                    "content": value,
                    "unresolved": unresolved,
                }
            )
        catalogues: list[dict] = []
        for catalog_id, version, data, checksum in store.connection.execute(
            f"SELECT catalog_id,version,{'content' if include_catalogues else 'NULL'},sha256 FROM catalog_versions WHERE project_id=? ORDER BY rowid",
            (pid,),
        ):
            catalogues.append(
                {
                    "catalog_id": "unresolved.catalogue." + str(len(catalogues) + 1)
                    if sanitized
                    else catalog_id,
                    "version": version,
                    "schema_version": "4.0",
                    "sha256": checksum,
                    "content": json.loads(data) if include_catalogues else None,
                }
            )
        projects.append(
            {
                "alias": "project-" + str(index + 1),
                "name": "Project " + str(index + 1) if sanitized else row[0],
                "description": "" if sanitized else row[1],
                "configuration": configs,
                "catalogues": catalogues,
            }
        )
    preferences = dict(
        store.connection.execute(
            "SELECT key,value FROM instance_settings WHERE key IN ('allow_external_egress','diagnostic_retention_days')"
        )
    )
    value = {
        "schema_version": 1,
        "app_version": "1.0.0",
        "sanitized": sanitized,
        "projects": projects,
        "operator_preferences": preferences,
        "redactions": [
            "Secret values, raw documents/evidence, runs, grants and capability reports are excluded.",
            "Credential refs are unresolved aliases; keys must be configured again.",
        ]
        + (
            [
                "Addresses, paths, headers and tool mappings redacted. Prompt/skill free text reset to defaults; this is a sharing template, not an identical pipeline."
            ]
            if sanitized
            else [
                "Endpoints, relative paths, names and prompt/skill text can reveal internal information; review before sharing."
            ]
        ),
    }
    if len(canonical(value)) > 32 * 1024**2:
        raise ValueError("settings_bundle_size_limit_choose_fewer_projects")
    return validate_bundle(value)


def preview_settings(value: dict):
    return {
        "sha256": digest(value),
        "schema_version": 1,
        "projects": [
            {
                "alias": project["alias"],
                "name": project["name"],
                "configuration_versions": len(project["configuration"]),
                "catalogues": [
                    {
                        "catalog_id": c["catalog_id"],
                        "version": c["version"],
                        "embedded": c["content"] is not None,
                    }
                    for c in project["catalogues"]
                ],
                "unresolved": [
                    {
                        "kind": cfg["kind"],
                        "id": cfg["id"],
                        "revision": cfg["revision"],
                        "fields": cfg["unresolved"],
                    }
                    for cfg in project["configuration"]
                    if cfg["unresolved"]
                ],
            }
            for project in value["projects"]
        ],
        "redactions": value["redactions"],
        "import_policy": "New isolated project scopes. Connections/sources/skills and catalogue bindings disabled. No grants/network calls. Remap keys/paths and review before use.",
        "operator_preferences": value["operator_preferences"],
        "operator_preferences_auto_applied": False,
    }


def _prepare_import_configuration(projects):
    """Pure normalization in one bounded child, before any database mutation."""
    prepared = []
    for project in projects:
        remap = {
            (entry["kind"], entry["id"]): str(uuid.uuid4()) for entry in project["configuration"]
        }
        entries: list[dict] = []
        unresolved: list[dict] = []
        for entry in sorted(
            project["configuration"], key=lambda x: (x["kind"], x["id"], x["revision"])
        ):
            content, credential_notes = portable(
                entry["kind"],
                json.loads(canonical(entry["content"])),
                sanitized=False,
                number=len(unresolved) + 1,
            )
            for kind, ref in references(entry["kind"], content):
                ref["id"] = remap[(kind, ref["id"])]
            if entry["kind"] in {"connection", "source", "skill"}:
                content["enabled"] = False
            content = normalize(entry["kind"], content)
            object_id = remap[(entry["kind"], entry["id"])]
            entries.append(
                {
                    "kind": entry["kind"],
                    "id": object_id,
                    "revision": entry["revision"],
                    "content": content,
                }
            )
            if entry["unresolved"] or credential_notes:
                unresolved.append(
                    {
                        "kind": entry["kind"],
                        "id": object_id,
                        "revision": entry["revision"],
                        "fields": entry["unresolved"] | credential_notes,
                    }
                )
        prepared.append({"entries": entries, "unresolved": unresolved})
    return prepared


def import_settings(store: Store, value: dict):
    checksum = digest(value)
    previous = store.connection.execute(
        "SELECT receipt FROM settings_imports WHERE bundle_sha256=?", (checksum,)
    ).fetchone()
    if previous:
        return json.loads(previous[0]) | {"replayed": True}
    prepared = bounded(_prepare_import_configuration, value["projects"], timeout=30)
    with store.connection:
        store.connection.execute("BEGIN IMMEDIATE")
        previous = store.connection.execute(
            "SELECT receipt FROM settings_imports WHERE bundle_sha256=?", (checksum,)
        ).fetchone()
        if previous:
            return json.loads(previous[0]) | {"replayed": True}
        receipt: dict = {
            "bundle_sha256": checksum,
            "projects": [],
            "operator_preferences_auto_applied": False,
        }
        for project, configuration in zip(value["projects"], prepared, strict=True):
            pid = str(uuid.uuid4())
            store.connection.execute(
                "INSERT INTO projects VALUES(?,?,?)", (pid, project["name"], project["description"])
            )
            for entry in configuration["entries"]:
                content, object_id = entry["content"], entry["id"]
                store.connection.execute(
                    "INSERT INTO registry_versions(project_id,kind,id,revision,content,sha256) VALUES(?,?,?,?,?,?)",
                    (
                        pid,
                        entry["kind"],
                        object_id,
                        entry["revision"],
                        canonical(content),
                        digest(content),
                    ),
                )
                store.connection.execute(
                    "INSERT INTO registry_heads VALUES(?,?,?,?) ON CONFLICT(project_id,kind,id) DO UPDATE SET revision=max(revision,excluded.revision)",
                    (pid, entry["kind"], object_id, entry["revision"]),
                )
            missing_catalogues = []
            for catalogue in project["catalogues"]:
                content = catalogue["content"]
                if content is None:
                    missing_catalogues.append(
                        {key: val for key, val in catalogue.items() if key != "content"}
                    )
                    continue
                cid = store.bind_catalog(pid, content, activate=False)
                store.set_catalog_binding(pid, cid, enabled=False)
            receipt["projects"].append(
                {
                    "original_alias": project["alias"],
                    "project_id": pid,
                    "unresolved": configuration["unresolved"],
                    "missing_catalogue_references": missing_catalogues,
                    "permissions_granted": False,
                    "documents_imported": 0,
                }
            )
            store.audit(pid, "settings_imported_new_scope_disabled", checksum)
        store.connection.execute(
            "INSERT INTO settings_imports(bundle_sha256,receipt) VALUES(?,?)",
            (checksum, canonical(receipt).decode()),
        )
    return receipt | {"replayed": False}
