"""Public run request, explicit project inputs and budgets."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .stage_contracts import Contract, DeltaOperation

ModelStage = Literal["P2", "P3", "P5", "P7", "P8"]


class VersionRef(Contract):
    id: str
    revision: int = Field(ge=1)


class RunLimits(Contract):
    max_requests: int = Field(default=200, ge=1, le=10000)
    max_tokens: int = Field(default=256000, ge=1024, le=100000000)
    wall_seconds: int = Field(default=1800, ge=5, le=86400)
    stage_max_requests: int = Field(default=100, ge=1, le=10000)
    batch_size: int = Field(default=4, ge=1, le=20)


class FactOverride(Contract):
    requirement_id: str
    predicate_id: str
    side: Literal["baseline", "proposed"]
    value: bool | None
    reason: str = Field(min_length=1, max_length=2000)


class AdoptionOverride(Contract):
    requirement_id: str
    mode: Literal["required", "advisory", "waived", "undecided"]
    reason: str = Field(min_length=1, max_length=2000)


class RunInput(Contract):
    mode: Literal["enrich_changes", "review_documents"] = "enrich_changes"
    review_document_ids: list[str] = Field(default_factory=list, max_length=1000)
    delta_document_id: str | None = None
    baseline_document_ids: list[str] = Field(default_factory=list, max_length=1000)
    context_document_ids: list[str] = Field(default_factory=list, max_length=1000)
    catalog_ids: list[str] = Field(min_length=1, max_length=100)
    connection: VersionRef
    pipeline_ref: VersionRef | None = None
    stage_models: dict[ModelStage, VersionRef] = Field(default_factory=dict)
    prompt_refs: dict[ModelStage, VersionRef] = Field(default_factory=dict)
    skill_refs: list[VersionRef] = Field(default_factory=list, max_length=50)
    source_refs: list[VersionRef] = Field(default_factory=list, max_length=100)
    limits: RunLimits = Field(default_factory=RunLimits)
    fact_overrides: list[FactOverride] = Field(default_factory=list, max_length=1000)
    adoption_overrides: list[AdoptionOverride] = Field(default_factory=list, max_length=1000)
    operation_overrides: list[DeltaOperation] = Field(default_factory=list, max_length=500)
    step_mode: bool = False

    @model_validator(mode="after")
    def input_mode(self):
        if self.mode == "enrich_changes":
            if not self.delta_document_id or not self.baseline_document_ids or self.review_document_ids:
                raise ValueError("enrichment_requires_delta_and_baseline")
        else:
            if not self.review_document_ids or len(set(self.review_document_ids)) != len(self.review_document_ids):
                raise ValueError("review_requires_unique_documents")
            if self.delta_document_id or self.baseline_document_ids or self.context_document_ids or self.operation_overrides:
                raise ValueError("review_does_not_accept_change_operations")
            if self.pipeline_ref or self.prompt_refs or self.skill_refs:
                raise ValueError("review_custom_pipeline_prompts_skills_not_supported")
            if set(self.stage_models) - {"P3", "P5"}:
                raise ValueError("review_only_uses_P3_P5_models")
            if any(item.side != "proposed" for item in self.fact_overrides):
                raise ValueError("review_fact_override_requires_checked_scope")
        return self


def matches_request_checksum(checksum, request):
    """New mode defaults must not invalidate an unchanged legacy idempotent retry."""
    from .catalog import digest
    if digest(request) == checksum:
        return True
    if request.get("mode", "enrich_changes") == "enrich_changes" and not request.get("review_document_ids"):
        legacy = {key: value for key, value in request.items()
                  if key not in {"mode", "review_document_ids"}}
        return digest(legacy) == checksum
    return False
