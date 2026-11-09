"""Native built-in P5 contract: optional non-decisional presentation summary."""

from copy import deepcopy

from .llm import OutputSchemaError
from .prompts import defaults
from .schema_worker import validate_payload
from .stage_contracts import JudgmentsOutput

WIRE_CONTRACT = "builtin.P5.native.optional-design-summary.v1"
INSTRUCTION = (
    "For this wire contract, applicable_design is an OPTIONAL presentation summary. "
    "If you supply it, keep it factual, nonempty and within 600 characters. "
    "You may omit it; do not invent a summary or use null/empty text. "
    "All verdict, evidence, explanation, support, question, control_removal and "
    "requirement/version/clause/side fields retain their required contracts."
)


def enabled(stage, profile, contract, instruction, *, prompt, schema, skill):
    return (
        stage == "P5"
        and profile.adapter == "lm_studio_native"
        and profile.output_mode == "prompt_json"
        and contract is JudgmentsOutput
        and instruction == defaults()["P5"]
        and prompt is None
        and schema is None
        and skill is None
    )


def instruction_for(prompt):
    # Only called after exact built-in matching. Avoid contradictory mandatory
    # wording while retaining all factual/evidence-first assessment guidance.
    template = prompt["template"].replace(
        "First state applicable_design: one short factual description",
        "Optionally state applicable_design: one short factual description",
        1,
    )
    return prompt | {"template": template}


def prepare(bound_canonical_schema):
    schema = deepcopy(dict(bound_canonical_schema))
    schema["$defs"]["ClauseJudgment"]["required"].remove("applicable_design")
    schema["title"] = WIRE_CONTRACT
    return schema, {
        "wire_contract": WIRE_CONTRACT,
        "optional_presentation_fields": ["applicable_design"],
        "missing_presentation_handling": "absent_to_null_without_inference",
    }


def decode(value, schema):
    # Local validation remains mandatory even for injected offline adapters.
    # Canonical DTO already represents an absent summary as None. No model
    # decision, citation, identifier or explanation is invented or repaired.
    issues = validate_payload(schema, value)
    if issues:
        raise OutputSchemaError(issues)
    return JudgmentsOutput.model_validate(value)
