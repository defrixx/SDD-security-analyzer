"""Freeze selected project inputs and exact configuration before a run."""

from __future__ import annotations

import json
from pathlib import Path

from .catalog import canonical, digest, semantic_hash
from .connections import ConnectionProfile
from .effective_state import blocks, target_quote
from .pipeline_config import freeze_configuration
from .prompts import (
    DOCUMENTARY_ASSESSMENT_RECIPE,
    POLICY,
    architectural_review_prompt,
    candidate_reassessment_prompt,
)
from .registry import Registry
from .run_contracts import ModelStage, RunInput
from .snapshots import sha256
from .storage import Store

MODEL_STAGES: tuple[ModelStage, ...] = ("P2", "P3", "P5", "P7", "P8")


def current_algorithm_hashes():
    root = Path(__file__).parent
    files = {path.name: path for path in sorted(root.glob("*.py"))}
    files["requirements.lock"] = root.parent / "requirements.lock"
    files["catalog/requirements.schema.json"] = root.parent / "catalog/requirements.schema.json"
    return {name: sha256(path.read_bytes()) for name, path in files.items()}


def prepare(store: Store, project_id: str, request: RunInput):
    from .lifecycle import require_no_pending_purge

    require_no_pending_purge(store, project_id)
    documents = []
    source_snapshots = []
    source_config_refs = {}
    review = request.mode == "review_documents"
    model_stages = ("P3", "P5") if review else MODEL_STAGES
    all_ids = request.review_document_ids if review else [
        request.delta_document_id,
        *request.baseline_document_ids,
        *request.context_document_ids,
    ]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("duplicate_input_document")
    size = 0
    for document_id in all_ids:
        row = store.connection.execute(
            "SELECT name,role,format,content,sha256 FROM documents WHERE project_id=? AND id=?",
            (project_id, document_id),
        ).fetchone()
        if row is None or sha256(row[3]) != row[4]:
            raise ValueError("missing_or_changed_project_snapshot")
        if review and not row[3].strip():
            raise ValueError("review_document_empty")
        role = "review" if review else (
            "delta"
            if document_id == request.delta_document_id
            else "master_spec"
            if document_id in request.baseline_document_ids
            else "context"
        )
        if not review and (role == "delta") != (row[1] == "delta"):
            raise ValueError("document_role_conflict")
        if role != "delta":
            size += len(row[3])
        documents.append(
            {
                "id": document_id,
                "name": row[0],
                "role": role,
                "format": row[2],
                "sha256": row[4],
                "byte_size": len(row[3]),
                "authority": "user_selected_snapshot",
                "freshness": "pinned_no_auto_refresh",
            }
        )
        snapshot = store.connection.execute(
            "SELECT id,source_id,source_revision,uri,raw_sha256,metadata FROM source_snapshots WHERE project_id=? AND document_id=? ORDER BY rowid DESC LIMIT 1",
            (project_id, document_id),
        ).fetchone()
        if snapshot:
            pinned = {
                "id": snapshot[0],
                "source_id": snapshot[1],
                "source_revision": snapshot[2],
                "uri": snapshot[3],
                "raw_sha256": snapshot[4],
                "metadata": json.loads(snapshot[5]),
            }
            documents[-1]["source_snapshot"] = pinned
            source_snapshots.append(pinned)
            source_config_refs[(snapshot[1], snapshot[2])] = {
                "kind": "source",
                "id": snapshot[1],
                "revision": snapshot[2],
            }
    if size > 10 * 1024 * 1024:
        raise ValueError("selected_corpus_limit")
    if request.operation_overrides:
        delta_blocks = {
            block.id: block
            for block in blocks(
                request.delta_document_id, store.document(project_id, request.delta_document_id)[2]
            )
        }
        baseline_blocks = {}
        for doc_id in request.baseline_document_ids:
            baseline_blocks.update(
                {block.id: block for block in blocks(doc_id, store.document(project_id, doc_id)[2])}
            )
        seen_operations, seen_delta = set(), set()
        for op in request.operation_overrides:
            if (
                op.id in seen_operations
                or op.delta_block_id in seen_delta
                or op.delta_block_id not in delta_blocks
            ):
                raise ValueError("duplicate_or_out_of_scope_operation_override")
            seen_operations.add(op.id)
            seen_delta.add(op.delta_block_id)
            if op.kind in {"replace", "delete"}:
                target_block = (
                    baseline_blocks.get(op.target_block_id)
                    if op.target_block_id is not None
                    else None
                )
                if target_block is None or op.target_hash != target_block.hash:
                    raise ValueError("operation_override_target_hash_or_scope")
                target_quote(delta_blocks[op.delta_block_id], target_block, op.target_quote)
            elif op.target_block_id is not None or op.target_hash is not None:
                raise ValueError("unexpected_operation_override_target")
            if op.kind == "unresolved" and not op.question:
                raise ValueError("unresolved_operation_requires_question")
    catalogs = []
    records: dict[str, dict] = {}
    ownership: dict[str, str] = {}
    if len(request.catalog_ids) != len(set(request.catalog_ids)):
        raise ValueError("duplicate_catalog_binding")
    for catalog_id in request.catalog_ids:
        binding = store.connection.execute(
            "SELECT enabled FROM catalog_bindings WHERE project_id=? AND catalog_version_id=?",
            (project_id, catalog_id),
        ).fetchone()
        value = store.catalog(project_id, catalog_id)
        if binding is None or not binding[0] or value is None:
            raise ValueError("required_catalog_binding_disabled")
        catalogs.append(
            {
                "id": catalog_id,
                "catalog_id": value["catalog_id"],
                "version": value["catalog_version"],
                "schema_version": value["schema_version"],
                "sha256": sha256(canonical(value)),
                "limitations": value["limitations"],
            }
        )
        receipt = store.connection.execute(
            "SELECT content,sha256 FROM catalog_updates WHERE project_id=? AND catalog_version_id=?",
            (project_id, catalog_id),
        ).fetchone()
        if receipt:
            catalogs[-1]["adoption_receipt"] = {
                "content": json.loads(receipt[0]),
                "sha256": receipt[1],
            }
        snapshot = store.connection.execute(
            "SELECT s.id,s.source_id,s.source_revision,s.uri,s.raw_sha256,s.metadata FROM source_snapshots s JOIN catalog_imports i ON s.project_id=i.project_id AND s.catalog_import_id=i.id WHERE s.project_id=? AND i.committed_id=? ORDER BY s.rowid DESC LIMIT 1",
            (project_id, catalog_id),
        ).fetchone()
        if snapshot:
            pinned = {
                "id": snapshot[0],
                "source_id": snapshot[1],
                "source_revision": snapshot[2],
                "uri": snapshot[3],
                "raw_sha256": snapshot[4],
                "metadata": json.loads(snapshot[5]),
            }
            catalogs[-1]["source_snapshot"] = pinned
            source_snapshots.append(pinned)
            source_config_refs[(snapshot[1], snapshot[2])] = {
                "kind": "source",
                "id": snapshot[1],
                "revision": snapshot[2],
            }
        for record in value["requirements"]:
            if record["status"] != "active":
                continue
            if record["id"] in records and digest(records[record["id"]]) != digest(record):
                raise ValueError("conflicting_active_requirement_versions")
            records[record["id"]] = record
            ownership.setdefault(record["id"], catalog_id)
    if len(records) > 5000 or len(canonical(list(records.values()))) > 32 * 1024 * 1024:
        raise ValueError("effective_catalog_limit")
    if review and not records:
        raise ValueError("review_requires_active_requirements")
    for record in records.values():
        for dep in record.get("dependencies", []):
            target = records.get(dep["requirement_id"])
            if (
                target is None
                or target["version"] != dep["version"]
                or semantic_hash(target) != dep["target_semantic_sha256"]
            ):
                raise ValueError("required_dependency_disabled_or_changed")
    seen = set()
    for override in request.fact_overrides:
        key = (override.requirement_id, override.side, override.predicate_id)
        record = records.get(override.requirement_id)
        if (
            key in seen
            or record is None
            or override.predicate_id not in {p["id"] for p in record["applicability"]["predicates"]}
            or override.predicate_id
            in record.get("policy_scope", {}).get("authoring_unknown_predicate_ids", [])
        ):
            raise ValueError("invalid_fact_override")
        seen.add(key)
    if len({item.requirement_id for item in request.adoption_overrides}) != len(
        request.adoption_overrides
    ) or any(item.requirement_id not in records for item in request.adoption_overrides):
        raise ValueError("invalid_adoption_override")
    registry = Registry(store)
    for ref in request.source_refs:
        record = registry.get(project_id, "source", ref.id, ref.revision)
        if record is None or not record["content"]["enabled"]:
            raise ValueError("selected_source_disabled_or_missing")
        source_config_refs[(ref.id, ref.revision)] = {"kind": "source", **ref.model_dump()}
        matched = [
            snapshot
            for snapshot in source_snapshots
            if snapshot["source_id"] == ref.id and snapshot["source_revision"] == ref.revision
        ]
        if record["content"]["required"] and (
            not matched
            or record["capability_report"] is None
            or record["capability_report"]["result"]["status"] != "passed"
        ):
            raise ValueError("required_source_not_read_or_not_selected")
    prompts, skills, configuration_refs = freeze_configuration(registry, project_id, request)
    selected_pipeline = None
    if request.pipeline_ref is not None:
        selected_pipeline = registry.get(
            project_id, "pipeline", request.pipeline_ref.id, request.pipeline_ref.revision
        )
        if selected_pipeline is None:
            raise ValueError("pipeline_not_in_project")
        actual = request.model_dump()
        for binding_name in (
            "connection",
            "stage_models",
            "prompt_refs",
            "skill_refs",
            "source_refs",
            "limits",
            "step_mode",
        ):
            wanted, given = selected_pipeline["content"][binding_name], actual[binding_name]
            if binding_name in {"skill_refs", "source_refs"}:
                wanted = sorted(wanted, key=lambda x: (x["id"], x["revision"]))
                given = sorted(given, key=lambda x: (x["id"], x["revision"]))
            if digest(wanted) != digest(given):
                raise ValueError("pipeline_binding_changed_choose_unbound_or_exact_versions")
        configuration_refs.append({"kind": "pipeline", **request.pipeline_ref.model_dump()})
    configurations = {}
    destinations = []
    refs = {}
    for stage in model_stages:
        reference = request.stage_models.get(stage, request.connection)
        record, profile = registry.require_egress(
            project_id,
            reference.id,
            reference.revision,
            (
                {"delta", "baseline_fragments", "catalog"}
                if stage != "P2"
                else {"delta", "baseline_fragments"}
            )
            | ({"skill_outputs"} if skills else set()),
        )
        if profile.kind != "llm":
            raise ValueError("model_step_requires_llm")
        configurations[stage] = {
            "id": record["id"],
            "revision": record["revision"],
            "sha256": record["sha256"],
            "content": ConnectionProfile.model_validate(record["content"]).model_dump(),
            "capability_report": record["capability_report"],
            "granted_scopes": record["granted_scopes"],
        }
        refs[("connection", record["id"], record["revision"])] = {
            "kind": "connection",
            "id": record["id"],
            "revision": record["revision"],
        }
        destinations.append(
            {
                "stage": stage,
                "endpoint": profile.endpoint,
                "data_scopes": ["delta", "baseline_fragments"]
                + ([] if stage == "P2" else ["catalog"])
                + (["skill_outputs"] if skills else []),
                "external": not bool(profile.local_addresses),
            }
        )
    for skill in skills:
        expected = {"delta", "baseline_fragments", "skill_outputs"} | (
            set() if skill["stage"] == "P2" else {"catalog"}
        )
        if expected - set(skill["data_scopes"]):
            raise ValueError("skill_data_scope_incomplete")
    algorithm_hashes = current_algorithm_hashes()
    manifest = {
        "app_version": "1.0.0",
        "pipeline_version": "review-documents.v1" if review else "1.0.0",
        "review_scope_policy": "review-scope-confirmation.v1" if review else None,
        "mode": request.mode,
        "review_contract_version": "review-documents.v1" if review else None,
        "catalog_schema": "4.0",
        "project_id": project_id,
        "documents": documents,
        "catalogs": catalogs,
        "configuration_refs": list(refs.values())
        + configuration_refs
        + list(source_config_refs.values()),
        "source_snapshots": source_snapshots,
        "sources": [
            registry.get(project_id, "source", ref["id"], ref["revision"])
            for ref in source_config_refs.values()
        ],
        "connections": configurations,
        "prompts": prompts,
        "documentary_assessment_recipe": DOCUMENTARY_ASSESSMENT_RECIPE,
        "candidate_reassessment_prompt": candidate_reassessment_prompt(prompts["P5"]),
        "architectural_review_prompt": architectural_review_prompt(prompts["P3"]),
        "skills": skills,
        "pipeline_configuration": selected_pipeline,
        "hook_order": [
            {
                "stage": stage,
                "calls": [
                    skill["id"]
                    for skill in skills
                    if skill["stage"] == stage and skill["hook"] == "before"
                ]
                + [
                    next(
                        (
                            skill["id"]
                            for skill in skills
                            if skill["stage"] == stage and skill["hook"] == "replace"
                        ),
                        "core." + stage,
                    )
                ]
                + [
                    skill["id"]
                    for skill in skills
                    if skill["stage"] == stage and skill["hook"] == "after"
                ]
                + (["core.P8.candidate_documentary_reassessment"] if stage == "P8" else [])
                + (
                    ["core.P3.architectural_fact_review_if_negative_or_conflicting"]
                    if stage == "P3" and not review
                    else []
                ),
            }
            for stage in model_stages
        ],
        "request": request.model_dump(),
        "application_policy": POLICY,
        "algorithm_hashes": algorithm_hashes,
        "methodology_hash": digest(
            {
                "policy": POLICY,
                "review_scope_policy": "review-scope-confirmation.v1" if review else None,
                "mode": request.mode,
                "review_contract_version": "review-documents.v1" if review else None,
                "prompts": prompts,
                "documentary_assessment_recipe": DOCUMENTARY_ASSESSMENT_RECIPE,
                "candidate_reassessment_prompt": candidate_reassessment_prompt(prompts["P5"]),
                "architectural_review_prompt": architectural_review_prompt(prompts["P3"]),
                "skills": skills,
                "pipeline_configuration": selected_pipeline,
                "pipeline": "1.0.0",
                "algorithms": algorithm_hashes,
            }
        ),
        "destinations": destinations,
        "limits": request.limits.model_dump(),
        "requirement_ownership": ownership,
        "project_rankings": [
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
            if row[0] in records and records[row[0]]["version"] == row[1]
        ],
        "limitations": [
            "Implementation is not verified.",
            "No automatic execution of catalog verification plans.",
            "Future payloads depend on retrieval and preceding stage outputs; actual rendered payloads are retained in sensitive local artifacts.",
        ],
    }
    return manifest, list(records.values())
