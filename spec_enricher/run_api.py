"""Run preflight, asynchronous local worker, progress and review API."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import Field

from .artifacts import ArtifactStore
from .call_control import approve as approve_call
from .catalog import canonical, digest
from .comparison import compare_runs
from .connections import ConnectionProfile
from .effective_state import blocks
from .exporting import ExportInput, ProposalPreviewInput, export_bundle, preview_proposal
from .i18n import translate
from .llm import LLMAdapter
from .pipeline import PipelineRunner
from .pipeline_config import rendered
from .preflight import current_algorithm_hashes, prepare
from .retrieval import ContextIndex
from .review_reporting import ReportExportInput, export_report, review_report
from .run_contracts import RunInput
from .runs import RunConflict, RunRepository
from .security import error
from .stage_contracts import Contract, OperationsOutput, bind_schema
from .storage import Store


class RunStart(Contract):
    request: RunInput
    expected_plan_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=100)


class DecisionInput(Contract):
    expected_revision: int = Field(ge=0)
    target_kind: Literal["finding", "patch"]
    target_id: str = Field(min_length=1, max_length=200)
    decision: Literal["accepted", "rejected", "needs_clarification", "deferred", "unreviewed"]
    comment: str = Field(max_length=4000)


class ResumeInput(Contract):
    expected_checkpoint_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    confirmed_call: bool = False
    call_preview_id: str | None = None
    expected_call_preview_sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")


def install(app, db, project_exists, data_dir, secrets, adapter_factory=None):
    artifacts = ArtifactStore(data_dir / "artifacts")
    database = data_dir / "app.sqlite3"
    threads: dict[str, threading.Thread] = {}
    lock = threading.Lock()
    with_store = Store(database)
    RunRepository(with_store, artifacts).interrupt_abandoned()
    with_store.close()
    app.state.run_threads = threads
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application):
            try:
                yield
            finally:
                store = Store(database)
                with store.connection:
                    store.connection.execute(
                        "UPDATE runs SET cancel_requested=1 WHERE status IN ('queued','running')"
                    )
                store.close()
                for thread in list(threads.values()):
                    await asyncio.to_thread(thread.join, 0.2)

    app.router.lifespan_context = lifespan

    def repository(store):
        return RunRepository(store, artifacts)

    def checked(project_id, run_id, store):
        repo = repository(store)
        run = repo.get(project_id, run_id)
        if run is None:
            raise HTTPException(404, translate("Run не найден в выбранном проекте."))
        return repo, run

    def launch(project_id, run_id, *, resume=False):
        def work():
            try:
                kwargs = {} if adapter_factory is None else {"adapter_factory": adapter_factory}
                PipelineRunner(
                    database, data_dir / "artifacts", secrets, project_id, run_id, **kwargs
                ).execute(resume=resume)
            except Exception:  # noqa: BLE001 — terminal worker boundary
                recovery = Store(database)
                try:
                    RunRepository(recovery, artifacts).set_status(
                        project_id, run_id, "failed", "worker_initialization_or_unexpected_error"
                    )
                finally:
                    recovery.close()
            finally:
                with lock:
                    threads.pop(run_id, None)

        thread = threading.Thread(target=work, name="run-" + run_id, daemon=True)
        with lock:
            threads[run_id] = thread
        thread.start()

    def frozen(project_id, body, store):
        manifest, records = prepare(store, project_id, body)
        try:
            for record in manifest["connections"].values():
                LLMAdapter(
                    project_id, ConnectionProfile.model_validate(record["content"]), secrets
                ).headers()
        except (ValueError, OSError):
            raise ValueError("configured_credential_unresolved") from None
        secrets.reject_known(canonical(manifest))
        manifest["execution_mode"] = (
            "real_adapter" if adapter_factory is None else "synthetic_test_adapter"
        )
        return manifest, records

    def first_payload(manifest, store):
        request = RunInput.model_validate(manifest["request"])
        if request.mode == "review_documents":
            # Exact payload is prepared after deterministic snapshots at P3.
            return None
        delta = blocks(
            request.delta_document_id,
            store.document(manifest["project_id"], request.delta_document_id)[2],
        )
        baseline = []
        for document_id in request.baseline_document_ids:
            baseline.extend(
                blocks(document_id, store.document(manifest["project_id"], document_id)[2])
            )
        if not delta:
            return None
        profile = ConnectionProfile.model_validate(manifest["connections"]["P2"]["content"])
        index = ContextIndex(baseline)
        try:
            from dataclasses import asdict

            selected, _ = index.retrieve(
                delta[0].text,
                allowed=set(index.blocks),
                max_bytes=min(10000, profile.context_budget // 3),
            )
            data: dict = {
                "delta_blocks": [asdict(delta[0])],
                "baseline_blocks": [asdict(item) for item in selected],
            }
            schema = bind_schema("P2", OperationsOutput.model_json_schema(), data)
            prompt = manifest["prompts"]["P2"]
            skills = [skill for skill in manifest.get("skills", []) if skill["stage"] == "P2"]
            first_skill = next((skill for skill in skills if skill["hook"] == "before"), None)
            replacement = next((skill for skill in skills if skill["hook"] == "replace"), None)
            if first_skill:
                data = {"stage_input": data, "prior_skill_outputs": []}
                schema, prompt = first_skill["output_schema"], first_skill["prompt"]
            elif replacement:
                prompt = replacement["prompt"]
            skill = first_skill or replacement
            if skill:
                profile = profile.model_copy(
                    update={
                        "timeout": min(profile.timeout, skill["timeout"]),
                        "max_output_tokens": min(
                            profile.max_output_tokens, skill["budget"]["max_output_tokens"]
                        ),
                    }
                )
            messages = [
                {
                    "role": "system",
                    "content": manifest["application_policy"]
                    + "\n\n"
                    + rendered(prompt, "P2", schema),
                },
                {
                    "role": "user",
                    "content": canonical({"trust": "untrusted_data_only", "data": data}).decode(),
                },
            ]
            return LLMAdapter(manifest["project_id"], profile, secrets).payload(messages, schema)
        finally:
            index.close()

    @app.post("/api/v1/projects/{project_id}/runs/preflight")
    def preflight(project_id: str, body: RunInput, store: Store = Depends(db)):
        project_exists(project_id, store)
        try:
            manifest, records = frozen(project_id, body, store)
        except ValueError as exc:
            return error(
                422,
                "preflight_blocked",
                translate(
                    "Запуск заблокирован: проверьте выбранные snapshots, активные каталоги/dependency closure, model capability и destination/data scopes."
                ),
                [{"reason": str(exc)}],
            )
        return {
            "plan_sha256": digest(manifest),
            "destinations": manifest["destinations"],
            "documents": manifest["documents"],
            "catalogs": manifest["catalogs"],
            "active_requirements": len(records),
            "limits": manifest["limits"],
            "first_payload_preview": first_payload(manifest, store),
            "future_payloads": translate(
                "Последующие inputs формируются из retrieval и предыдущих outputs. Реальные payloads сохраняются в run inspector."
            ),
            "execution_mode": manifest["execution_mode"],
            "hook_order": manifest["hook_order"],
            "prompts": manifest["prompts"],
            "positive_review_prompt": manifest.get("positive_review_prompt"),
            "candidate_reassessment_prompt": manifest["candidate_reassessment_prompt"],
            "architectural_review_prompt": manifest["architectural_review_prompt"],
            "skills": manifest["skills"],
        }

    @app.post("/api/v1/projects/{project_id}/runs", status_code=202)
    def start(project_id: str, body: RunStart, store: Store = Depends(db)):
        project_exists(project_id, store)
        repo = repository(store)
        existing = store.connection.execute(
            "SELECT id,request_sha256 FROM runs WHERE project_id=? AND request_key=?",
            (project_id, body.idempotency_key),
        ).fetchone()
        if existing:
            from .run_contracts import matches_request_checksum
            if not matches_request_checksum(existing[1], body.request.model_dump()):
                return error(
                    409,
                    "idempotency_conflict",
                    translate("Этот idempotency key относится к другому запросу."),
                )
            return {"id": existing[0], "replayed": True}
        try:
            manifest, _ = frozen(project_id, body.request, store)
            if digest(manifest) != body.expected_plan_sha256:
                return error(
                    409,
                    "plan_changed",
                    translate(
                        "Preflight plan изменился; просмотрите актуальные destinations и inputs."
                    ),
                )
            run_id, created = repo.create(
                project_id, body.idempotency_key, body.request.model_dump(), manifest
            )
        except RunConflict:
            return error(
                409,
                "active_run_or_scope_conflict",
                translate("Уже выполняется другой run, либо ссылка выходит за project scope."),
            )
        except ValueError:
            return error(
                422,
                "preflight_blocked",
                translate("Конфигурация запуска не соответствует preflight-контракту."),
            )

        if created:
            launch(project_id, run_id)
        return {"id": run_id, "replayed": not created}

    @app.get("/api/v1/projects/{project_id}/runs")
    def history(project_id: str, store: Store = Depends(db)):
        project_exists(project_id, store)
        return repository(store).list_runs(project_id)

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}")
    def run(project_id: str, run_id: str, store: Store = Depends(db)):
        repo, value = checked(project_id, run_id, store)
        value.pop("manifest")
        counts = store.connection.execute(
            "SELECT change_effect,count(*) FROM requirement_results WHERE project_id=? AND run_id=? GROUP BY change_effect",
            (project_id, run_id),
        ).fetchall()
        value.update(change_effect_counts=dict(counts), stages=repo.stages(project_id, run_id))
        paired = store.connection.execute(
            "SELECT baseline,proposed FROM requirement_results WHERE project_id=? AND run_id=?",
            (project_id, run_id),
        ).fetchall()
        summary = {}
        for position, side in enumerate(("baseline", "proposed")):
            assessments: dict[str, int] = {}
            applicability: dict[str, int] = {}
            for row in paired:
                item = json.loads(row[position])
                for key, counters in [
                    ("assessment", assessments),
                    ("applicability", applicability),
                ]:
                    label = item.get(key, "unknown")
                    counters[label] = counters.get(label, 0) + 1
            summary[side] = {"assessment": assessments, "applicability": applicability}
        value["documentary_summary"] = {
            "rows": len(paired),
            "sides": summary,
            "notice": "Matrix coverage includes unknown/incomplete assessments. No runtime verification or security percentage.",
        }
        value["checkpoint"] = repo.checkpoint(project_id, run_id)
        return value

    @app.post("/api/v1/projects/{project_id}/runs/{run_id}/resume", status_code=202)
    def resume(project_id: str, run_id: str, body: ResumeInput, store: Store = Depends(db)):
        repo, value = checked(project_id, run_id, store)
        checkpoint = repo.checkpoint(project_id, run_id)
        if value["manifest"].get("algorithm_hashes") != current_algorithm_hashes():
            return error(
                409,
                "runtime_methodology_changed",
                translate(
                    "Код, dependency lock или schema изменились. История сохранена; создайте новый run на текущей версии вместо смешения методик."
                ),
            )
        if (
            value["status"] not in {"waiting_approval", "interrupted"}
            or checkpoint["sha256"] != body.expected_checkpoint_sha256
        ):
            return error(
                409,
                "checkpoint_conflict",
                translate("Run/checkpoint изменился; обновите состояние перед продолжением."),
            )
        pending = checkpoint.get("pending_call")
        if pending and (
            not body.confirmed_call
            or body.call_preview_id != pending["artifact_id"]
            or body.expected_call_preview_sha256 != pending["sha256"]
        ):
            return error(
                409,
                "exact_payload_confirmation_required",
                translate(
                    "Просмотрите точный payload и подтвердите его hash перед этим внешним вызовом."
                ),
            )
        with lock:
            if run_id in threads:
                return error(
                    409, "worker_still_active", translate("Предыдущий worker ещё завершает работу.")
                )
            try:
                with store.connection:
                    result = store.connection.execute(
                        "UPDATE runs SET status='queued',cancel_requested=0,finished_at=NULL,error_code=NULL WHERE project_id=? AND id=? AND status IN ('waiting_approval','interrupted')",
                        (project_id, run_id),
                    )
                    if result.rowcount != 1:
                        return error(
                            409,
                            "checkpoint_conflict",
                            translate("Run уже продолжен другим запросом."),
                        )
                    if pending:
                        approve_call(
                            repo,
                            project_id,
                            run_id,
                            body.call_preview_id,
                            body.expected_call_preview_sha256,
                        )
            except sqlite3.IntegrityError:
                return error(
                    409,
                    "active_run_conflict",
                    translate("Другой run уже активен; сначала завершите его."),
                )
        store.audit(project_id, "run_checkpoint_explicitly_resumed", run_id)
        launch(project_id, run_id, resume=True)
        return {"id": run_id, "next_stage": checkpoint["next_stage"]}

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/manifest")
    def manifest(project_id: str, run_id: str, store: Store = Depends(db)):
        return checked(project_id, run_id, store)[1]["manifest"]

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/compare/{other_run_id}")
    def compare(project_id: str, run_id: str, other_run_id: str, store: Store = Depends(db)):
        try:
            return compare_runs(repository(store), project_id, run_id, other_run_id)
        except LookupError:
            raise HTTPException(
                404, translate("Оба run должны принадлежать выбранному проекту.")
            ) from None

    @app.post("/api/v1/projects/{project_id}/runs/{run_id}/cancel")
    def cancel(project_id: str, run_id: str, store: Store = Depends(db)):
        repo, _ = checked(project_id, run_id, store)
        repo.cancel(project_id, run_id)
        if repo.get(project_id, run_id)["status"] == "cancelled":
            repo.fill_unfinished_results(project_id, run_id, "cancelled_before_assessment")
        return {
            "cancel_requested": True,
            "notice": translate(
                "Новые calls прекращаются кооперативно; уже отправленный запрос может завершиться и быть оплачен."
            ),
        }

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/artifacts")
    def artifact_list(project_id: str, run_id: str, store: Store = Depends(db)):
        repo, _ = checked(project_id, run_id, store)
        return repo.list_artifacts(project_id, run_id)

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(project_id: str, run_id: str, artifact_id: str, store: Store = Depends(db)):
        repo, _ = checked(project_id, run_id, store)
        value = repo.artifact(project_id, run_id, artifact_id, body=True)
        if value is None:
            raise HTTPException(404, translate("Artifact не найден в run/project scope."))
        store.audit(project_id, "run_artifact_read", artifact_id)
        return Response(
            value["body"],
            media_type="text/plain; charset=utf-8",
            headers={"X-Content-SHA256": value["sha256"]},
        )

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/assessments")
    def assessments(
        project_id: str,
        run_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        applicability: str = "",
        assessment: str = "",
        store: Store = Depends(db),
    ):
        checked(project_id, run_id, store)
        rows = store.connection.execute(
            "SELECT requirement_id,requirement_version,baseline,proposed,change_effect FROM requirement_results WHERE project_id=? AND run_id=? AND (?='' OR json_extract(proposed,'$.applicability')=?) AND (?='' OR json_extract(proposed,'$.assessment')=?) ORDER BY requirement_id LIMIT ? OFFSET ?",
            (
                project_id,
                run_id,
                applicability,
                applicability,
                assessment,
                assessment,
                limit,
                offset,
            ),
        ).fetchall()
        return [
            {
                "requirement_id": row[0],
                "version": row[1],
                "baseline": json.loads(row[2]),
                "proposed": json.loads(row[3]),
                "change_effect": row[4],
            }
            for row in rows
        ]

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/questions")
    def questions(
        project_id: str,
        run_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(25, ge=1, le=100),
        store: Store = Depends(db),
    ):
        repo, run = checked(project_id, run_id, store)
        from .exporting import latest_output

        output = latest_output(repo, project_id, run_id, "stage.P3.output") or {
            "facts": [],
            "questions": [],
        }
        unresolved = [item for item in output["facts"] if item["value"] is None]
        facts = []
        for item in unresolved[offset : offset + limit]:
            cid = run["manifest"]["requirement_ownership"][item["requirement_id"]]
            record = store.requirement(project_id, cid, item["requirement_id"])
            predicate = next(
                value
                for value in record["applicability"]["predicates"]
                if value["id"] == item["predicate_id"]
            )
            facts.append(
                {
                    key: item[key]
                    for key in (
                        "requirement_id",
                        "predicate_id",
                        "side",
                        "value",
                        "question",
                        "explanation",
                    )
                }
                | {"predicate_text": predicate["text"]}
            )
        operation_output = latest_output(repo, project_id, run_id, "stage.P2.output") or {
            "unresolved_operations": [],
            "questions": [],
        }
        return {
            "facts": facts,
            "total_unknown_facts": len(unresolved),
            "operation_questions": operation_output["unresolved_operations"],
            "questions": list(dict.fromkeys(output["questions"] + operation_output["questions"]))[
                :100
            ],
        }

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/findings")
    def findings(
        project_id: str,
        run_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=100),
        type: str = "",
        severity: str = "",
        effect: str = "",
        review_state: str = "",
        store: Store = Depends(db),
    ):
        checked(project_id, run_id, store)
        rows = store.connection.execute(
            "SELECT f.id,f.content FROM findings f WHERE f.project_id=? AND f.run_id=? AND (?='' OR f.type=?) AND (?='' OR f.severity=?) AND (?='' OR f.change_effect=?) AND (?='' OR coalesce((SELECT d.decision FROM review_decisions d WHERE d.project_id=f.project_id AND d.run_id=f.run_id AND d.target_kind='finding' AND d.target_id=f.id ORDER BY d.revision DESC LIMIT 1),'unreviewed')=?) ORDER BY f.id LIMIT ? OFFSET ?",
            (
                project_id,
                run_id,
                type,
                type,
                severity,
                severity,
                effect,
                effect,
                review_state,
                review_state,
                limit,
                offset,
            ),
        ).fetchall()
        result = []
        for row in rows:
            value = json.loads(row[1])
            decision = store.connection.execute(
                "SELECT decision,comment,revision FROM review_decisions WHERE project_id=? AND run_id=? AND target_kind='finding' AND target_id=? ORDER BY revision DESC LIMIT 1",
                (project_id, run_id, row[0]),
            ).fetchone()
            if decision:
                value.update(
                    review_state=decision[0],
                    review_comment=decision[1],
                    review_revision=decision[2],
                )
            result.append(value)
        return result

    def proposal(repo, project_id, run_id):
        values = repo.list_artifacts(project_id, run_id, "stage.P7.output")
        if not values:
            raise HTTPException(404, translate("Предложения ещё не подготовлены."))
        return json.loads(repo.artifact(project_id, run_id, values[-1]["id"], body=True)["body"])

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/proposal")
    def proposals(project_id: str, run_id: str, store: Store = Depends(db)):
        repo, run = checked(project_id, run_id, store)
        value = proposal(repo, project_id, run_id)
        for patch in value["patches"]:
            decision = store.connection.execute(
                "SELECT decision,comment FROM review_decisions WHERE project_id=? AND run_id=? AND target_kind='patch' AND target_id=? ORDER BY revision DESC LIMIT 1",
                (project_id, run_id, patch["id"]),
            ).fetchone()
            if decision:
                patch.update(review_state=decision[0], review_comment=decision[1])
        return value | {"review_revision": run["review_revision"]}

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/critique")
    def critique(
        project_id: str,
        run_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(25, ge=1, le=100),
        store: Store = Depends(db),
    ):
        repo, _ = checked(project_id, run_id, store)
        values = repo.list_artifacts(project_id, run_id, "review.json")
        if not values:
            raise HTTPException(
                404,
                translate(
                    "Независимый контроль P8 ещё не завершён; смотрите status/errors стадии."
                ),
            )
        report = json.loads(repo.artifact(project_id, run_id, values[-1]["id"], body=True)["body"])
        return {
            "critique": report["critique"][offset : offset + limit],
            "critique_count": len(report["critique"]),
            "reassessment_of_proposal_only": report["reassessment_of_proposal_only"][
                offset : offset + limit
            ],
            "reassessment_count": len(report["reassessment_of_proposal_only"]),
            "candidate_assessments": [
                {
                    key: row[key]
                    for key in (
                        "requirement_id",
                        "version",
                        "applicability",
                        "assessment",
                        "enforcement",
                        "reason",
                        "implementation_verification",
                    )
                }
                for row in report.get("candidate_assessments", [])[offset : offset + limit]
            ],
            "candidate_assessment_count": len(report.get("candidate_assessments", [])),
            "unchecked_patch_ids": report.get("unchecked_patch_ids", []),
            "implementation_verification": "not_verified",
            "coverage": report["coverage"],
            "questions": report["questions"],
            "offset": offset,
        }

    @app.post("/api/v1/projects/{project_id}/runs/{run_id}/decisions")
    def decision(project_id: str, run_id: str, body: DecisionInput, store: Store = Depends(db)):
        repo, _ = checked(project_id, run_id, store)
        if body.target_kind == "finding":
            exists = store.connection.execute(
                "SELECT 1 FROM findings WHERE project_id=? AND run_id=? AND id=?",
                (project_id, run_id, body.target_id),
            ).fetchone()
        else:
            exists = any(
                item["id"] == body.target_id
                for item in proposal(repo, project_id, run_id)["patches"]
            )
        if not exists:
            raise HTTPException(404, translate("Объект review не найден в run/project scope."))
        with store.connection:
            store.connection.execute("BEGIN IMMEDIATE")
            revision = store.connection.execute(
                "SELECT review_revision FROM runs WHERE project_id=? AND id=?", (project_id, run_id)
            ).fetchone()[0]
            if revision != body.expected_revision:
                return error(
                    409,
                    "review_revision_conflict",
                    translate("Review изменился; обновите результаты перед сохранением решения."),
                )
            revision += 1
            store.connection.execute(
                "INSERT INTO review_decisions(project_id,run_id,revision,target_kind,target_id,decision,comment) VALUES(?,?,?,?,?,?,?)",
                (
                    project_id,
                    run_id,
                    revision,
                    body.target_kind,
                    body.target_id,
                    body.decision,
                    body.comment,
                ),
            )
            store.connection.execute(
                "UPDATE runs SET review_revision=? WHERE project_id=? AND id=?",
                (revision, project_id, run_id),
            )
        store.audit(project_id, "review_decision", body.target_id)
        return {"review_revision": revision, "assessment_unchanged": True}

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/report")
    def document_report(project_id: str, run_id: str, store: Store = Depends(db)):
        repo, _ = checked(project_id, run_id, store)
        try:
            report = review_report(repo, project_id, run_id)
            secrets.reject_known(canonical(report))
            return report
        except RunConflict as exc:
            return error(409, "report_unavailable", "Document report is not ready for this run.",
                         [{"reason": str(exc)}])

    @app.post("/api/v1/projects/{project_id}/runs/{run_id}/report/exports")
    def document_report_export(
        project_id: str, run_id: str, body: ReportExportInput, store: Store = Depends(db)
    ):
        repo, _ = checked(project_id, run_id, store)
        try:
            configured_root = os.environ.get("APP_EXPORT_DIR")
            destination = Path(configured_root) if configured_root else data_dir / "exports"
            return export_report(repo, project_id, run_id, body, destination,
                                 validate_content=secrets.reject_known)
        except RunConflict as exc:
            return error(409, "report_export_conflict", "Report or review revision changed; refresh the report.",
                         [{"reason": str(exc)}])

    @app.post("/api/v1/projects/{project_id}/runs/{run_id}/proposal/preview")
    def preview(
        project_id: str, run_id: str, body: ProposalPreviewInput, store: Store = Depends(db)
    ):
        repo, _ = checked(project_id, run_id, store)
        try:
            return preview_proposal(repo, project_id, run_id, body)
        except RunConflict:
            return error(
                409,
                "proposal_conflict",
                translate("Review или original hash изменились; обновите preview."),
            )

    @app.post("/api/v1/projects/{project_id}/runs/{run_id}/exports")
    def export(project_id: str, run_id: str, body: ExportInput, store: Store = Depends(db)):
        repo, _ = checked(project_id, run_id, store)
        try:
            configured_root = os.environ.get("APP_EXPORT_DIR")
            destination = Path(configured_root) if configured_root else data_dir / "exports"
            return export_bundle(
                repo, project_id, run_id, body, destination, validate_content=secrets.reject_known
            )
        except RunConflict as exc:
            return error(
                409,
                "export_conflict",
                translate(
                    "Hash, review revision или принятые решения изменились; обновите preview."
                ),
                [{"reason": str(exc)}],
            )

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/exports/{export_id}")
    def download_export(project_id: str, run_id: str, export_id: str, store: Store = Depends(db)):
        repo, _ = checked(project_id, run_id, store)
        row = store.connection.execute(
            "SELECT artifact_id,metadata FROM exports WHERE project_id=? AND run_id=? AND id=?",
            (project_id, run_id, export_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, translate("Export не найден в выбранном project/run scope."))
        metadata = json.loads(row[1])
        artifact = repo.artifact(project_id, run_id, row[0], body=True)
        return Response(
            artifact["body"],
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="' + metadata["filename"] + '"'},
        )

    @app.get("/api/v1/projects/{project_id}/runs/{run_id}/events")
    async def events(project_id: str, run_id: str, request: Request, store: Store = Depends(db)):
        checked(project_id, run_id, store)

        async def stream():
            previous = None
            sequence = 0
            heartbeat = 0
            while not await request.is_disconnected():
                local = Store(database)
                try:
                    repo = repository(local)
                    value = repo.get(project_id, run_id)
                    stages = repo.stages(project_id, run_id)
                finally:
                    local.close()
                if value is None:
                    break
                payload = {
                    "id": run_id,
                    "status": value["status"],
                    "stages": [
                        {
                            key: item[key]
                            for key in (
                                "stage_id",
                                "status",
                                "started_at",
                                "finished_at",
                                "warnings",
                                "errors",
                                "usage",
                            )
                        }
                        for item in stages
                    ],
                }
                encoded = json.dumps(payload, ensure_ascii=False)
                if encoded != previous:
                    sequence += 1
                    yield f"id: {sequence}\nevent: progress\ndata: {encoded}\n\n"
                    previous = encoded
                if value["status"] not in {"queued", "running", "waiting_approval"}:
                    break
                heartbeat += 1
                if heartbeat % 5 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )
