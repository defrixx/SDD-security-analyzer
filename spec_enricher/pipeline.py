"""P0–P8 orchestration. Models propose data; deterministic services own authority."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .artifacts import ArtifactStore
from .assessment import record_effect
from .call_control import CallApprovalRequired, pending_call, require_approval
from .call_control import identity as call_identity
from .catalog import canonical, digest
from .connections import ConnectionProfile
from .domain import condition
from .effective_state import Block, Operation, blocks, resolve, target_quote
from .evaluation import active_clauses, context_only, matrix_row, verification_input
from .llm import LLMAdapter, ModelError, ModelOutputTruncated, repair_text
from .model_diagnostics import safe_network_failure, safe_output_rejection
from .network import NetworkError
from .pipeline_config import rendered
from .prompts import (
    ARCHITECTURAL_REVIEW_PHASE,
    CANDIDATE_REASSESSMENT_PHASE,
)
from .registry import Registry
from .retrieval import ContextIndex
from .run_contracts import RunInput
from .runs import RunRepository, now
from .schema_worker import SchemaValidationUnavailable, validate_payload
from .secrets import SecretReferenceError
from .snapshots import Patch, apply, sha256
from .stage_contracts import (
    Citation,
    CritiqueOutput,
    FactsOutput,
    JudgmentsOutput,
    OperationsOutput,
    PatchesOutput,
    ProposedPatch,
    bind_schema,
    declared_operation_deferral,
    evidence,
    validate_facts,
    validate_judgments,
)
from .storage import Store


class Cancelled(ValueError):
    pass


class BudgetExceeded(ValueError):
    pass


class ContextExceeded(ValueError):
    pass


class PermissionRevoked(ValueError):
    pass


def documentary_requirement(record):
    return {
        key: record[key]
        for key in (
            "id",
            "version",
            "statement",
            "applicability",
            "clauses",
            "clause_logic",
            "clause_normativity",
            "source_context",
        )
    }


def implementation_claim(text: str) -> bool:
    """Flag explicit current-state claims, not future design obligations.

    This deterministic check supplements the independent model critique; it is
    not a universal natural-language proof checker.
    """
    return bool(
        re.search(
            r"\b(?:контроль|контроли|защита)\s+(?:уже\s+)?(?:реализован[а-яё]*|внедр[её]н[а-яё]*|проверен[а-яё]*)\b"
            r"|\b(?:controls?|safeguards?|protection)\s+(?:is|are|has been|have been)\s+(?:already\s+)?(?:implemented|verified|deployed|operational)\b"
            r"|безопасность гарантирована|security (?:is guaranteed|certified)",
            text,
            re.IGNORECASE,
        )
    )


class PipelineRunner:
    def __init__(
        self,
        database: Path,
        artifact_root: Path,
        secret_store,
        project_id: str,
        run_id: str,
        *,
        adapter_factory=LLMAdapter,
    ):
        self.store = Store(database)
        self.repo = RunRepository(self.store, ArtifactStore(artifact_root))
        self.project_id, self.run_id = project_id, run_id
        run = self.repo.get(project_id, run_id)
        if run is None:
            raise ValueError("run_scope")
        self.manifest = run["manifest"]
        self.request = RunInput.model_validate(self.manifest["request"])
        self.secret_store, self.adapter_factory = secret_store, adapter_factory
        self.records = []
        for rid, catalog_id in self.manifest["requirement_ownership"].items():
            record = self.store.requirement(project_id, catalog_id, rid)
            if record is None:
                raise ValueError("pinned_requirement_missing")
            self.records.append(record)
        self.state: dict[str, dict] = {}
        self.envelope: dict = {}
        self.started = time.monotonic()
        self.request_count = 0
        self.stage_requests: dict[str, int] = {}
        self.token_count = 0
        self.usage_unknown = False
        self.skill_outputs: list[dict] = []
        self.skill_requests: dict[str, int] = {}
        self.skill_incomplete = False
        self.active_seconds_before = 0.0

    def check(self):
        if self.repo.cancelled(self.project_id, self.run_id):
            raise Cancelled("cancelled")
        if (
            self.active_seconds_before + time.monotonic() - self.started
            > self.request.limits.wall_seconds
        ):
            raise BudgetExceeded("wall_budget_exhausted")

    def artifact(self, kind: str, value: object, metadata: dict | None = None):
        content = value if isinstance(value, bytes) else canonical(value)
        self.secret_store.reject_known(content)
        result = self.repo.add_artifact(self.project_id, self.run_id, kind, content, metadata or {})
        if self.envelope:
            self.envelope["output_artifact_refs"].append(result["id"])
            self.repo.save_stage(self.project_id, self.envelope)
        return result

    @staticmethod
    def assessment_scope(data: dict):
        return data.get("assessment_scope") or data.get("stage_input", {}).get("assessment_scope")

    def visible_skill_outputs(self, data: dict):
        if self.assessment_scope(data) == "baseline_only":
            return [
                item
                for item in self.skill_outputs
                if item.get("assessment_scope") == "baseline_only"
            ]
        return self.skill_outputs

    def call(self, stage: str, data: dict, contract, validate=None):
        skills = [skill for skill in self.manifest.get("skills", []) if skill["stage"] == stage]
        for skill in skills:
            if skill["hook"] == "before":
                self.skill_call(skill, data)
        replacing = next((skill for skill in skills if skill["hook"] == "replace"), None)
        visible = self.visible_skill_outputs(data)
        effective = data if not visible else data | {"declarative_skill_outputs": visible}
        output = self.complete(
            stage,
            effective,
            contract,
            validate,
            prompt=None if replacing is None else replacing["prompt"],
            skill=replacing,
        )
        for skill in skills:
            if skill["hook"] == "after":
                self.skill_call(skill, {"stage_input": data, "stage_output": output.model_dump()})
        return output

    def skill_call(self, skill: dict, data: dict):
        payload = {"stage_input": data, "prior_skill_outputs": self.visible_skill_outputs(data)}
        try:
            if validate_payload(skill["input_schema"], payload):
                raise ValueError("skill_input_contract_rejected")
            output = self.complete(
                skill["stage"],
                payload,
                None,
                prompt=skill["prompt"],
                schema=skill["output_schema"],
                skill=skill,
            )
            self.skill_outputs.append(
                {
                    "skill_id": skill["id"],
                    "revision": skill["revision"],
                    "stage": skill["stage"],
                    "hook": skill["hook"],
                    "assessment_scope": self.assessment_scope(data),
                    "output": output,
                    "trust": "untrusted_annotation_not_permissions_or_review_decision",
                }
            )
        except (
            Cancelled,
            BudgetExceeded,
            ContextExceeded,
            PermissionRevoked,
            SecretReferenceError,
            SchemaValidationUnavailable,
        ):
            raise
        except (ModelError, NetworkError, ValueError):
            if skill["failure_policy"] == "stop":
                raise
            self.skill_incomplete = True
            self.envelope["warnings"].append(
                {
                    "code": "report_skill_failed",
                    "skill_id": skill["id"],
                    "revision": skill["revision"],
                }
            )
            self.repo.save_stage(self.project_id, self.envelope)

    def complete(
        self,
        stage: str,
        data: dict,
        contract,
        validate=None,
        *,
        prompt=None,
        schema=None,
        skill=None,
    ):
        self.check()
        configured = self.manifest["connections"][stage]
        scopes = (
            {"delta", "baseline_fragments"}
            | (set() if stage == "P2" else {"catalog"})
            | ({"skill_outputs"} if self.manifest.get("skills") else set())
        )

        def authorize():
            try:
                Registry(self.store).require_egress(
                    self.project_id, configured["id"], configured["revision"], scopes
                )
            except ValueError as exc:
                raise PermissionRevoked("egress_permission_revoked") from exc

        authorize()
        profile = ConnectionProfile.model_validate(configured["content"])
        if skill is not None:
            profile = profile.model_copy(
                update={
                    "timeout": min(profile.timeout, skill["timeout"]),
                    "max_output_tokens": min(
                        profile.max_output_tokens, skill["budget"]["max_output_tokens"]
                    ),
                }
            )
        adapter = self.adapter_factory(self.project_id, profile, self.secret_store)
        schema = (
            schema
            if schema is not None
            else bind_schema(
                stage,
                contract.model_json_schema(),
                data,
                literal_choices=profile.output_mode == "json_schema",
            )
        )
        instruction = prompt or self.manifest["prompts"][stage]
        messages = [
            {
                "role": "system",
                "content": self.manifest["application_policy"]
                + "\n\n"
                + rendered(instruction, stage, schema),
            },
            {
                "role": "user",
                "content": canonical({"trust": "untrusted_data_only", "data": data}).decode(),
            },
        ]
        base_payload = adapter.payload(messages, schema)
        cache_key = digest(
            {
                "profile": configured["sha256"],
                "methodology": self.manifest["methodology_hash"],
                "stage": stage,
                "execution_mode": self.manifest.get("execution_mode", "real_adapter"),
                "payload": base_payload,
            }
        )
        cached = self.repo.cached(self.project_id, cache_key)
        if cached is not None:
            self.secret_store.reject_known(cached["body"])
            output = (
                contract.model_validate(json.loads(cached["body"]))
                if contract
                else json.loads(cached["body"])
            )
            if validate:
                validate(output)
            if cached["source_run_id"] == self.run_id:
                self.envelope["output_artifact_refs"].append(cached["id"])
                self.envelope["warnings"].append(
                    {
                        "code": "same_run_output_restored_without_new_call",
                        "artifact_id": cached["id"],
                    }
                )
                self.repo.save_stage(self.project_id, self.envelope)
                return output
            self.artifact(
                "model.output",
                output.model_dump() if contract else output,
                {
                    "cache_key": cache_key,
                    "cache_reused": True,
                    "source_artifact": cached["id"],
                    "stage": stage,
                },
            )
            return output
        repaired = False
        raw_content = None
        retries = 0
        if self.request.step_mode:
            pending = pending_call(
                self.repo, self.project_id, self.run_id, logical_key=cache_key, body=True
            )
            if pending:
                # Wire messages may already contain adapter-owned formatting
                # (prompt_json schema instructions). Rebuild once from the exact
                # logical conversation, never format the wire payload again.
                messages = pending["retry_state"]["logical_messages"]
                if digest(adapter.payload(messages, schema)) != pending["payload_sha256"]:
                    raise ValueError("pending_payload_configuration_changed")
                repaired = pending["retry_state"]["repaired"]
                retries = pending["retry_state"]["retries"]
        while True:
            # A retry/repair is a new transmission. Recheck after any delay or
            # in-flight response, before resolving headers and again at send.
            authorize()
            raw_content = None

            def before(payload, repaired=repaired, retries=retries, logical_messages=messages):
                self.check()
                authorize()
                if skill is not None:
                    if self.skill_requests.get(skill["id"], 0) >= skill["budget"]["max_requests"]:
                        raise BudgetExceeded("skill_request_budget_exhausted")
                input_bytes = len(json.dumps(payload, ensure_ascii=False).encode())
                if input_bytes + profile.max_output_tokens > profile.context_budget:
                    raise ContextExceeded("context_budget_exceeded")
                reserve = input_bytes + profile.max_output_tokens
                if (
                    self.request_count >= self.request.limits.max_requests
                    or self.stage_requests.get(stage, 0) >= self.request.limits.stage_max_requests
                    or self.token_count + reserve > self.request.limits.max_tokens
                ):
                    raise BudgetExceeded("request_or_token_budget_exhausted")
                self.secret_store.reject_known(canonical(payload))
                ordinal, call_key = call_identity(
                    self.repo, self.project_id, self.run_id, cache_key, payload
                )
                if self.request.step_mode:
                    ordinal, call_key = require_approval(
                        self.repo,
                        self.project_id,
                        self.run_id,
                        cache_key,
                        payload,
                        {
                            "repaired": repaired,
                            "retries": retries,
                            "logical_messages": logical_messages,
                        },
                        configured,
                        stage,
                    )
                if skill is not None:
                    self.skill_requests[skill["id"]] = self.skill_requests.get(skill["id"], 0) + 1
                self.request_count += 1
                self.stage_requests[stage] = self.stage_requests.get(stage, 0) + 1
                self.token_count += reserve
                self.usage_unknown = True
                self.artifact(
                    "model.request",
                    payload,
                    {
                        "stage": stage,
                        "logical_call_key": cache_key,
                        "call_key": call_key,
                        "request_ordinal": ordinal,
                        "attempt": self.request_count,
                        "skill_id": None if skill is None else skill["id"],
                        "conservative_token_reservation": reserve,
                    },
                )
                self.store.audit(self.project_id, "llm_request", configured["id"])

            def after(content, latency):
                nonlocal raw_content
                raw_content = content
                self.artifact(
                    "model.raw", content, {"stage": stage, "latency_ms": latency, "sensitive": True}
                )

            try:
                completion = adapter.complete(
                    messages, schema, before_request=before, after_response=after
                )
                output = contract.model_validate(completion.value) if contract else completion.value
                if validate:
                    validate(output)
                saved = self.artifact(
                    "model.output",
                    output.model_dump() if contract else output,
                    {
                        "cache_key": cache_key,
                        "stage": stage,
                        "skill_id": None if skill is None else skill["id"],
                        "usage": completion.usage,
                        "format_normalization": completion.format_normalization,
                        "cache_reused": False,
                    },
                )
                self.repo.cache(self.project_id, cache_key, saved["id"])
                self.check()
                return output
            except NetworkError as exc:
                self.artifact(
                    "model.transport_error", safe_network_failure(exc, stage), {"stage": stage}
                )
                if (
                    exc.code
                    not in {"rate_limited", "connect_or_protocol_failed", "request_timeout"}
                    or retries >= profile.retries
                ):
                    raise
                delay = exc.retry_after if exc.retry_after is not None else 2**retries
                retries += 1
                self.envelope["warnings"].append(
                    {"code": exc.code, "retry": retries, "delay_seconds": delay}
                )
                self.repo.save_stage(self.project_id, self.envelope)
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    self.check()
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            except (
                ContextExceeded,
                BudgetExceeded,
                Cancelled,
                PermissionRevoked,
                SecretReferenceError,
                SchemaValidationUnavailable,
            ):
                raise
            except (ModelError, ValueError) as exc:
                # Retain safe diagnostics, never Pydantic input/context or arbitrary
                # exception text (which can contain model/document/secret values).
                terminal = isinstance(exc, ModelOutputTruncated) or str(exc) in {
                    "credential_echo_rejected",
                    "reasoning_option_not_honored",
                }
                rejection = safe_output_rejection(
                    exc,
                    stage,
                    repair_permitted=not repaired and not terminal,
                    raw_content=raw_content,
                    contract=contract,
                    output_mode=profile.output_mode,
                )
                self.artifact("model.rejection", rejection)
                if isinstance(exc, ModelOutputTruncated):
                    raise
                if repaired or terminal:
                    raise ValueError("model_contract_rejected_after_bounded_repair") from exc
                repaired = True
                previous = repair_text(raw_content)
                messages = messages + [
                    {"role": "assistant", "content": previous},
                    {
                        "role": "user",
                        "content": "One permitted structural/reference repair. Your output failed schema, coverage or exact evidence/ID validation. Return the complete contract with only supplied IDs and exact quotes. Do not infer missing evidence. Use unknown and a question when needed. Safe validation diagnostics: "
                        + canonical(rejection).decode(),
                    },
                ]

    def restore_checkpoint(self):
        checkpoint = self.repo.checkpoint(self.project_id, self.run_id)
        for number, artifact in enumerate(checkpoint["outputs"]):
            value = json.loads(
                self.repo.artifact(self.project_id, self.run_id, artifact["id"], body=True)["body"]
            )
            self.state["P" + str(number)] = value
            self.skill_outputs = value.get("declarative_skill_state", self.skill_outputs)
            self.skill_incomplete = value.get("skill_incomplete", self.skill_incomplete)
        for artifact in self.repo.list_artifacts(self.project_id, self.run_id, "model.request"):
            metadata = artifact["metadata"]
            self.request_count += 1
            self.token_count += metadata["conservative_token_reservation"]
            stage = metadata["stage"]
            self.stage_requests[stage] = self.stage_requests.get(stage, 0) + 1
            if metadata.get("skill_id"):
                key = metadata["skill_id"]
                self.skill_requests[key] = self.skill_requests.get(key, 0) + 1
        for stage in self.repo.stages(self.project_id, self.run_id):
            self.active_seconds_before = max(
                self.active_seconds_before, stage["usage"].get("active_seconds_total", 0)
            )
        return checkpoint

    def execute(self, *, resume=False):
        try:
            checkpoint: dict = (
                self.restore_checkpoint() if resume else {"next_stage": 0, "outputs": []}
            )
            self.repo.set_status(self.project_id, self.run_id, "running")
            inputs = [
                checkpoint["outputs"][-1]["id"]
                if checkpoint["outputs"]
                else self.repo.get(self.project_id, self.run_id)["configuration_snapshot_ref"]
            ]
            for number in range(checkpoint["next_stage"], 9):
                self.check()
                stage = "P" + str(number)
                self.envelope = self.repo.stage(
                    self.project_id, self.run_id, stage, inputs, self.request.limits.model_dump()
                )
                try:
                    value = getattr(self, stage)()
                    value["declarative_skill_state"] = list(self.skill_outputs)
                    value["skill_incomplete"] = self.skill_incomplete
                    output = self.artifact("stage." + stage + ".output", value)
                    self.state[stage] = value
                    self.envelope.update(
                        status="partial" if value.get("incomplete") else "succeeded",
                        finished_at=now(),
                        usage={
                            "request_count_total": self.request_count,
                            "conservative_token_count": self.token_count,
                            "provider_usage": "see_call_artifacts_or_unknown",
                            "cost": "unknown_no_user_tariff",
                            "active_seconds_total": self.active_seconds_before
                            + time.monotonic()
                            - self.started,
                        },
                    )
                    if value.get("incomplete"):
                        self.envelope["warnings"].append(
                            {
                                "code": "incomplete_stage",
                                "reason": value.get(
                                    "reason", "explicit_questions_or_coverage_limits"
                                ),
                            }
                        )
                    self.repo.save_stage(self.project_id, self.envelope)
                    inputs = [output["id"]]
                    if self.request.step_mode and number < 8:
                        self.repo.set_status(self.project_id, self.run_id, "waiting_approval")
                        return
                except CallApprovalRequired:
                    self.envelope.update(
                        status="waiting_approval",
                        finished_at=now(),
                        usage={
                            "request_count_total": self.request_count,
                            "conservative_token_count": self.token_count,
                            "active_seconds_total": self.active_seconds_before
                            + time.monotonic()
                            - self.started,
                        },
                    )
                    self.repo.save_stage(self.project_id, self.envelope)
                    self.repo.set_status(self.project_id, self.run_id, "waiting_approval")
                    return
                except (OSError, sqlite3.Error, ValueError) as exc:
                    self.envelope.update(
                        status="cancelled"
                        if isinstance(exc, Cancelled)
                        else "partial"
                        if isinstance(exc, (BudgetExceeded, ContextExceeded, ModelOutputTruncated))
                        else "failed",
                        finished_at=now(),
                    )
                    self.envelope["errors"].append(
                        {
                            "code": "cancelled"
                            if isinstance(exc, Cancelled)
                            else "egress_permission_revoked"
                            if isinstance(exc, PermissionRevoked)
                            else "credential_reference_unavailable"
                            if isinstance(exc, SecretReferenceError)
                            else "local_schema_validation_unavailable"
                            if isinstance(exc, SchemaValidationUnavailable)
                            else "context_budget_exceeded"
                            if isinstance(exc, ContextExceeded)
                            else "model_output_truncated"
                            if isinstance(exc, ModelOutputTruncated)
                            else "budget_exhausted"
                            if isinstance(exc, BudgetExceeded)
                            else "stage_contract_or_adapter_error",
                            "message": "Connection/destination/data scope or operator permission is no longer valid. No further request was sent. Review permissions and create a new run explicitly."
                            if isinstance(exc, PermissionRevoked)
                            else "Configured credential reference is unavailable. Check the session/file reference and permissions, then repeat capability test explicitly. No request was sent using this unresolved reference."
                            if isinstance(exc, SecretReferenceError)
                            else "Local JSON Schema validation did not complete. No model repair or further request was sent. The result remains unvalidated; check local resources/schema and rerun explicitly."
                            if isinstance(exc, SchemaValidationUnavailable)
                            else "Serialized input plus reserved output exceeds the configured context budget. Reduce selected context/batch size or explicitly configure a sufficient model budget; no request was sent for this payload."
                            if isinstance(exc, ContextExceeded)
                            else "Provider marked finish_reason=length. Inspect retained raw usage, reduce context/batch or explicitly adjust output and loaded model context before a new run."
                            if isinstance(exc, ModelOutputTruncated)
                            else "Stage did not complete; inspect locally retained inputs/outputs. No implementation verification is implied.",
                        }
                    )
                    self.repo.save_stage(self.project_id, self.envelope)
                    raise
            incomplete = self.skill_incomplete or any(
                value.get("incomplete") for value in self.state.values()
            )
            self.repo.set_status(
                self.project_id, self.run_id, "partial" if incomplete else "succeeded"
            )
        except Cancelled:
            self.repo.set_status(self.project_id, self.run_id, "cancelled", "cancelled")
        except PermissionRevoked:
            self.repo.set_status(
                self.project_id, self.run_id, "failed", "egress_permission_revoked"
            )
        except SecretReferenceError:
            self.repo.set_status(
                self.project_id, self.run_id, "failed", "credential_reference_unavailable"
            )
        except SchemaValidationUnavailable:
            self.repo.set_status(
                self.project_id, self.run_id, "failed", "local_schema_validation_unavailable"
            )
        except ModelOutputTruncated:
            self.repo.set_status(self.project_id, self.run_id, "partial", "model_output_truncated")
        except ContextExceeded:
            self.repo.set_status(self.project_id, self.run_id, "partial", "context_budget_exceeded")
        except BudgetExceeded:
            self.repo.set_status(self.project_id, self.run_id, "partial", "budget_exhausted")
        except (OSError, sqlite3.Error, ValueError):
            self.repo.set_status(
                self.project_id, self.run_id, "failed", "stage_contract_or_storage_error"
            )
        finally:
            try:
                if "P6" not in self.state and self.repo.get(self.project_id, self.run_id)[
                    "status"
                ] not in {"waiting_approval", "running", "queued"}:
                    self.persist_results(failure_reason="pipeline_incomplete")
            finally:
                self.store.close()

    def P0(self):
        self.artifact(
            "catalog.snapshot",
            {
                "schema_version": "4.0",
                "requirements": self.records,
                "catalogs": self.manifest["catalogs"],
            },
        )
        delta = self.store.document(self.project_id, self.request.delta_document_id)
        self.artifact("delta.original.md", delta[2])
        for snapshot in self.manifest.get("source_snapshots", []):
            source = self.store.connection.execute(
                "SELECT raw_content,raw_sha256 FROM source_snapshots WHERE project_id=? AND id=?",
                (self.project_id, snapshot["id"]),
            ).fetchone()
            if (
                source is None
                or sha256(source[0]) != snapshot["raw_sha256"]
                or source[1] != snapshot["raw_sha256"]
            ):
                raise ValueError("pinned_original_source_hash_conflict")
            self.artifact("source.original", source[0], snapshot)
        return {
            "manifest": self.manifest,
            "requirement_count": len(self.records),
            "implementation_verification": "not_verified",
            "incomplete": False,
        }

    def P1(self):
        documents = []
        for selected in self.manifest["documents"]:
            self.check()
            row = self.store.document(self.project_id, selected["id"])
            if row is None or sha256(row[2]) != selected["sha256"]:
                raise ValueError("pinned_source_missing_or_changed")
            documents.append(selected | {"content": row[2].decode("utf-8")})
            self.store.audit(self.project_id, "source_snapshot_read", selected["id"])
        return {"documents": documents, "fetch_errors": [], "incomplete": False}

    def block_maps(self):
        source = self.state["P2"]
        baseline = {item["id"]: Block(**item) for item in source["baseline_blocks"]}
        proposed = {item["id"]: Block(**item) for item in source["proposed_blocks"]}
        return baseline, proposed

    def P2(self):
        baseline: list[Block] = []
        context: list[Block] = []
        delta: list[Block] = []
        for doc in self.state["P1"]["documents"]:
            parsed = blocks(doc["id"], doc["content"].encode())
            (
                delta
                if doc["role"] == "delta"
                else baseline
                if doc["role"] == "master_spec"
                else context
            ).extend(parsed)
        index = ContextIndex(baseline)
        operations = []
        questions = []
        retrieval = []
        overrides = {op.delta_block_id: op for op in self.request.operation_overrides}
        if len(overrides) != len(self.request.operation_overrides) or set(overrides) - {
            block.id for block in delta
        }:
            raise ValueError("invalid_operation_override")
        try:
            for block in delta:
                if block.id in overrides:
                    output_operations = [overrides[block.id]]
                else:
                    profile = self.manifest["connections"]["P2"]["content"]
                    selected, log = index.retrieve(
                        block.text,
                        allowed=set(index.blocks),
                        max_bytes=min(10000, profile["context_budget"] // 3),
                    )
                    retrieval.append(log)
                    allowed = {item.id: item for item in selected}

                    def validate(output, block=block, allowed=allowed):
                        if (
                            len(output.operations) != 1
                            or output.operations[0].delta_block_id != block.id
                        ):
                            raise ValueError("delta_operation_coverage")
                        op = output.operations[0]
                        if declared_operation_deferral(block.text) and op.kind != "unresolved":
                            raise ValueError("explicit_operation_deferral_requires_question")
                        if op.target_block_id is not None and (
                            op.target_block_id not in allowed
                            or op.target_hash != allowed[op.target_block_id].hash
                        ):
                            raise ValueError("invented_delta_target")
                        if op.kind in {"replace", "delete"}:
                            if op.target_block_id is None:
                                raise ValueError("invented_delta_target")
                            target_quote(block, allowed[op.target_block_id], op.target_quote)
                        if op.kind == "unresolved" and not op.question:
                            raise ValueError("unresolved_operation_requires_question")

                    output = self.call(
                        "P2",
                        {
                            "delta_blocks": [asdict(block)],
                            "baseline_blocks": [asdict(item) for item in selected],
                        },
                        OperationsOutput,
                        validate,
                    )
                    output_operations = output.operations
                    questions.extend(output.questions)
                for op in output_operations:
                    if op.question:
                        questions.append(op.question)
                    operations.append(
                        Operation(
                            "operation-" + digest({"delta_block_id": op.delta_block_id})[:24],
                            op.kind,
                            op.delta_block_id,
                            op.target_block_id,
                            op.target_hash,
                            target_quote=op.target_quote,
                        )
                    )
        finally:
            index.close()
        state = resolve(baseline, delta, operations)
        proposed = list(state.blocks) + context
        # Deletion/ambiguous descriptions remain evidence of what delta says.
        known = {item.id for item in proposed}
        proposed.extend(item for item in delta if item.id not in known)
        return {
            "baseline_blocks": [asdict(item) for item in baseline + context],
            "proposed_blocks": [asdict(item) for item in proposed],
            "delta_blocks": [asdict(item) for item in delta],
            "operations": [asdict(item) for item in operations],
            "unresolved_operations": list(state.unresolved),
            "questions": questions,
            "retrieval": retrieval,
            "incomplete": bool(state.unresolved),
            "reason": "delta_ambiguity" if state.unresolved else None,
        }

    def contexts(self, records, baseline, proposed, query=""):
        query = query or " ".join(
            record["statement"]
            + " "
            + " ".join(p["text"] for p in record["applicability"]["predicates"])
            for record in records
        )
        union = {**baseline, **proposed}
        index = ContextIndex(list(union.values()))
        try:
            profile = self.manifest["connections"][self.envelope["stage_id"]]["content"]
            limit = min(12000, profile["context_budget"] // 4)
            before, log1 = index.retrieve(query, allowed=set(baseline), max_bytes=limit)
            after, log2 = index.retrieve(
                query,
                allowed=set(proposed),
                max_bytes=limit,
                priority_ids=[block["id"] for block in self.state["P2"]["delta_blocks"]],
            )
            self.artifact("retrieval.trace", {"baseline": log1, "proposed": log2})
            return {item.id: item for item in before}, {item.id: item for item in after}
        finally:
            index.close()

    def P3(self):
        baseline, proposed = self.block_maps()
        facts: list[dict] = []
        conflicts: list[dict] = []
        questions: list[str] = []
        for record in self.records:
            if context_only(record):
                continue
            unspecified = set(
                record.get("policy_scope", {}).get("authoring_unknown_predicate_ids", [])
            )
            predicates = [
                p for p in record["applicability"]["predicates"] if p["id"] not in unspecified
            ]
            for predicate in record["applicability"]["predicates"]:
                if predicate["id"] not in unspecified:
                    continue
                question = record["applicability"]["unknown_when"]
                questions.append(question)
                for side in ("baseline", "proposed"):
                    facts.append(
                        {
                            "requirement_id": record["id"],
                            "version": record["version"],
                            "predicate_id": predicate["id"],
                            "side": side,
                            "value": None,
                            "evidence_state": "insufficient",
                            "evidence": [],
                            "explanation": "Author explicitly left this applicability condition undefined; no architectural premise inferred.",
                            "question": question,
                        }
                    )
            before, after = self.contexts([record], baseline, proposed)
            for start in range(0, len(predicates), self.request.limits.batch_size):
                partial = record | {
                    "applicability": record["applicability"]
                    | {"predicates": predicates[start : start + self.request.limits.batch_size]}
                }
                data_record = {
                    key: partial[key]
                    for key in ("id", "version", "source_context", "applicability")
                }
                data = {
                    "requirements": [data_record],
                    "baseline_blocks": [
                        asdict(item) | {"evidence_side": "baseline"} for item in before.values()
                    ],
                    "proposed_blocks": [
                        asdict(item) | {"evidence_side": "proposed"} for item in after.values()
                    ],
                    "operations": self.state["P2"]["operations"],
                    "unresolved_operations": self.state["P2"]["unresolved_operations"],
                }
                output = self.call(
                    "P3",
                    data,
                    FactsOutput,
                    lambda value, partial=partial, before=before, after=after: validate_facts(
                        value, [partial], before, after
                    ),
                )
                output = self.review_architectural_facts(partial, data, output, before, after)
                facts.extend(item.model_dump() for item in output.facts)
                conflicts.extend(item.model_dump() for item in output.conflicts)
                questions.extend(output.questions)
                questions.extend(item.question for item in output.facts if item.question)
                questions.extend(
                    "Clarify the authority of conflicting snapshots: " + item.explanation
                    for item in output.conflicts
                )
        resolved_questions = set()
        for override in self.request.fact_overrides:
            for fact in facts:
                if (fact["requirement_id"], fact["side"], fact["predicate_id"]) == (
                    override.requirement_id,
                    override.side,
                    override.predicate_id,
                ):
                    if override.value is not None and fact.get("question"):
                        resolved_questions.add(fact["question"])
                    fact.update(
                        model_value=fact["value"],
                        value=override.value,
                        evidence_state="inferred",
                        override_reason=override.reason,
                        user_override=True,
                        question=None if override.value is not None else override.reason,
                    )
        questions = [question for question in questions if question not in resolved_questions]
        questions.extend(fact["question"] for fact in facts if fact.get("question"))
        return {
            "facts": facts,
            "conflicts": conflicts,
            "questions": list(dict.fromkeys(questions)),
            "incomplete": bool(conflicts),
        }

    def review_architectural_facts(self, record, data, output, before, after):
        if not any(
            fact.value is False or fact.value is None and fact.evidence_state == "conflicting"
            for fact in output.facts
        ):
            return output
        review_data = data | {"assessment_phase": ARCHITECTURAL_REVIEW_PHASE}
        schema = bind_schema(
            "P3",
            FactsOutput.model_json_schema(),
            review_data,
            literal_choices=self.manifest["connections"]["P3"]["content"]["output_mode"]
            == "json_schema",
        )
        schema["properties"]["conflicts"]["maxItems"] = 0

        def validate(value):
            validate_facts(value, [record], before, after)
            if value.conflicts:
                raise ValueError("architectural_review_conflicts_must_remain_separate")

        reviewed = self.complete(
            "P3",
            review_data,
            FactsOutput,
            validate,
            prompt=self.manifest["architectural_review_prompt"],
            schema=schema,
        )
        self.artifact(
            "assessment.architectural_review",
            {
                "phase": ARCHITECTURAL_REVIEW_PHASE,
                "preliminary": [fact.model_dump() for fact in output.facts],
                "reviewed": [fact.model_dump() for fact in reviewed.facts],
                "control_conflicts_retained": [item.model_dump() for item in output.conflicts],
                "semantic_proof": False,
            },
            {"stage": "P3", "phase": ARCHITECTURAL_REVIEW_PHASE},
        )
        return output.model_copy(
            update={"facts": reviewed.facts, "questions": output.questions + reviewed.questions}
        )

    def facts_for(self, record, side):
        return {
            item["predicate_id"]: item["value"]
            for item in self.state.get("P3", {}).get("facts", [])
            if item["requirement_id"] == record["id"] and item["side"] == side
        }

    def P4(self):
        rows = []
        for record in self.records:
            for side in ("baseline", "proposed"):
                facts = self.facts_for(record, side)
                value = condition(record["applicability"]["expression"], facts)
                active, uncertain = active_clauses(record, facts)
                rows.append(
                    {
                        "requirement_id": record["id"],
                        "version": record["version"],
                        "side": side,
                        "applicability": value,
                        "active_clauses": sorted(active),
                        "uncertain_clauses": sorted(uncertain),
                        "facts": facts,
                    }
                )
        return {"rows": rows, "incomplete": False}

    def P5(self):
        baseline, proposed = self.block_maps()
        judgments: list[dict] = []
        questions: list[str] = []
        for record in self.records:
            by_side = {
                row["side"]: row
                for row in self.state["P4"]["rows"]
                if row["requirement_id"] == record["id"]
            }
            keys = [
                (record["id"], side, cid)
                for side, row in by_side.items()
                if row["applicability"] is True
                for cid in row["active_clauses"]
            ]
            clause_ids = sorted({key[2] for key in keys})
            # Baseline judgments must not see future changes, facts or plans.
            # Proposed judgments retain baseline only for explicit removal proof.
            for side in ("baseline", "proposed"):
                side_proposed = {} if side == "baseline" else proposed
                before, after = self.contexts([record], baseline, side_proposed)
                for start in range(0, len(clause_ids), self.request.limits.batch_size):
                    selected_clauses = set(
                        clause_ids[start : start + self.request.limits.batch_size]
                    )
                    expected = {
                        key for key in keys if key[1] == side and key[2] in selected_clauses
                    }
                    if not expected:
                        continue
                    row = by_side[side]
                    data = {
                        "assessment_scope": "baseline_only"
                        if side == "baseline"
                        else "proposed_with_removal_context",
                        "requirements": [documentary_requirement(record)],
                        "requested_judgments": [list(key) for key in sorted(expected)],
                        "requested_obligations": [
                            {
                                "requirement_id": record["id"],
                                "version": record["version"],
                                "clause": clause,
                                "normativity": record["clause_normativity"][clause["id"]],
                            }
                            for clause in record["clauses"]
                            if (record["id"], side, clause["id"]) in expected
                        ],
                        "applicability": {side: row},
                        "verification": {
                            side: verification_input(
                                record, row["facts"], set(row["active_clauses"])
                            )
                        },
                    }
                    for expansion in range(4):
                        removal_ids = {
                            op["delta_block_id"]
                            for op in self.state["P2"]["operations"]
                            if side == "proposed" and op["kind"] in {"replace", "delete"}
                        } & set(after)
                        data.update(
                            baseline_blocks=[asdict(item) for item in before.values()],
                            proposed_blocks=[asdict(item) for item in after.values()],
                            delta_block_ids=[
                                b["id"]
                                for b in self.state["P2"]["delta_blocks"]
                                if side == "proposed" and b["id"] in after
                            ],
                            removal_delta_block_ids=sorted(removal_ids),
                        )
                        output = self.call(
                            "P5",
                            data,
                            JudgmentsOutput,
                            lambda value, record=record, expected=expected, before=before, after=after, removal_ids=removal_ids: (
                                validate_judgments(
                                    value,
                                    [record],
                                    expected,
                                    before,
                                    after,
                                    removal_delta_ids=removal_ids,
                                )
                            ),
                        )
                        if not output.retrieval_queries or expansion == 3:
                            break
                        before, after = self.contexts(
                            [record], baseline, side_proposed, " ".join(output.retrieval_queries)
                        )
                    judgments.extend(item.model_dump() for item in output.judgments)
                    questions.extend(output.questions)
                    questions.extend(item.question for item in output.judgments if item.question)
        return {"judgments": judgments, "questions": questions, "incomplete": False}

    def reassess_candidate(self, record, data, output, expected, after):
        """Assess the candidate independently of generator findings/patch history.

        P5's frozen documentary standard compares the original baseline with the
        effective candidate, using the P8 connection and its normal controls.
        Auxiliary baseline judgments never overwrite the original P5 matrix.
        """
        if not expected:
            return output
        baseline, _ = self.block_maps()
        before, _ = self.contexts([record], baseline, {})
        by_side = {
            row["side"]: row
            for row in self.state["P4"]["rows"]
            if row["requirement_id"] == record["id"]
        }
        clauses = sorted({key[2] for key in expected})
        prompt = self.manifest["candidate_reassessment_prompt"]
        result = []
        questions = list(output.questions)
        for start in range(0, len(clauses), self.request.limits.batch_size):
            selected = set(clauses[start : start + self.request.limits.batch_size])
            proposed_keys = {key for key in expected if key[2] in selected}
            comparison_keys = proposed_keys | {
                (record["id"], "baseline", cid)
                for cid in selected
                if by_side["baseline"]["applicability"] is True
                and cid in by_side["baseline"]["active_clauses"]
            }
            review_data = {
                "assessment_phase": CANDIDATE_REASSESSMENT_PHASE,
                "requirements": [documentary_requirement(record)],
                "requested_judgments": [list(key) for key in sorted(comparison_keys)],
                "applicability": by_side,
                "verification": {
                    side: verification_input(record, row["facts"], set(row["active_clauses"]))
                    for side, row in by_side.items()
                },
                "baseline_blocks": [asdict(item) for item in before.values()],
                "proposed_blocks": [asdict(item) for item in after.values()],
                "delta_block_ids": [],
                "removal_delta_block_ids": [],
            }
            schema = bind_schema(
                "P5",
                JudgmentsOutput.model_json_schema(),
                review_data,
                literal_choices=self.manifest["connections"]["P8"]["content"]["output_mode"]
                == "json_schema",
            )

            def validate(value, comparison_keys=comparison_keys):
                validate_judgments(value, [record], comparison_keys, before, after)
                if value.retrieval_queries:
                    raise ValueError("candidate_reassessment_requires_bounded_context_or_question")

            reviewed = self.complete(
                "P8", review_data, JudgmentsOutput, validate, prompt=prompt, schema=schema
            )
            proposed = [item for item in reviewed.judgments if item.side == "proposed"]
            result.extend(proposed)
            questions.extend(reviewed.questions)
            self.artifact(
                "assessment.candidate_reassessment",
                {
                    "phase": CANDIDATE_REASSESSMENT_PHASE,
                    "prompt_sha256": digest(prompt),
                    "candidate_sha256": data["context_scope"]["candidate_sha256"],
                    "comparison": [item.model_dump() for item in reviewed.judgments],
                    "reviewed": [item.model_dump() for item in proposed],
                    "original_P5_matrix_unchanged": True,
                    "semantic_proof": False,
                },
                {"stage": "P8", "phase": CANDIDATE_REASSESSMENT_PHASE},
            )
        return output.model_copy(
            update={
                "reassessment": result,
                "questions": questions,
            }
        )

    def persist_results(self, failure_reason=None):
        rows = []
        conflicts = {
            rid
            for item in self.state.get("P3", {}).get("conflicts", [])
            for rid in item["requirement_ids"]
        }
        adoption = {
            item.requirement_id: {"mode": item.mode, "reason": item.reason}
            for item in self.request.adoption_overrides
        }
        for record in self.records:
            sides = {}
            for side in ("baseline", "proposed"):
                judgments = {
                    item["clause_id"]: item
                    for item in self.state.get("P5", {}).get("judgments", [])
                    if item["requirement_id"] == record["id"] and item["side"] == side
                }
                row = matrix_row(
                    record,
                    self.facts_for(record, side),
                    judgments,
                    reason=failure_reason,
                    adoption=adoption.get(record["id"]),
                    source_conflict=record["id"] in conflicts,
                )
                if (
                    side == "proposed"
                    and row["applicability"] != "not_applicable"
                    and not context_only(record)
                    and row["reason"] != "no_active_clauses"
                    and not failure_reason
                    and self.state.get("P2", {}).get("unresolved_operations")
                ):
                    row.update(
                        assessment="unknown", reason="unresolved_delta_operation", incomplete=True
                    )
                    # An unchosen effective state cannot establish a required
                    # outcome. Explicit project waivers/advice are independent
                    # decisions, not documentary passes, and remain visible.
                    if row["enforcement"] not in {"advisory", "waived"}:
                        row.update(
                            enforcement="unknown",
                            enforcement_reason=row.get("enforcement_reason")
                            or "unresolved_delta_operation",
                        )
                sides[side] = row
            change, branch_changes = record_effect(
                record,
                sides["baseline"],
                sides["proposed"],
                self.facts_for(record, "baseline"),
                self.facts_for(record, "proposed"),
                additions_only=bool(self.state.get("P2", {}).get("operations"))
                and all(op["kind"] == "add" for op in self.state["P2"]["operations"]),
            )
            sides["proposed"]["branch_change_effects"] = branch_changes
            result = {
                "requirement_id": record["id"],
                "version": record["version"],
                "baseline": sides["baseline"],
                "proposed": sides["proposed"],
                "change_effect": change,
            }
            with self.store.connection:
                self.store.connection.execute(
                    "INSERT INTO requirement_results VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id,run_id,requirement_id) DO NOTHING",
                    (
                        self.project_id,
                        self.run_id,
                        record["id"],
                        self.manifest["requirement_ownership"][record["id"]],
                        record["version"],
                        json.dumps(sides["baseline"], ensure_ascii=False),
                        json.dumps(sides["proposed"], ensure_ascii=False),
                        change,
                    ),
                )
            rows.append(result)
        return rows

    def P6(self):
        rows = self.persist_results()
        findings = []
        by_id = {record["id"]: record for record in self.records}
        baseline, proposed = self.block_maps()
        for row in rows:
            assessment = row["proposed"]["assessment"]
            if assessment not in {"contradicted_by_spec", "not_addressed", "unknown"}:
                continue
            record = by_id[row["requirement_id"]]
            conflicts = [
                item
                for item in self.state["P3"]["conflicts"]
                if record["id"] in item["requirement_ids"]
            ]
            kind = (
                "source_conflict"
                if conflicts
                or any(
                    row[side]["reason"] == "source_conflict" for side in ("baseline", "proposed")
                )
                else "uncertain_applicability"
                if assessment == "unknown"
                else "existing_issue"
                if row["change_effect"] == "unchanged"
                else "explicit_contradiction"
                if assessment == "contradicted_by_spec"
                else "missing_security_design"
            )
            refs = []
            for side, allowed in (("baseline", baseline), ("proposed", proposed)):
                for item in row[side]["clause_assessments"].values():
                    refs.extend(
                        evidence(
                            [Citation.model_validate(value) for value in item["evidence"]], allowed
                        )
                    )
                    removal = item.get("control_removal")
                    if removal:
                        refs.extend(
                            evidence(
                                [Citation.model_validate(c) for c in removal["baseline_evidence"]],
                                baseline,
                            )
                        )
                        refs.extend(
                            evidence(
                                [Citation.model_validate(c) for c in removal["delta_evidence"]],
                                {
                                    b["id"]: proposed[b["id"]]
                                    for b in self.state["P2"]["delta_blocks"]
                                    if b["id"] in proposed
                                },
                            )
                        )
            for conflict in conflicts:
                refs.extend(
                    evidence(
                        [Citation.model_validate(value) for value in conflict["evidence"]], baseline
                    )
                )
            for fact in self.state["P3"]["facts"]:
                if fact["requirement_id"] == record["id"] and fact["evidence"]:
                    refs.extend(
                        evidence(
                            [Citation.model_validate(value) for value in fact["evidence"]],
                            baseline if fact["side"] == "baseline" else proposed,
                        )
                    )
            refs = list({digest(ref): ref for ref in refs}.values())
            by_document = {
                document["id"]: document.get("source_snapshot")
                for document in self.manifest["documents"]
            }
            for ref in refs:
                source = by_document.get(ref["snapshot_id"])
                if source:
                    ref.update(
                        source_id=source["source_id"],
                        source_snapshot_id=source["id"],
                        canonical_uri=source["uri"],
                        raw_source_sha256=source["raw_sha256"],
                        fetched_at=source["metadata"]["fetched_at"],
                        transform=source["metadata"]["transform"],
                        transport_metadata=source["metadata"].get("transport_metadata", {}),
                    )
            finding = {
                "id": "finding-"
                + digest({"run": self.run_id, "requirement": record["id"], "type": kind})[:24],
                "type": kind,
                "requirement_ref": {"id": record["id"], "version": record["version"]},
                "baseline_assessment": row["baseline"],
                "proposed_assessment": row["proposed"],
                "change_effect": row["change_effect"],
                "severity": record["severity"],
                "confidence_label": "requires_review",
                "evidence_refs": refs,
                "explanation": " ".join(
                    item["explanation"] for item in row["proposed"]["clause_assessments"].values()
                )
                or row["proposed"].get("reason")
                or "Unknown architectural scope; answer the predicate questions.",
                "normative_statement": record["statement"],
                "suggested_resolution": "Resolve the cited design gap or answer the applicability/branch question.",
                "acceptance_criteria": [
                    record["verification"]["specification"]["outcome_conditions"][
                        "satisfied_by_spec"
                    ]
                ],
                "review_state": "unreviewed",
                "review_comment": "",
                "enforcement": row["proposed"]["enforcement"],
                "implementation_verification": "not_verified",
            }
            findings.append(finding)
        for item in self.state["P2"]["unresolved_operations"]:
            findings.append(
                {
                    "id": "finding-" + digest({"run": self.run_id, "ambiguity": item})[:24],
                    "type": "delta_ambiguity",
                    "requirement_ref": None,
                    "baseline_assessment": None,
                    "proposed_assessment": None,
                    "change_effect": "unresolved",
                    "severity": "unrated",
                    "confidence_label": "requires_review",
                    "evidence_refs": [],
                    "explanation": item["reason"],
                    "suggested_resolution": "Clarify add/replace/delete and the target baseline.",
                    "acceptance_criteria": [],
                    "review_state": "unreviewed",
                    "review_comment": "",
                    "implementation_verification": "not_verified",
                }
            )
        for finding in findings:
            requirement_ref = finding.get("requirement_ref")
            finding["project_ranking"] = next(
                (
                    ranking
                    for ranking in self.manifest.get("project_rankings", [])
                    if requirement_ref
                    and ranking["requirement_id"] == requirement_ref["id"]
                    and ranking["version"] == requirement_ref["version"]
                ),
                None,
            )
            with self.store.connection:
                self.store.connection.execute(
                    "INSERT INTO findings VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(project_id,id) DO NOTHING",
                    (
                        self.project_id,
                        finding["id"],
                        self.run_id,
                        None
                        if finding["requirement_ref"] is None
                        else finding["requirement_ref"]["id"],
                        finding["type"],
                        finding["severity"],
                        finding["change_effect"],
                        json.dumps(finding, ensure_ascii=False),
                    ),
                )
        return {
            "matrix": rows,
            "findings": findings,
            "questions": self.state["P2"]["questions"]
            + self.state["P3"]["questions"]
            + self.state["P5"]["questions"],
            "incomplete": any(
                row["proposed"]["incomplete"] or row["baseline"]["incomplete"] for row in rows
            ),
        }

    def P7(self):
        original = self.store.document(self.project_id, self.request.delta_document_id)[2]
        delta = {item["id"]: Block(**item) for item in self.state["P2"]["delta_blocks"]}
        findings = self.state["P6"]["findings"]
        finding_ids = {item["id"] for item in findings}
        records = {record["id"]: record for record in self.records}
        scope_rows = {
            row["requirement_id"]: row
            for row in self.state["P4"]["rows"]
            if row["side"] == "proposed"
        }
        work = [
            findings[start : start + self.request.limits.batch_size]
            for start in range(0, len(findings), self.request.limits.batch_size)
        ]
        profile = self.manifest["connections"]["P7"]["content"]
        context_limit = max(
            256, min(16000, (profile["context_budget"] - profile["max_output_tokens"]) // 8)
        )
        index = ContextIndex(list(delta.values()))
        merged: list[ProposedPatch] = []
        questions: list[str] = []
        incomplete = False
        try:
            while work:
                batch = work.pop(0)
                batch_ids = {item["id"] for item in batch}
                # Full assessments remain in P6 artifacts; generation needs exact
                # findings, force, statements, criteria and anchors, without two
                # redundant matrices in every payload.
                compact = [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"baseline_assessment", "proposed_assessment"}
                    }
                    for item in batch
                ]
                policy_context = []
                for finding in batch:
                    reference = finding["requirement_ref"]
                    if reference is None:
                        continue
                    record = records[reference["id"]]
                    scope = scope_rows[record["id"]]
                    active = (
                        set(scope["active_clauses"]) if scope["applicability"] is True else set()
                    )
                    uncertain = set(scope["uncertain_clauses"])
                    if scope["applicability"] is None:
                        uncertain.update(scope["active_clauses"])
                    included = active | uncertain
                    policy_context.append(
                        {
                            "id": record["id"],
                            "version": record["version"],
                            "applicability": scope["applicability"],
                            "facts": scope["facts"],
                            "active_clause_ids": sorted(active),
                            "uncertain_clause_ids": sorted(uncertain),
                            "clause_logic": record["clause_logic"],
                            "clauses": [
                                clause for clause in record["clauses"] if clause["id"] in included
                            ],
                            "clause_normativity": {
                                cid: record["clause_normativity"][cid] for cid in sorted(included)
                            },
                            "verification_plan": verification_input(record, scope["facts"], active),
                            "instruction": "Preserve typed ALL/ANY/WHEN and per-clause force/nature. Only active clauses can demand a control; uncertain scope/branches require a question. Context is excluded. Advice remains advice, and review/process duties remain duties. Summary text is not authority to change clause force.",
                        }
                    )
                query = " ".join(
                    item.get("normative_statement", "") + " " + item["explanation"]
                    for item in batch
                )
                selected, trace = index.retrieve(query, allowed=set(delta), max_bytes=context_limit)
                self.artifact(
                    "retrieval.trace", {"stage": "P7", "finding_ids": sorted(batch_ids), **trace}
                )
                allowed = {item.id: item for item in selected}
                data = {
                    "findings": compact,
                    "policy_context": policy_context,
                    "original_delta": original.decode() if len(original) <= context_limit else None,
                    "original_sha256": sha256(original),
                    "delta_blocks": [asdict(item) for item in selected],
                    "context_scope": {
                        "selected_block_count": len(selected),
                        "total_block_count": len(delta),
                        "full_original_included": len(original) <= context_limit,
                        "instruction": "Only supplied blocks may be replaced/deleted. Omitted original blocks remain unchanged. Ask a question if the needed target is unavailable.",
                    },
                }

                def validate(output, allowed=allowed, batch_ids=batch_ids):
                    self.materialize_patches(output, original, allowed, batch_ids)

                try:
                    output = self.call("P7", data, PatchesOutput, validate)
                except ContextExceeded:
                    if len(batch) > 1:
                        middle = len(batch) // 2
                        work[0:0] = [batch[:middle], batch[middle:]]
                        continue
                    questions.append(
                        "Generation context exceeded for "
                        + batch[0]["id"]
                        + "; its finding remains unresolved. Increase the explicitly configured context budget or reduce the selected scope."
                    )
                    incomplete = True
                    continue
                questions.extend(output.questions)
                for patch in output.patches:
                    # IDs are model data; assign a stable run-local identity after
                    # validation so independent batches cannot collide.
                    normalized = patch.model_copy(
                        update={
                            "id": "patch-"
                            + digest({"run": self.run_id, "patch": patch.model_dump()})[:24]
                        }
                    )
                    duplicate = next(
                        (
                            value
                            for value in merged
                            if (
                                value.operation,
                                value.target_block_id,
                                value.target_hash,
                                value.original,
                                value.replacement,
                            )
                            == (
                                normalized.operation,
                                normalized.target_block_id,
                                normalized.target_hash,
                                normalized.original,
                                normalized.replacement,
                            )
                        ),
                        None,
                    )
                    if duplicate:
                        combined = sorted(set(duplicate.finding_ids + normalized.finding_ids))
                        if len(combined) > 50:
                            self.artifact(
                                "proposal.merge_conflict",
                                normalized.model_dump(),
                                {
                                    "reason": "merged_finding_reference_limit",
                                    "excluded_from_candidate": True,
                                },
                            )
                            questions.append(
                                "Proposal reference limit exceeded for "
                                + normalized.id
                                + "; additional finding links require manual review."
                            )
                            incomplete = True
                        else:
                            duplicate.finding_ids = combined
                        continue
                    try:
                        self.materialize_patches(
                            PatchesOutput(patches=merged + [normalized], questions=[]),
                            original,
                            delta,
                            finding_ids,
                        )
                    except ValueError:
                        self.artifact(
                            "proposal.merge_conflict",
                            normalized.model_dump(),
                            {
                                "finding_ids": normalized.finding_ids,
                                "excluded_from_candidate": True,
                            },
                        )
                        questions.append(
                            "Conflicting proposal "
                            + normalized.id
                            + " was retained in proposal.merge_conflict and excluded from the candidate; findings "
                            + ", ".join(normalized.finding_ids)
                            + " require manual resolution."
                        )
                        incomplete = True
                    else:
                        merged.append(normalized)
        finally:
            index.close()
        output = PatchesOutput(patches=merged, questions=[])
        patches, proposed = self.materialize_patches(output, original, delta, finding_ids)
        candidate = self.artifact("delta.proposed.md", proposed)
        return {
            "patches": patches,
            "questions": list(dict.fromkeys(questions)),
            "candidate_artifact": candidate,
            "original_sha256": sha256(original),
            "document_status": "draft_requires_review",
            "incomplete": incomplete,
        }

    def materialize_patches(self, output, original, delta, finding_ids):
        result = []
        generated = []
        for item in output.patches:
            if set(item.finding_ids) - finding_ids:
                raise ValueError("unknown_patch_finding")
            if item.target_block_id is None:
                if (
                    item.operation != "insert"
                    or item.target_hash != sha256(original)
                    or item.original
                ):
                    raise ValueError("invalid_eof_patch")
                start = end = len(original.decode())
            else:
                block = delta.get(item.target_block_id)
                if block is None or item.target_hash != block.hash:
                    raise ValueError("patch_target_hash_conflict")
                if item.operation == "insert":
                    if item.original:
                        raise ValueError("insert_original_must_be_empty")
                    start = end = len(original[: block.byte_end].decode())
                else:
                    if item.original != block.text or (
                        item.operation == "delete" and item.replacement
                    ):
                        raise ValueError("patch_original_conflict")
                    start, end = (
                        len(original[: block.byte_start].decode()),
                        len(original[: block.byte_end].decode()),
                    )
            result.append(
                item.model_dump() | {"start": start, "end": end, "review_state": "unreviewed"}
            )
            generated.append(Patch(item.id, start, end, item.original, item.replacement))
        return result, apply(original, sha256(original), generated, {item.id for item in generated})

    def P8(self):
        candidate = self.repo.artifact(
            self.project_id, self.run_id, self.state["P7"]["candidate_artifact"]["id"], body=True
        )["body"]
        proposed_blocks = blocks(self.state["P7"]["candidate_artifact"]["id"], candidate)
        affected = {
            item["requirement_ref"]["id"]
            for item in self.state["P6"]["findings"]
            if item["requirement_ref"]
        }
        work = [record for record in self.records if record["id"] in affected]
        if not work or any(not item["requirement_ref"] for item in self.state["P6"]["findings"]):
            work.append(None)
        inherited = {
            item["id"]: Block(**item)
            for item in self.state["P2"]["proposed_blocks"]
            if item["snapshot_id"] != self.request.delta_document_id
        }
        index = ContextIndex(proposed_blocks + list(inherited.values()))
        issues: list[dict] = []
        reassessment: list[dict] = []
        questions: list[str] = []
        unchecked_patches: set[str] = set()
        try:
            for record in work:
                scoped_findings = [
                    item
                    for item in self.state["P6"]["findings"]
                    if (record is None and not item["requirement_ref"])
                    or (
                        record is not None
                        and item["requirement_ref"]
                        and item["requirement_ref"]["id"] == record["id"]
                    )
                ]
                finding_ids = {item["id"] for item in scoped_findings}
                scoped_patches = [
                    item
                    for item in self.state["P7"]["patches"]
                    if finding_ids.intersection(item["finding_ids"])
                ]
                known_requirements = set() if record is None else {record["id"]}
                query = (
                    record["statement"]
                    if record
                    else " ".join(block.text for block in proposed_blocks[:4])
                )
                limit = min(
                    12000, self.manifest["connections"]["P8"]["content"]["context_budget"] // 3
                )
                selected, log = index.retrieve(query, allowed=set(index.blocks), max_bytes=limit)
                self.artifact("retrieval.trace", {"stage": "P8", **log})
                allowed = {item.id: item for item in selected}
                scope = [] if record is None else [record]
                active = (
                    set()
                    if record is None
                    else active_clauses(record, self.facts_for(record, "proposed"))[0]
                )
                if (
                    record is not None
                    and condition(
                        record["applicability"]["expression"], self.facts_for(record, "proposed")
                    )
                    is not True
                ):
                    active = set()
                keys = (
                    []
                    if record is None
                    else [(record["id"], "proposed", cid) for cid in sorted(active)]
                )
                batches = [
                    keys[start : start + self.request.limits.batch_size * 2]
                    for start in range(0, len(keys), self.request.limits.batch_size * 2)
                ] or [[]]
                patch_groups: list[list[dict]] = [[]]
                patch_bytes = 0
                for patch in scoped_patches:
                    size = len(canonical(patch))
                    if patch_groups[-1] and (
                        patch_bytes + size > limit
                        or len(patch_groups[-1]) >= self.request.limits.batch_size
                    ):
                        patch_groups.append([])
                        patch_bytes = 0
                    patch_groups[-1].append(patch)
                    patch_bytes += size
                tasks = [(batch, patch_groups[0]) for batch in batches] + [
                    ([], group) for group in patch_groups[1:]
                ]
                while tasks:
                    batch, patch_group = tasks.pop(0)
                    known_patches = {item["id"] for item in patch_group}
                    expected: set[tuple[str, str, str]] = set()

                    def validate(
                        output,
                        scope=scope,
                        expected=expected,
                        allowed=allowed,
                        known_patches=known_patches,
                        known_requirements=known_requirements,
                    ):
                        for issue in output.issues:
                            if (
                                issue.patch_id is not None
                                and issue.patch_id not in known_patches
                                or issue.requirement_id is not None
                                and issue.requirement_id not in known_requirements
                            ):
                                raise ValueError("critique_invented_reference")
                        validate_judgments(
                            JudgmentsOutput(
                                judgments=output.reassessment,
                                retrieval_queries=[],
                                questions=output.questions,
                            ),
                            scope,
                            expected,
                            {},
                            allowed,
                        )

                    data = {
                        "candidate_blocks": [asdict(item) for item in selected],
                        "patches": patch_group,
                        "context_scope": {
                            "candidate_sha256": sha256(candidate),
                            "selected_blocks": len(selected),
                            "total_blocks": len(index.blocks),
                            "only_scoped_findings_and_patches": True,
                        },
                        "findings": [
                            {
                                key: value
                                for key, value in item.items()
                                if key not in {"baseline_assessment", "proposed_assessment"}
                            }
                            for item in scoped_findings
                        ],
                        "requested_reassessment": [],
                        "catalog": [
                            {
                                "id": item["id"],
                                "version": item["version"],
                                "clauses": [
                                    clause for clause in item["clauses"] if clause["id"] in active
                                ],
                                "clause_logic": item["clause_logic"],
                                "clause_normativity": {
                                    cid: item["clause_normativity"][cid] for cid in sorted(active)
                                },
                                "active_clause_ids": sorted(active),
                                "facts": self.facts_for(item, "proposed"),
                                "verification_plan": verification_input(
                                    item, self.facts_for(item, "proposed"), active
                                ),
                                "scope_instruction": "Only these active clauses may require evidence; omitted context/inactive branches are not obligations. Reassess exactly requested_reassessment, preserving typed alternatives and force/nature.",
                            }
                            for item in scope
                        ],
                    }
                    try:
                        output = self.call("P8", data, CritiqueOutput, validate)
                        output = self.reassess_candidate(
                            record,
                            data,
                            output,
                            set(batch),
                            allowed,
                        )
                    except ContextExceeded:
                        if len(batch) > 1:
                            middle = len(batch) // 2
                            tasks[0:0] = [
                                (batch[:middle], patch_group),
                                (batch[middle:], patch_group),
                            ]
                            continue
                        if len(patch_group) > 1:
                            middle = len(patch_group) // 2
                            tasks[0:0] = [(batch, patch_group[:middle]), ([], patch_group[middle:])]
                            continue
                        unchecked_patches.update(known_patches)
                        message = "P8 context exceeded: selected proposal/requirement could not be independently reviewed; increase the explicit context budget or reduce scope."
                        questions.append(message)
                        issues.append(
                            {
                                "type": "remaining_gap",
                                "patch_id": next(iter(known_patches), None),
                                "requirement_id": None if record is None else record["id"],
                                "explanation": message,
                            }
                        )
                        for rid, side, cid in batch:
                            reassessment.append(
                                {
                                    "requirement_id": rid,
                                    "version": record["version"],
                                    "clause_id": cid,
                                    "side": side,
                                    "assessment": "unknown",
                                    "evidence_state": "insufficient",
                                    "evidence": [],
                                    "explanation": message,
                                    "question": message,
                                }
                            )
                        continue
                    issues.extend(item.model_dump() for item in output.issues)
                    reassessment.extend(item.model_dump() for item in output.reassessment)
                    questions.extend(output.questions)
        finally:
            index.close()
        candidate_assessments = []
        for record in work:
            if record is None:
                continue
            judgments = {
                item["clause_id"]: item
                for item in reassessment
                if item["requirement_id"] == record["id"]
            }
            original = next(
                row for row in self.state["P6"]["matrix"] if row["requirement_id"] == record["id"]
            )
            row = matrix_row(
                record,
                self.facts_for(record, "proposed"),
                judgments,
                adoption=original["proposed"]["adoption"],
            )
            candidate_assessments.append(
                {
                    "requirement_id": record["id"],
                    "version": record["version"],
                    **row,
                }
            )
            if row["assessment"] not in {"satisfied_by_spec", "not_assessed"}:
                issues.append(
                    {
                        "type": "remaining_gap",
                        "patch_id": None,
                        "requirement_id": record["id"],
                        "explanation": "Candidate documentary assessment remains "
                        + row["assessment"]
                        + " under the original typed clause logic; see candidate_assessments and clause evidence.",
                    }
                )
        for patch in self.state["P7"]["patches"]:
            if implementation_claim(patch["replacement"]):
                issues.append(
                    {
                        "type": "unverifiable_claim",
                        "patch_id": patch["id"],
                        "requirement_id": None,
                        "explanation": "Generated implementation claim has no runtime verification.",
                    }
                )
        report = {
            "matrix": self.state["P6"]["matrix"],
            "findings": self.state["P6"]["findings"],
            "patches": self.state["P7"]["patches"],
            "critique": list({digest(issue): issue for issue in issues}.values()),
            "unchecked_patch_ids": sorted(unchecked_patches),
            "reassessment_of_proposal_only": reassessment,
            "candidate_assessments": candidate_assessments,
            "questions": list(
                dict.fromkeys(
                    self.state["P6"]["questions"] + self.state["P7"]["questions"] + questions
                )
            ),
            "implementation_verification": "not_verified",
            "original_sha256": self.state["P7"]["original_sha256"],
            "coverage": {
                "active_requirements": len(self.records),
                "rows": len(self.state["P6"]["matrix"]),
            },
            "limits": self.manifest["limitations"],
        }
        self.artifact("review.json", report)
        return report | {"incomplete": bool(issues)}
