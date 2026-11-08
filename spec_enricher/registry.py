"""Immutable project configuration versions and grants pinned to exact content."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from contextlib import nullcontext

from .catalog import canonical
from .connections import ConnectionProfile
from .network import external_egress_denied
from .snapshots import sha256
from .stage_contracts import Contract
from .storage import Store


class RevisionConflict(ValueError):
    pass


def normalize(kind: str, content: dict):
    if kind == "connection":
        content = ConnectionProfile.model_validate(content).model_dump()
    elif kind in {"prompt", "skill", "pipeline"}:
        from .pipeline_config import PipelineDefinition, PromptDefinition, SkillDefinition

        validators: dict[str, type[Contract]] = {
            "prompt": PromptDefinition,
            "skill": SkillDefinition,
            "pipeline": PipelineDefinition,
        }
        content = validators[kind].model_validate(content).model_dump()
    elif kind == "source":
        from .sources import SourceDefinition

        content = SourceDefinition.model_validate(content).model_dump()
    else:
        raise ValueError("unsupported configuration kind")
    if len(canonical(content)) > 512 * 1024:
        raise ValueError("configuration limit")
    return content


class Registry:
    def __init__(self, store: Store):
        self.store = store

    def save(
        self,
        project_id: str,
        kind: str,
        content: dict,
        *,
        object_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        content = normalize(kind, content)
        data = canonical(content)
        if len(data) > 512 * 1024:
            raise ValueError("configuration limit")
        new_id = object_id or str(uuid.uuid4())
        con = self.store.connection
        outer = con.in_transaction
        with con if not outer else nullcontext():
            if not outer:
                con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT revision FROM registry_heads WHERE project_id=? AND kind=? AND id=?",
                (project_id, kind, new_id),
            ).fetchone()
            if object_id is not None and (row is None or row[0] != expected_revision):
                raise RevisionConflict("revision conflict")
            revision = 1 if row is None else row[0] + 1
            checksum = sha256(data)
            con.execute(
                "INSERT INTO registry_versions(project_id,kind,id,revision,content,sha256) VALUES(?,?,?,?,?,?)",
                (project_id, kind, new_id, revision, data, checksum),
            )
            con.execute(
                "INSERT INTO registry_heads VALUES(?,?,?,?) ON CONFLICT(project_id,kind,id) DO UPDATE SET revision=excluded.revision",
                (project_id, kind, new_id, revision),
            )
            if kind in {"connection", "source"} and content.get("enabled") is False:
                con.execute(
                    "DELETE FROM egress_grants WHERE project_id=? AND kind=? AND id=?",
                    (project_id, kind, new_id),
                )
        self.store.audit(project_id, kind + "_version_created", new_id)
        return {"id": new_id, "revision": revision, "sha256": checksum, "content": content}

    def get(self, project_id: str, kind: str, object_id: str, revision: int | None = None):
        if revision is None:
            row = self.store.connection.execute(
                """SELECT v.revision,v.content,v.sha256 FROM registry_versions v JOIN registry_heads h
                ON v.project_id=h.project_id AND v.kind=h.kind AND v.id=h.id AND v.revision=h.revision
                WHERE v.project_id=? AND v.kind=? AND v.id=?""",
                (project_id, kind, object_id),
            ).fetchone()
        else:
            row = self.store.connection.execute(
                "SELECT revision,content,sha256 FROM registry_versions WHERE project_id=? AND kind=? AND id=? AND revision=?",
                (project_id, kind, object_id, revision),
            ).fetchone()
        if row is None:
            return None
        report = self.store.connection.execute(
            "SELECT report,created_at FROM capability_reports WHERE project_id=? AND kind=? AND id=? AND revision=?",
            (project_id, kind, object_id, row[0]),
        ).fetchone()
        grant = self.store.connection.execute(
            "SELECT scopes,config_sha256 FROM egress_grants WHERE project_id=? AND kind=? AND id=? AND revision=?",
            (project_id, kind, object_id, row[0]),
        ).fetchone()
        return {
            "id": object_id,
            "revision": row[0],
            "sha256": row[2],
            "content": json.loads(row[1]),
            "capability_report": None
            if report is None
            else {"result": json.loads(report[0]), "tested_at": report[1]},
            "granted_scopes": [] if grant is None or grant[1] != row[2] else json.loads(grant[0]),
        }

    def entries(self, project_id: str, kind: str):
        ids = self.store.connection.execute(
            "SELECT id FROM registry_heads WHERE project_id=? AND kind=? ORDER BY rowid",
            (project_id, kind),
        ).fetchall()
        return [self.get(project_id, kind, row[0]) for row in ids]

    def report(
        self, project_id: str, object_id: str, revision: int, report: dict, *, kind="connection"
    ):
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO capability_reports(project_id,kind,id,revision,report) VALUES(?,?,?,?,?) ON CONFLICT(project_id,kind,id,revision) DO UPDATE SET report=excluded.report,created_at=CURRENT_TIMESTAMP",
                (
                    project_id,
                    kind,
                    object_id,
                    revision,
                    json.dumps(report, ensure_ascii=False),
                ),
            )
        self.store.audit(project_id, "connection_test", object_id)

    def grant(
        self,
        project_id: str,
        object_id: str,
        revision: int,
        expected_hash: str,
        scopes: Sequence[str],
    ):
        record = self.get(project_id, "connection", object_id, revision)
        if record is None or expected_hash != record["sha256"]:
            raise RevisionConflict("configuration changed")
        profile = ConnectionProfile.model_validate(record["content"])
        if set(scopes) - set(profile.data_scopes) or len(scopes) != len(set(scopes)):
            raise ValueError("scope not configured")
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO egress_grants(project_id,kind,id,revision,config_sha256,scopes) VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,kind,id,revision) DO UPDATE SET config_sha256=excluded.config_sha256,scopes=excluded.scopes,created_at=CURRENT_TIMESTAMP",
                (
                    project_id,
                    "connection",
                    object_id,
                    revision,
                    expected_hash,
                    json.dumps(list(scopes)),
                ),
            )
        self.store.audit(project_id, "egress_granted", object_id)

    def require_egress(
        self,
        project_id: str,
        object_id: str,
        revision: int,
        scopes: set[str],
        *,
        synthetic: bool = False,
    ):
        record = self.get(project_id, "connection", object_id, revision)
        if record is None:
            raise ValueError("connection not found in project")
        profile = ConnectionProfile.model_validate(record["content"])
        if not profile.local_addresses and not self.external_egress_allowed():
            raise ValueError("external egress disabled by operator policy")
        if not synthetic:
            if not profile.enabled or scopes - set(record["granted_scopes"]):
                raise ValueError("destination/data scope not granted")
            report = record["capability_report"]
            if report is None or report["result"]["status"] not in {"passed", "degraded"}:
                raise ValueError("capability test required")
        return record, profile

    def external_egress_allowed(self) -> bool:
        allowed = self.store.connection.execute(
            "SELECT value FROM instance_settings WHERE key='allow_external_egress'"
        ).fetchone()
        return not external_egress_denied() and allowed is not None and allowed[0] == "true"
