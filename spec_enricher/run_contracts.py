"""Public run request, explicit project inputs and budgets."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

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
    delta_document_id: str
    baseline_document_ids: list[str] = Field(min_length=1, max_length=1000)
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
