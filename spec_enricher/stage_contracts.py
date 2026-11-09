"""Formal model-step outputs and deterministic evidence/reference validation."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .effective_state import Block

CONFLICT_CITATION_COUNT = 2


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_by_name=True)


class EvidenceValidationError(ValueError):
    """Code-owned location only; no model text or source content in diagnostics."""

    def __init__(self, location: tuple[str | int, ...]):
        super().__init__("invented_or_out_of_scope_evidence")
        self.location = location


def declared_add_only(text: str) -> bool:
    """High-confidence author intent, not a guess for arbitrary prose.

    Mixed, conditional and tentative directives stay with the normal resolver.
    An unconditional explicit addition does not require choosing a replacement
    target. Unknown security design remains a separate P3/P5 question. Catalogue
    text is never executed.
    """
    return bool(
        re.match(r"^\s*(?:add|append|introduce|добавить|добавьте|добавляем)\b", text, re.I)
        and not re.search(
            r"\b(?:replac\w*|remov\w*|delet\w*|supersed\w*|updat\w*|modif\w*|chang\w*|alter\w*|замен\w*|удал\w*|убра\w*|измен\w*|обнов\w*|if|unless|maybe|perhaps|possibly|whether|undecided|tentative|если|возможно|предположительно)\b|\?|\bnot\s+(?:yet\s+)?decided\b|\bне\s+(?:решено|определено)\b",
            text,
            re.I,
        )
    )


def declared_operation_deferral(text: str) -> bool:
    """Recognize an explicit author request to postpone choosing the operation.

    This is a conservative guard, not a general prose classifier. Quoted examples
    and code are inert; ordinary missing security details are not delta ambiguity.
    A human operation override is handled separately by the pipeline.
    """
    prose = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is not None or re.match(r"^(?: {4}|\t|\s*[>\"'«“])", line):
            continue
        prose.append(line)
    text = " ".join(prose)
    text = re.sub(r'"[^"\n]*"|«[^»]*»|“[^”]*”|`[^`]*`', "", text)
    unclear = re.search(
        r"\b(?:unclear|undecided|неясно|непонятно)\b|"
        r"\bnot\s+(?:yet\s+)?decided\b|\bне\s+(?:решено|определено)\b",
        text,
        re.I,
    )
    choices = sum(
        bool(re.search(pattern, text, re.I))
        for pattern in (
            r"\b(?:add\w*|append\w*|добав\w*)\b",
            r"\b(?:replac\w*|supersed\w*|замен\w*)\b",
            r"\b(?:delet\w*|remov\w*|удал\w*)\b",
        )
    )
    clarify = re.search(
        r"\b(?:clarify|clarification|уточнить|уточните|уточнения)\b.{0,100}"
        r"\b(?:intent|operation|замыс\w*|операци\w*)\b|"
        r"\b(?:before|прежде)\b.{0,80}(?:proposed[-_ ]state|применени\w*|изменени\w*)",
        text,
        re.I,
    )
    # Only a later explicit decision clears the pending author request.
    decision = re.search(
        r"\b(?:decision|resolved|решено|решение)\s*:\s*"
        r"(?:add|append|replace|delete|remove|добав\w*|замен\w*|удал\w*)\b",
        text,
        re.I,
    )
    if (
        decision
        and unclear
        and clarify
        and decision.start() > max(unclear.start(), clarify.start())
    ):
        return False
    return bool(unclear and choices >= 2 and clarify)


class Citation(Contract):
    block_id: str
    quote: str = Field(min_length=1, max_length=8000)


class DeltaOperation(Contract):
    id: str
    delta_block_id: str
    explanation: str = Field(min_length=1, max_length=2000)
    target_block_id: str | None
    target_hash: str | None
    target_quote: str | None = Field(default=None, min_length=1, max_length=4096)
    question: str | None
    kind: Literal["add", "replace", "delete", "unresolved"] = Field(validation_alias="operation")

    @model_validator(mode="after")
    def target_shape(self):
        if self.kind == "add" and (
            self.target_block_id is not None
            or self.target_hash is not None
            or self.target_quote is not None
        ):
            raise ValueError("add_requires_null_target_id_and_hash")
        if self.kind in {"replace", "delete"} and (
            self.target_block_id is None or self.target_hash is None
        ):
            raise ValueError("replace_delete_require_existing_target_id_and_hash")
        if (self.target_block_id is None) != (self.target_hash is None):
            raise ValueError("target_id_and_hash_must_be_paired")
        if self.kind == "unresolved" and not self.question:
            raise ValueError("unresolved_requires_question")
        if self.kind != "unresolved" and self.question is not None:
            raise ValueError("resolved_operation_cannot_have_unresolved_question")
        return self


class OperationsOutput(Contract):
    operations: list[DeltaOperation] = Field(max_length=500)
    questions: list[str] = Field(max_length=100)

    @model_validator(mode="after")
    def unresolved_questions(self):
        if self.questions and not any(op.kind == "unresolved" for op in self.operations):
            raise ValueError("operation_questions_require_unresolved_operation")
        return self


class PredicateFact(Contract):
    requirement_id: str
    version: str
    predicate_id: str
    side: Literal["baseline", "proposed"]
    evidence: list[Citation] = Field(max_length=12)
    explanation: str = Field(min_length=1, max_length=2000)
    evidence_state: Literal["direct", "inferred", "insufficient", "conflicting"] = Field(
        validation_alias="support",
        description="State of evidence for THIS side's architectural fact. Conflicting means same-side sources disagree about that fact, not baseline-versus-delta change or policy noncompliance. Known values require direct/inferred citations; false requires direct absence evidence.",
    )
    question: str | None
    value: bool | None = Field(validation_alias="truth")

    @model_validator(mode="after")
    def uncertainty_shape(self):
        if self.value is None and not self.question:
            raise ValueError("unknown_fact_requires_question")
        if self.value is not None and self.question is not None:
            raise ValueError(
                "known_fact_cannot_have_unresolved_question_use_null_for_undecided_scope"
            )
        return self


class SourceConflict(Contract):
    requirement_ids: list[str] = Field(min_length=1, max_length=50)
    evidence: list[Citation] = Field(min_length=2, max_length=12)
    explanation: str = Field(min_length=1, max_length=2000)


class FactsOutput(Contract):
    facts: list[PredicateFact] = Field(max_length=1000)
    conflicts: list[SourceConflict] = Field(max_length=100)
    questions: list[str] = Field(max_length=100)


class ControlRemoval(Contract):
    baseline_evidence: list[Citation] = Field(min_length=1, max_length=4)
    delta_evidence: list[Citation] = Field(min_length=1, max_length=4)
    explanation: str = Field(min_length=1, max_length=2000)


class ClauseJudgment(Contract):
    requirement_id: str
    version: str
    clause_id: str
    side: Literal["baseline", "proposed"]
    applicable_design: str | None = Field(
        default=None,
        min_length=1,
        max_length=600,
        description="One concise factual statement of the supplied design for THIS exact clause and side, including relevant retained guarantees and supplied classification/content facts. No verdict, speculation, debate or reasoning steps. Historical DTOs may omit this field; new provider responses must supply it.",
    )
    evidence: list[Citation] = Field(max_length=12)
    explanation: str = Field(min_length=1, max_length=2000)
    evidence_state: Literal["direct", "inferred", "insufficient", "conflicting"] = Field(
        validation_alias="support",
        description="State of same-side documentary evidence, selected after its quotes and explanation. A clear specification violation has direct/inferred support, NOT conflicting. Conflicting means competing documentation sources disagree about the SAME control; set verdict unknown, cite both blocks and ask which source is authoritative. A baseline-versus-delta change or a separate API operation is not a source conflict.",
    )
    assessment: Literal[
        "satisfied_by_spec", "contradicted_by_spec", "not_addressed", "unknown", "not_assessed"
    ] = Field(validation_alias="verdict")
    question: str | None
    control_removal: ControlRemoval | None = Field(
        default=None,
        description="Only for a proposed contradiction: exact positive baseline evidence for a particular safeguard within this clause and exact delta evidence explicitly removing/contradicting that same safeguard. Other baseline duties may still be missing; this does not make the whole baseline clause satisfied. Null for unchanged debt, unknown scope, or no positively described prior safeguard.",
    )

    @model_validator(mode="after")
    def uncertainty_shape(self):
        if self.control_removal is not None and (
            self.side != "proposed" or self.assessment != "contradicted_by_spec"
        ):
            raise ValueError("control_removal_requires_proposed_contradiction")
        if self.assessment == "unknown" and not self.question:
            raise ValueError("unknown_judgment_requires_question")
        if self.assessment in {"satisfied_by_spec", "contradicted_by_spec"} and (
            not self.evidence or self.evidence_state not in {"direct", "inferred"}
        ):
            raise ValueError("known_judgment_requires_nonconflicting_evidence")
        if self.evidence_state == "conflicting" and (
            self.assessment != "unknown" or len({e.block_id for e in self.evidence}) < 2
        ):
            raise ValueError("conflicting_judgment_requires_unknown_and_two_sources")
        return self


class JudgmentsOutput(Contract):
    judgments: list[ClauseJudgment] = Field(max_length=1000)
    retrieval_queries: list[str] = Field(max_length=3)
    questions: list[str] = Field(max_length=100)


class ProposedPatch(Contract):
    id: str
    finding_ids: list[str] = Field(min_length=1, max_length=50)
    operation: Literal["insert", "replace", "delete"]
    target_block_id: str | None
    target_hash: str
    original: str
    replacement: str = Field(max_length=32000)
    explanation: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    changes_business_scope: bool


class PatchesOutput(Contract):
    patches: list[ProposedPatch] = Field(max_length=100)
    questions: list[str] = Field(max_length=100)


class CritiqueIssue(Contract):
    type: Literal[
        "unverifiable_claim", "evidence_error", "remaining_gap", "scope_change", "contradiction"
    ]
    patch_id: str | None
    requirement_id: str | None
    explanation: str = Field(min_length=1, max_length=2000)


class CritiqueOutput(Contract):
    issues: list[CritiqueIssue] = Field(max_length=100)
    reassessment: list[ClauseJudgment] = Field(max_length=1000)
    questions: list[str] = Field(max_length=100)


def bind_schema(stage: str, schema: dict, data: dict, *, literal_choices: bool = True) -> dict:
    """Constrain reference vocabulary; semantic/side/hash checks remain mandatory."""
    from .catalog import digest
    from .schema_definition import _BoundBuiltinSchema

    contracts: dict[str, type[Contract]] = {
        "P2": OperationsOutput,
        "P3": FactsOutput,
        "P5": JudgmentsOutput,
        "P7": PatchesOutput,
        "P8": CritiqueOutput,
    }
    builtin = stage in contracts and digest(schema) == digest(contracts[stage].model_json_schema())
    result = deepcopy(schema)
    definitions = result.get("$defs", {})

    def field(definition: str, name: str, values, nullable: bool = False):
        if definition in definitions:
            prop = definitions[definition]["properties"][name]
            choices = sorted(set(values)) + ([None] if nullable else [])
            if choices:
                prop["enum"] = choices

    def side_constraint(side, side_blocks):
        ids = sorted({block["id"] for block in side_blocks})
        citations: dict
        if ids:
            name = side.title() + "Citation"
            if name not in definitions:
                citation = deepcopy(definitions["Citation"])
                citation["properties"]["block_id"]["enum"] = ids
                # Grammar-constrained literal choices preserve newlines and
                # Unicode exactly. Whole blocks remain available; at most sixteen
                # nonempty lines per block provide smaller quotes without an
                # unbounded enum for adversarial newline-heavy input. The full
                # record/source bytes and post-response semantic checks remain.
                if literal_choices:
                    references = []
                    for index, block in enumerate(side_blocks):
                        part_name = f"{name}Block{index}"
                        part = deepcopy(definitions["Citation"])
                        part["properties"]["block_id"] = {"const": block["id"]}
                        part["properties"]["quote"]["enum"] = list(
                            dict.fromkeys(
                                [
                                    block["text"],
                                    *[
                                        line.strip()
                                        for line in block["text"].splitlines()
                                        if line.strip()
                                    ][:16],
                                ]
                            )
                        )
                        definitions[part_name] = part
                        references.append({"$ref": "#/$defs/" + part_name})
                    # Reuse each literal menu once across ordinary citations
                    # and every conflict partition. Duplicating the quote text
                    # in each group needlessly exhausts the explicit byte cap.
                    citation = {"anyOf": references}
                definitions[name] = citation
            citations = {"items": {"$ref": "#/$defs/" + name}}
        else:
            citations = {"maxItems": 0}
        return {"side": {"const": side}, "evidence": citations}

    def conflicting_evidence(side, side_blocks):
        """Generate a representative pair of distinct documentary blocks.

        Tuple items deliberately have no homogeneous ``items`` sibling: some
        native grammar converters prefer that sibling and ignore prefixItems.
        Each pair of different block indices differs in at least one bit, so
        disjoint bit partitions admit either order of every legitimate pair
        without enumerating all N*(N-1) pairs. Historical/public DTOs still
        allow up to twelve citations; only new conflict generation selects two.
        """
        ids = sorted({block["id"] for block in side_blocks})
        side_constraint(side, side_blocks)
        citation = definitions[side.title() + "Citation"]
        variants = []
        for bit in range((len(ids) - 1).bit_length()):
            names = []
            for group in (0, 1):
                members = [value for index, value in enumerate(ids) if (index >> bit) & 1 == group]
                name = f"{side.title()}Conflict{bit}Group{group}Citation"
                selected: dict
                if literal_choices:
                    selected = {
                        "anyOf": [
                            reference
                            for reference in citation["anyOf"]
                            if definitions[reference["$ref"].removeprefix("#/$defs/")][
                                "properties"
                            ]["block_id"]["const"]
                            in members
                        ]
                    }
                else:
                    selected = deepcopy(citation)
                    selected["properties"]["block_id"]["enum"] = members
                definitions[name] = selected
                names.append(name)
            for order in (names, names[::-1]):
                variants.append(
                    {
                        "type": "array",
                        "minItems": CONFLICT_CITATION_COUNT,
                        "maxItems": CONFLICT_CITATION_COUNT,
                        "prefixItems": [{"$ref": "#/$defs/" + name} for name in order],
                    }
                )
        return {"anyOf": variants}

    blocks = data.get("baseline_blocks", []) + data.get("proposed_blocks", [])
    if stage == "P2":
        # Historical request DTOs may omit the new field; new model calls must
        # explicitly select a literal span for replace/delete and null for add.
        definitions["DeltaOperation"]["required"].append("target_quote")
        result["properties"]["operations"].update(
            minItems=len(data["delta_blocks"]), maxItems=len(data["delta_blocks"])
        )
        definitions["DeltaOperation"]["anyOf"] = [
            {
                "properties": {
                    "operation": {"const": "add"},
                    "target_block_id": {"type": "null"},
                    "target_hash": {"type": "null"},
                    "target_quote": {"type": "null"},
                    "question": {"type": "null"},
                }
            },
            {
                "properties": {
                    "operation": {"enum": ["replace", "delete"]},
                    "target_block_id": {"type": "string"},
                    "target_hash": {"type": "string"},
                    "target_quote": {"type": "string", "minLength": 1},
                    "question": {"type": "null"},
                }
            },
            {
                "properties": {
                    "operation": {"const": "unresolved"},
                    "question": {"type": "string", "minLength": 1},
                }
            },
        ]
        pending = {
            block["id"]
            for block in data["delta_blocks"]
            if declared_operation_deferral(block.get("text", ""))
        }
        addable = [block["id"] for block in data["delta_blocks"] if block["id"] not in pending]
        unresolved = [
            block["id"]
            for block in data["delta_blocks"]
            if not declared_add_only(block.get("text", ""))
        ]
        replaceable = [bid for bid in unresolved if bid not in pending]
        variants = []
        for variant, allowed in zip(
            definitions["DeltaOperation"]["anyOf"], (addable, replaceable, unresolved), strict=True
        ):
            if allowed:
                variant["properties"]["delta_block_id"] = {"enum": allowed}
                variants.append(variant)
        if variants:
            definitions["DeltaOperation"]["anyOf"] = variants
        else:
            # An empty delta requires operations:[]; JSON Schema forbids anyOf:[].
            definitions["DeltaOperation"].pop("anyOf")
        if not unresolved:
            result["properties"]["questions"]["maxItems"] = 0
        field("DeltaOperation", "delta_block_id", (b["id"] for b in data["delta_blocks"]))
        field("DeltaOperation", "target_block_id", (b["id"] for b in data["baseline_blocks"]), True)
        field("DeltaOperation", "target_hash", (b["hash"] for b in data["baseline_blocks"]), True)
    elif stage == "P3":
        variants = []
        for side in data.get("assessment_sides", ("baseline", "proposed")):
            for value in (True, False, None):
                properties = side_constraint(side, data[side + "_blocks"])
                properties.update(truth={"const": value})
                if value is None:
                    properties["question"] = {"type": "string", "minLength": 1}
                else:
                    properties.update(
                        question={"type": "null"},
                        evidence=properties["evidence"] | {"minItems": 1},
                        support={"enum": ["direct", "inferred"] if value else ["direct"]},
                    )
                variants.append({"properties": properties})
        definitions["PredicateFact"]["anyOf"] = variants
        conflict_side = "proposed" if data.get("review_mode") else "baseline"
        if len({block["id"] for block in data[conflict_side + "_blocks"]}) >= 2:
            definitions["SourceConflict"]["properties"]["evidence"] = conflicting_evidence(
                conflict_side, data[conflict_side + "_blocks"]
            )
        else:
            result["properties"]["conflicts"]["maxItems"] = 0
        records = data["requirements"]
        count = len(data.get("assessment_sides", ("baseline", "proposed"))) * sum(len(r["applicability"]["predicates"]) for r in records)
        result["properties"]["facts"].update(minItems=count, maxItems=count)
        field("PredicateFact", "requirement_id", (r["id"] for r in records))
        field("PredicateFact", "version", (r["version"] for r in records))
        field(
            "PredicateFact",
            "predicate_id",
            (p["id"] for r in records for p in r["applicability"]["predicates"]),
        )
    elif stage in {"P5", "P8"}:
        requested = data.get("requested_judgments", data.get("requested_reassessment", []))
        result["properties"]["judgments" if stage == "P5" else "reassessment"].update(
            minItems=len(requested), maxItems=len(requested)
        )
        field("ClauseJudgment", "requirement_id", (key[0] for key in requested))
        field("ClauseJudgment", "clause_id", (key[2] for key in requested))
        field("ClauseJudgment", "side", (key[1] for key in requested))
        field(
            "ClauseJudgment",
            "version",
            (r["version"] for r in data.get("requirements", data.get("catalog", []))),
        )
        if stage == "P8":
            blocks = data["candidate_blocks"]
            field("CritiqueIssue", "patch_id", (p["id"] for p in data["patches"]), True)
            field("CritiqueIssue", "requirement_id", (r["id"] for r in data["catalog"]), True)
            if not requested:
                result["properties"]["reassessment"]["maxItems"] = 0
        definitions["ClauseJudgment"]["required"].extend(["applicable_design", "control_removal"])
        # The provider-only verdict alias preserves the stored/API assessment.
        # Both declaration order and canonical preview put factual design,
        # evidence and explanation before the verdict. Do not force an enum
        # before the model has described this clause's actual evidence.
        # Legacy DTO limits remain readable; these bounds apply to new calls.
        definitions["ClauseJudgment"]["properties"]["applicable_design"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 600,
            "description": "Concise factual design for this exact clause/side, with relevant retained guarantees and supplied data classification. No verdict, speculation, debate or reasoning steps.",
        }
        # Inherit the shared explanation bound. A tighter provider-only cap can
        # force a comparison to stop mid-sentence before its verdict is chosen.
        # Each branch below supplies either homogeneous side-bound citations or
        # a distinct conflict tuple. Keeping the original homogeneous sibling
        # here would make native converters ignore a branch's tuple constraint.
        definitions["ClauseJudgment"]["properties"]["evidence"].pop("items", None)
        removal_ids = set(data.get("removal_delta_block_ids", []))
        removal_blocks = [b for b in data.get("proposed_blocks", []) if b["id"] in removal_ids]
        if stage == "P5" and data["baseline_blocks"] and removal_blocks:
            definitions["ControlRemoval"]["properties"]["baseline_evidence"].update(
                side_constraint("baseline", data["baseline_blocks"])["evidence"]
            )
            definitions["ControlRemoval"]["properties"]["delta_evidence"].update(
                side_constraint("delta", removal_blocks)["evidence"]
            )
        else:
            # No impossible minItems:1/maxItems:0 object branch in the grammar.
            # Add-only and candidate-only contexts can only emit explicit null.
            definitions["ClauseJudgment"]["properties"]["control_removal"] = {"type": "null"}
            definitions.pop("ControlRemoval", None)
        variants = []
        for side in ("baseline", "proposed"):
            side_blocks = (
                data.get(side + "_blocks", [])
                if stage == "P5"
                else data["candidate_blocks"]
                if side == "proposed"
                else []
            )
            for assessments, states, minimum in (
                (["satisfied_by_spec"], ["direct", "inferred"], 1),
                (["contradicted_by_spec"], ["direct", "inferred"], 1),
                (["not_addressed"], ["direct", "inferred", "insufficient"], 0),
                (["unknown"], ["direct", "inferred", "insufficient"], 0),
                (["unknown"], ["conflicting"], 2),
            ):
                # Match the existing semantic validator: two quotes from one
                # block cannot be two distinct competing documentary sources.
                if states == ["conflicting"] and len({b["id"] for b in side_blocks}) < 2:
                    continue
                properties = side_constraint(side, side_blocks)
                properties.update(
                    verdict={"enum": assessments},
                    support={"enum": states},
                    evidence=properties["evidence"] | {"minItems": minimum},
                )
                if states == ["conflicting"]:
                    properties["evidence"] = conflicting_evidence(side, side_blocks)
                if stage != "P5" or side != "proposed" or assessments != ["contradicted_by_spec"]:
                    properties["control_removal"] = {"type": "null"}
                if assessments == ["unknown"]:
                    properties["question"] = {"type": "string", "minLength": 1}
                variants.append({"properties": properties})
        definitions["ClauseJudgment"]["anyOf"] = variants
        # Native decoding follows property declaration order. Select the
        # documentary verdict from the factual comparison before classifying
        # support: choosing `conflicting` first otherwise commits the grammar
        # to `unknown`, even after an explanation that establishes compliance.
        # Keep every branch/validator intact; only generation order changes.
        properties = definitions["ClauseJudgment"]["properties"]
        order = list(properties)
        order.remove("verdict")
        order.insert(order.index("support"), "verdict")
        definitions["ClauseJudgment"]["properties"] = {name: properties[name] for name in order}
    elif stage == "P7":
        blocks = data["delta_blocks"]
        field("ProposedPatch", "target_block_id", (b["id"] for b in blocks), True)
        field(
            "ProposedPatch", "target_hash", [data["original_sha256"]] + [b["hash"] for b in blocks]
        )
        field("ProposedPatch", "original", [""] + [b["text"] for b in blocks])
        definitions["ProposedPatch"]["properties"]["finding_ids"]["items"]["enum"] = [
            f["id"] for f in data["findings"]
        ]
    field("Citation", "block_id", (b["id"] for b in blocks))
    # Only exact code-owned contracts receive this in-memory proof. Binding
    # inserts inert enum/const values, not regex/refs/schema keywords from data.
    # Custom/remote schemas still require bounded definition validation.
    return _BoundBuiltinSchema(result) if builtin else result


def evidence(
    citations: list[Citation],
    allowed: dict[str, Block],
    *,
    location: tuple[str | int, ...] = (),
) -> list[dict]:
    result = []
    for index, citation in enumerate(citations):
        block = allowed.get(citation.block_id)
        if block is None or citation.quote not in block.text:
            raise EvidenceValidationError((*location, index))
        result.append(
            {
                "snapshot_id": block.snapshot_id,
                "source_id": block.snapshot_id,
                "block_id": block.id,
                "heading": block.heading,
                "line_start": block.start_line,
                "line_end": block.end_line,
                "quote": citation.quote,
                "block_sha256": block.hash,
                "byte_start": block.byte_start,
                "byte_end": block.byte_end,
            }
        )
    return result


def validate_facts(
    output: FactsOutput, records: list[dict], baseline: dict[str, Block], proposed: dict[str, Block],
    *, sides=("baseline", "proposed"), conflict_scope=None
):
    by_id = {record["id"]: record for record in records}
    expected = {
        (r["id"], side, p["id"])
        for r in records
        for side in sides
        for p in r["applicability"]["predicates"]
    }
    seen = set()
    for fact_index, fact in enumerate(output.facts):
        key = (fact.requirement_id, fact.side, fact.predicate_id)
        if (
            key not in expected
            or key in seen
            or fact.version != by_id[fact.requirement_id]["version"]
        ):
            raise ValueError("unknown_duplicate_or_unpinned_predicate")
        seen.add(key)
        if (
            fact.predicate_id
            in by_id[fact.requirement_id]
            .get("policy_scope", {})
            .get("authoring_unknown_predicate_ids", [])
            and fact.value is not None
        ):
            raise ValueError("authoring_unknown_condition_must_remain_unknown")
        evidence(
            fact.evidence,
            baseline if fact.side == "baseline" else proposed,
            location=("facts", fact_index, "evidence"),
        )
        if fact.value is None and not fact.question:
            raise ValueError("unknown_fact_requires_question")
        if fact.value is not None and (
            not fact.evidence or fact.evidence_state in {"insufficient", "conflicting"}
        ):
            raise ValueError("known_fact_requires_evidence")
        if fact.value is False and fact.evidence_state != "direct":
            raise ValueError("non_applicability_requires_direct_absence_evidence")
    if seen != expected:
        raise ValueError("missing_predicate_coverage")
    for conflict_index, conflict in enumerate(output.conflicts):
        if set(conflict.requirement_ids) - set(by_id):
            raise ValueError("unknown_conflict_requirement")
        evidence(conflict.evidence, baseline if conflict_scope is None else conflict_scope, location=("conflicts", conflict_index, "evidence"))
        if len({ref.block_id for ref in conflict.evidence}) < 2:
            raise ValueError("source_conflict_requires_two_blocks")


def validate_judgments(
    output: JudgmentsOutput,
    records: list[dict],
    expected: set[tuple[str, str, str]],
    baseline: dict[str, Block],
    proposed: dict[str, Block],
    *,
    removal_delta_ids: set[str] | None = None,
):
    by_id = {record["id"]: record for record in records}
    seen = set()
    for judgment_index, judgment in enumerate(output.judgments):
        key = (judgment.requirement_id, judgment.side, judgment.clause_id)
        if (
            key not in expected
            or key in seen
            or judgment.version != by_id[judgment.requirement_id]["version"]
        ):
            raise ValueError("unknown_duplicate_inactive_or_unpinned_clause")
        seen.add(key)
        evidence(
            judgment.evidence,
            baseline if judgment.side == "baseline" else proposed,
            location=("judgments", judgment_index, "evidence"),
        )
        if judgment.control_removal is not None:
            if not removal_delta_ids:
                raise ValueError("control_removal_requires_replace_or_delete_delta")
            evidence(
                judgment.control_removal.baseline_evidence,
                baseline,
                location=("judgments", judgment_index, "control_removal", "baseline_evidence"),
            )
            evidence(
                judgment.control_removal.delta_evidence,
                {key: block for key, block in proposed.items() if key in removal_delta_ids},
                location=("judgments", judgment_index, "control_removal", "delta_evidence"),
            )
        if judgment.assessment == "not_assessed":
            raise ValueError("active_clause_must_be_assessed_or_unknown")
        if (
            judgment.assessment in {"satisfied_by_spec", "contradicted_by_spec"}
            and not judgment.evidence
        ):
            raise ValueError("positive_or_contradictory_judgment_requires_evidence")
        if judgment.assessment == "unknown" and not judgment.question:
            raise ValueError("unknown_judgment_requires_question")
    if seen != expected:
        raise ValueError("missing_clause_coverage")
