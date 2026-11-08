"""Partial drafts are separate from schema4.0 active policies and immutable runs."""

from __future__ import annotations

import difflib
import json
import re
import uuid
from datetime import UTC, datetime
from functools import lru_cache

from pydantic import Field

from .builtin import load_verified
from .catalog import canonical, digest, semantic_hash
from .i18n import translate
from .parser_worker import parse_bounded
from .registry import RevisionConflict
from .stage_contracts import Contract
from .storage import Store


class DraftInput(Contract):
    expected_revision: int | None = Field(default=None, ge=1)
    base_requirement_ref: dict | None = None
    fields: dict = Field(default_factory=dict)
    unresolved_fields: list[str] = Field(default_factory=list, max_length=100)
    questions: list[str] = Field(default_factory=list, max_length=100)


@lru_cache(maxsize=1)
def upstream_ids():
    return {record["id"] for record in load_verified()[1]["requirements"]}


def new_fields():
    return {
        "id": "USER-" + uuid.uuid4().hex.upper(),
        "version": "1.0.0",
        "title": "",
        "statement": "",
        "domain": "",
        "subdomain": "",
        "tags": [],
        "applicability": {"predicates": [], "expression": {}, "unknown_when": ""},
        "clauses": [],
        "clause_logic": {},
        "clause_normativity": {},
        "verification": {
            "specification": {
                "inputs": [],
                "steps": [],
                "outcome_conditions": {
                    "satisfied_by_spec": "",
                    "contradicted_by_spec": "",
                    "not_addressed": "",
                    "unknown": "",
                },
            },
            "source_level": {
                "inputs": [],
                "steps": [],
                "pass_when": "",
                "fail_when": "",
                "unknown_when": "",
            },
        },
        "origin": {"author": "", "external_provenance": None, "license": None},
        "source_kind": "mandatory",
        "level": "specification",
        "severity": "unrated",
    }


def source_record(fields: dict, *, created_at: str):
    """Materialize only code-owned mechanics; human meaning is never filled by NLP."""
    record = json.loads(canonical(fields))
    rid = record.get("id", "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._:-]{2,127}", rid) or rid in upstream_ids():
        raise ValueError("protected_or_invalid_user_requirement_id")
    record.setdefault("record_key", "user." + rid)
    record.setdefault("legacy_ids", [])
    record.setdefault("status", "active")
    record.setdefault("classification_status", "normative")
    record.setdefault("source_kind", "mandatory")
    record.setdefault("level", "specification")
    record.setdefault("severity", "unrated")
    record.setdefault(
        "source_context",
        {
            "heading_path": [record.get("title") or translate("Собственная политика")],
            "profile": translate("Собственная политика пользователя; не требование Hub"),
            "framing_ref_ids": ["user-source"],
            "qualifier_ref_ids": ["user-source"],
        },
    )
    origin = record.setdefault("origin", {})
    # An edit of an upstream record must be copied into a new user-owned draft.
    if origin.get("kind") == "upstream_source":
        raise ValueError("upstream_requires_user_owned_override")
    authored = record.get("statement", "")
    parts = [authored] + [item.get("obligation", "") for item in record.get("clauses", [])]
    embedded = "\n".join(part for part in parts if part)
    origin.setdefault("kind", "user_authored")
    origin.setdefault("created_at", created_at)
    origin.setdefault("classification", "user")
    origin.setdefault("embedded_source", embedded)
    origin.setdefault("external_provenance", None)
    origin.setdefault("license", None)
    if embedded:
        record.setdefault(
            "source_refs",
            [
                {
                    "id": "user-source",
                    "line_start": 1,
                    "line_end": len(embedded.splitlines()),
                    "quote": embedded,
                }
            ],
        )
    for item in record.get("clauses", []) + record.get("applicability", {}).get("predicates", []):
        item.setdefault("source_ref_ids", ["user-source"])
    for value in record.get("clause_normativity", {}).values():
        value.setdefault("source_ref_ids", ["user-source"])
    verification = record.setdefault("verification", {})
    verification.update(
        execution_policy="plan_only_no_automatic_execution",
        spec_only_implementation_state="not_verified",
    )
    record["review"] = {
        "status": "pending_independent_review",
        "content_sha256": semantic_hash(record),
        "ledger_refs": [],
        "scope": "Candidate awaiting local authoring approval; no independent semantic review.",
    }
    record["normativity_review"] = {
        "status": "pending_peer_review",
        "annotation_sha256": digest(record.get("clause_normativity", {})),
        "report_refs": [],
    }
    return record


