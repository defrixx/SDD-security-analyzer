"""Trusted declarative configuration; never executable extensions or permissions."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from .catalog import digest
from .connections import DataScope, safe_schema
from .prompts import POLICY, defaults
from .run_contracts import ModelStage, RunLimits, VersionRef
from .stage_contracts import (
    Contract,
    CritiqueOutput,
    FactsOutput,
    JudgmentsOutput,
    OperationsOutput,
    PatchesOutput,
)

CONTRACTS: dict[str, type[Contract]] = {
    "P2": OperationsOutput,
    "P3": FactsOutput,
    "P5": JudgmentsOutput,
    "P7": PatchesOutput,
    "P8": CritiqueOutput,
}


class PipelineDefinition(Contract):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    connection: VersionRef
    stage_models: dict[ModelStage, VersionRef] = Field(default_factory=dict)
    prompt_refs: dict[ModelStage, VersionRef] = Field(default_factory=dict)
    skill_refs: list[VersionRef] = Field(default_factory=list, max_length=50)
    source_refs: list[VersionRef] = Field(default_factory=list, max_length=100)
    limits: RunLimits = Field(default_factory=RunLimits)
    step_mode: bool = False


class PromptDefinition(Contract):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    stage: Literal["P2", "P3", "P5", "P7", "P8", "skill"]
    template: str = Field(min_length=1, max_length=32000)
    allowed_variables: list[Literal["stage", "output_contract"]] = Field(default_factory=list)
    output_schema: dict
    status: Literal["draft", "published"] = "draft"
    changelog: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def valid(self):
        safe_schema(self.output_schema)
        names = re.findall(r"\{\{\s*([A-Za-z_][A-Za-z_0-9]*)\s*\}\}", self.template)
        if set(names) - set(self.allowed_variables):
            raise ValueError("unknown_template_variable")
        remainder = re.sub(r"\{\{\s*[A-Za-z_][A-Za-z_0-9]*\s*\}\}", "", self.template)
        if "{{" in remainder or "}}" in remainder:
            raise ValueError("invalid_template_variable")
        if self.stage != "skill" and digest(self.output_schema) != digest(
            CONTRACTS[self.stage].model_json_schema()
        ):
            raise ValueError("mandatory_stage_output_contract")
        return self


class SkillBudget(Contract):
    max_requests: int = Field(default=20, ge=1, le=1000)
    max_output_tokens: int = Field(default=2048, ge=32, le=32768)


class SkillDefinition(Contract):
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    kind: Literal["prompt"] = "prompt"
    stage: ModelStage
    hook: Literal["before", "replace", "after"]
    order: int = Field(default=0, ge=-1000, le=1000)
    input_schema: dict
    output_schema: dict
    prompt_ref: VersionRef
    allowed_tools: list[str] = Field(default_factory=list, max_length=0)
    data_scopes: list[DataScope]
    timeout: int = Field(default=30, ge=1, le=300)
    budget: SkillBudget = Field(default_factory=SkillBudget)
    affects_assessment: bool = True
    failure_policy: Literal["stop", "continue_with_warning"] = "stop"
    enabled: bool = False

    @model_validator(mode="after")
    def valid(self):
        safe_schema(self.input_schema)
        safe_schema(self.output_schema)
        if self.affects_assessment and self.failure_policy != "stop":
            raise ValueError("assessment_skill_must_stop_on_failure")
        if self.hook == "replace" and digest(self.output_schema) != digest(
            CONTRACTS[self.stage].model_json_schema()
        ):
            raise ValueError("replace_skill_requires_stage_contract")
        if self.hook == "replace" and not self.affects_assessment:
            raise ValueError("replace_affects_assessment")
        return self


def rendered(prompt: dict, stage: str, schema: dict) -> str:
    from .catalog import canonical

    values = {"stage": stage, "output_contract": canonical(schema).decode()}
    return re.sub(
        r"\{\{\s*([A-Za-z_][A-Za-z_0-9]*)\s*\}\}", lambda m: values[m[1]], prompt["template"]
    )


def freeze_configuration(registry, project_id, request):
    prompts = defaults()
    refs = []
    for stage, ref in request.prompt_refs.items():
        record = registry.get(project_id, "prompt", ref.id, ref.revision)
        if record is None:
            raise ValueError("prompt_outside_project")
        prompt = PromptDefinition.model_validate(record["content"])
        if prompt.stage != stage or prompt.status != "published":
            raise ValueError("published_stage_prompt_required")
        prompts[stage] = record["content"] | {
            "id": ref.id,
            "revision": ref.revision,
            "hash": record["sha256"],
        }
        refs.append({"kind": "prompt", **ref.model_dump()})
    skills, replace_stages, seen = [], set(), set()
    for ref in request.skill_refs:
        if ref.id in seen:
            raise ValueError("duplicate_skill")
        seen.add(ref.id)
        record = registry.get(project_id, "skill", ref.id, ref.revision)
        if record is None:
            raise ValueError("skill_outside_project")
        skill = SkillDefinition.model_validate(record["content"])
        if not skill.enabled:
            raise ValueError("disabled_skill")
        prompt = registry.get(project_id, "prompt", skill.prompt_ref.id, skill.prompt_ref.revision)
        if prompt is None or prompt["content"]["status"] != "published":
            raise ValueError("skill_requires_published_prompt")
        definition = PromptDefinition.model_validate(prompt["content"])
        expected_stage = skill.stage if skill.hook == "replace" else "skill"
        if definition.stage != expected_stage or digest(definition.output_schema) != digest(
            skill.output_schema
        ):
            raise ValueError("skill_prompt_contract_mismatch")
        if skill.hook == "replace":
            if skill.stage in replace_stages:
                raise ValueError("multiple_replace_hooks")
            replace_stages.add(skill.stage)
        skills.append(
            record["content"]
            | {
                "id": ref.id,
                "revision": ref.revision,
                "hash": record["sha256"],
                "prompt": prompt["content"]
                | {"id": prompt["id"], "revision": prompt["revision"], "hash": prompt["sha256"]},
            }
        )
        refs.extend(
            [
                {"kind": "skill", **ref.model_dump()},
                {"kind": "prompt", **skill.prompt_ref.model_dump()},
            ]
        )
    skills.sort(
        key=lambda x: (
            x["stage"],
            {"before": 0, "replace": 1, "after": 2}[x["hook"]],
            x["order"],
            x["id"],
        )
    )
    return prompts, skills, list({(x["kind"], x["id"], x["revision"]): x for x in refs}.values())


def preview_prompt(content: dict, data: dict):
    prompt = PromptDefinition.model_validate(content)
    return {
        "messages": [
            {
                "role": "system",
                "content": POLICY
                + "\n\n"
                + rendered(prompt.model_dump(), prompt.stage, prompt.output_schema),
            },
            {"role": "user", "content": {"trust": "untrusted_data_only", "data": data}},
        ],
        "output_schema": prompt.output_schema,
    }