def catalog_value(records: list[dict], catalog_id: str, version: str, title: str):
    return {
        "schema_version": "4.0",
        "catalog_id": catalog_id,
        "catalog_version": version,
        "title": title,
        "ready_for_use": True,
        "release_status": "user_approved",
        "scope": "user_catalogue",
        "domain_filter": [],
        "origin": {"kind": "user_authored", "classification": "user", "external_provenance": None},
        "sources": [],
        "requirements": records,
        "limitations": [
            "User-authored policy; no independent semantic review.",
            "Verification plans are data only; implementation is not_verified.",
        ],
    }


class Authoring:
    def __init__(self, store: Store):
        self.store = store

    def save(self, scope: str, owner_id: str, body: DraftInput, draft_id: str | None = None):
        if len(canonical(body.model_dump())) > 512 * 1024:
            raise ValueError("draft_size_limit")
        draft_id = draft_id or str(uuid.uuid4())
        with self.store.connection:
            self.store.connection.execute("BEGIN IMMEDIATE")
            old = self.get(scope, owner_id, draft_id)
            if (
                old is None
                and body.expected_revision is not None
                or old is not None
                and old["revision"] != body.expected_revision
            ):
                raise RevisionConflict("draft_revision_conflict")
            if old and old["fields"].get("id") and old["fields"].get("id") != body.fields.get("id"):
                raise ValueError("stable_id_cannot_change_after_first_save")
            revision = 1 if old is None else old["revision"] + 1
            created = old["created_at"] if old else datetime.now(UTC).isoformat()
            value = {
                "draft_id": draft_id,
                "revision": revision,
                "scope": scope,
                "project_id": owner_id if scope == "project" else None,
                "user_catalogue_id": owner_id if scope == "user_catalogue" else None,
                "base_requirement_ref": body.base_requirement_ref,
                "fields": body.fields,
                "unresolved_fields": body.unresolved_fields,
                "questions": body.questions,
                "created_at": created,
            }
            self.store.connection.execute(
                "INSERT INTO requirement_drafts(id,revision,scope,project_id,user_catalogue_id,content,sha256) VALUES(?,?,?,?,?,?,?)",
                (
                    draft_id,
                    revision,
                    scope,
                    value["project_id"],
                    value["user_catalogue_id"],
                    canonical(value),
                    digest(value),
                ),
            )
        return value

    def get(self, scope: str, owner_id: str, draft_id: str):
        column = "project_id" if scope == "project" else "user_catalogue_id"
        row = self.store.connection.execute(
            f"SELECT content FROM requirement_drafts WHERE scope=? AND {column}=? AND id=? ORDER BY revision DESC LIMIT 1",
            (scope, owner_id, draft_id),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def preview(self, draft: dict):
        try:
            record = source_record(draft["fields"], created_at=draft["created_at"])
            candidate = catalog_value(
                [record],
                "user.record." + record["id"],
                record.get("version", ""),
                record.get("title", ""),
            )
            if draft["scope"] == "user_catalogue":
                owner = draft["user_catalogue_id"]
                current = self.store.connection.execute(
                    "SELECT content FROM user_catalogue_versions WHERE user_catalogue_id=? ORDER BY revision DESC LIMIT 1",
                    (owner,),
                ).fetchone()
                records = (
                    {}
                    if current is None
                    else {r["id"]: r for r in json.loads(current[0])["requirements"]}
                )
                records[record["id"]] = record
                revision = self.store.connection.execute(
                    "SELECT next_revision FROM user_catalogues WHERE id=?", (owner,)
                ).fetchone()[0]
                candidate = catalog_value(
                    list(records.values()),
                    "user.catalogue." + owner,
                    str(revision),
                    translate("Пользовательский каталог"),
                )
            if draft["scope"] == "project":
                by_id = {record["id"]: record}
                pending = list(record.get("dependencies", []))
                while pending:
                    dependency = pending.pop()
                    if dependency["requirement_id"] in by_id:
                        continue
                    rows = self.store.connection.execute(
                        "SELECT content FROM requirement_index WHERE project_id=? AND requirement_id=? AND version=?",
                        (draft["project_id"], dependency["requirement_id"], dependency["version"]),
                    ).fetchall()
                    target = next(
                        (
                            json.loads(row[0])
                            for row in rows
                            if semantic_hash(json.loads(row[0]))
                            == dependency["target_semantic_sha256"]
                        ),
                        None,
                    )
                    if target is not None:
                        by_id[target["id"]] = target
                        pending.extend(target.get("dependencies", []))
                candidate["requirements"] = list(by_id.values())
            candidate.update(ready_for_use=False, release_status="draft")
            _, issues = parse_bounded(canonical(candidate), "json", catalog=True)
        except ValueError as exc:
            return {
                "candidate": None,
                "issues": [{"path": "/id", "code": str(exc)}],
                "conflicts": [],
                "questions": draft["questions"],
                "sha256": None,
                "scope": draft["scope"],
            }
        conflicts = []
        if draft["scope"] == "project":
            rows = self.store.connection.execute(
                "SELECT version,content FROM requirement_index WHERE project_id=? AND requirement_id=? ORDER BY rowid",
                (draft["project_id"], record["id"]),
            ).fetchall()
        else:
            rows = []
            versions = self.store.connection.execute(
                "SELECT content FROM user_catalogue_versions WHERE user_catalogue_id=? ORDER BY revision DESC LIMIT 1",
                (draft["user_catalogue_id"],),
            ).fetchone()
            if versions:
                rows = [
                    (r["version"], canonical(r))
                    for r in json.loads(versions[0])["requirements"]
                    if r["id"] == record["id"]
                ]
        for version, content in rows:
            original = json.loads(content)
            if semantic_hash(original) != semantic_hash(record):
                conflicts.append(
                    {
                        "id": record["id"],
                        "version": version,
                        "same_version": version == record.get("version"),
                        "original": original,
                        "candidate": record,
                        "diff": "".join(
                            difflib.unified_diff(
                                json.dumps(original, ensure_ascii=False, indent=2).splitlines(True),
                                json.dumps(record, ensure_ascii=False, indent=2).splitlines(True),
                                fromfile="existing",
                                tofile="draft",
                            )
                        ),
                    }
                )
        return {
            "candidate": candidate,
            "issues": issues,
            "conflicts": conflicts,
            "questions": draft["questions"],
            "unresolved_fields": draft["unresolved_fields"],
            "sha256": digest(candidate),
            "scope": draft["scope"],
            "semantic_review": "not_performed",
            "implementation_verification": "not_verified",
            "transfer_notice": "Only authored policy fields; no project documents, evidence, credentials or grants are copied.",
        }

    def commit(self, draft: dict, expected_hash: str, *, activate: bool, replace_bindings: bool):
        preview = self.preview(draft)
        if (
            preview["sha256"] != expected_hash
            or preview["issues"]
            or draft["unresolved_fields"]
            or any(c["same_version"] for c in preview["conflicts"])
        ):
            raise RevisionConflict("draft_not_ready_or_conflicting_version")
        value = preview["candidate"]
        value.update(ready_for_use=True, release_status="user_approved")
        approved_record = next(
            record for record in value["requirements"] if record["id"] == draft["fields"]["id"]
        )
        approved_record["review"]["status"] = "user_approved"
        approved_record["normativity_review"]["status"] = "user_approved"
        if draft["scope"] == "project":
            pid = draft["project_id"]
            active = self.store.connection.execute(
                "SELECT c.id FROM catalog_versions c JOIN catalog_bindings b ON c.project_id=b.project_id AND c.id=b.catalog_version_id WHERE c.project_id=? AND c.catalog_id=? AND b.enabled=1 AND c.version!=?",
                (pid, value["catalog_id"], value["catalog_version"]),
            ).fetchall()
            if activate and active and not replace_bindings:
                raise RevisionConflict("explicit_binding_update_required")
            cid = self.store.bind_catalog(pid, value, activate=activate)
            if activate and active:
                for row in active:
                    self.store.set_catalog_binding(pid, row[0], enabled=False)
            return {"catalog_id": cid, "scope": "project", "activated": activate}
        owner = draft["user_catalogue_id"]
        row = self.store.connection.execute(
            "SELECT revision,content FROM user_catalogue_versions WHERE user_catalogue_id=? ORDER BY revision DESC LIMIT 1",
            (owner,),
        ).fetchone()
        records = {} if row is None else {r["id"]: r for r in json.loads(row[1])["requirements"]}
        record = approved_record
        if record["id"] in records and semantic_hash(records[record["id"]]) == semantic_hash(
            record
        ):
            return {
                "user_catalogue_id": owner,
                "revision": row[0],
                "scope": "user_catalogue",
                "activated": False,
            }
        records[record["id"]] = record
        revision = self.store.connection.execute(
            "SELECT next_revision FROM user_catalogues WHERE id=?", (owner,)
        ).fetchone()[0]
        _, issues = parse_bounded(canonical(value), "json", catalog=True)
        if issues:
            raise ValueError("user_catalogue_dependency_or_schema_conflict")
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO user_catalogue_versions(user_catalogue_id,revision,content,sha256) VALUES(?,?,?,?)",
                (owner, revision, canonical(value), digest(value)),
            )
            self.store.connection.execute(
                "UPDATE user_catalogues SET next_revision=? WHERE id=?", (revision + 1, owner)
            )
        return {
            "user_catalogue_id": owner,
            "revision": revision,
            "scope": "user_catalogue",
            "activated": False,
        }
