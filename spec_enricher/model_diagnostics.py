"""Code-owned repair diagnostics: never echo untrusted validation messages/keys."""

from pydantic import ValidationError

from .llm import OutputSchemaError, output_object, repair_text
from .network import NetworkError
from .stage_contracts import (
    CONFLICT_CITATION_COUNT,
    CritiqueOutput,
    EvidenceValidationError,
    FactsOutput,
    JudgmentsOutput,
    OperationsOutput,
    PatchesOutput,
)

SAFE_REASONS = {
    "model_output_truncated",
    "bound_conflict_requires_two_citations",
    "delta_operation_coverage",
    "explicit_operation_deferral_requires_question",
    "invented_delta_target",
    "target_quote_missing_or_ambiguous",
    "target_quote_conflicts_with_explicit_delta_quote",
    "invented_or_out_of_scope_evidence",
    "unknown_duplicate_or_unpinned_predicate",
    "unknown_fact_requires_question",
    "known_fact_requires_evidence",
    "non_applicability_requires_direct_absence_evidence",
    "missing_predicate_coverage",
    "unknown_duplicate_inactive_or_unpinned_clause",
    "missing_clause_coverage",
    "positive_or_contradictory_judgment_requires_evidence",
    "conflicting_judgment_requires_unknown_and_question",
    "unknown_judgment_requires_question",
    "conflicting_judgment_requires_unknown_and_two_sources",
    "known_judgment_requires_nonconflicting_evidence",
    "control_removal_requires_proposed_contradiction",
    "known_fact_cannot_have_unresolved_question_use_null_for_undecided_scope",
    "control_removal_requires_replace_or_delete_delta",
    "unknown_patch_finding",
    "patch_target_hash_conflict",
    "patch_original_conflict",
    "invalid_eof_patch",
    "critique_invented_reference",
    "credential_echo_rejected",
    "reasoning_option_not_honored",
    "empty_content_with_reasoning",
    "architectural_review_conflicts_must_remain_separate",
    "positive_review_requires_bounded_context_or_question",
}

SAFE_NETWORK_CODES = {
    "invalid_operator_egress_policy",
    "invalid_destination",
    "invalid_port",
    "dns_busy",
    "dns_unavailable",
    "dns_timeout",
    "dns_failed",
    "https_required",
    "invalid_local_address",
    "operator_external_egress_denied",
    "forbidden_address",
    "private_address_not_approved",
    "local_address_changed",
    "invalid_query",
    "invalid_endpoint_suffix",
    "request_timeout",
    "invalid_header",
    "redirect_forbidden",
    "unsupported_auth",
    "auth_failed",
    "rate_limited",
    "http_error",
    "response_too_large",
    "invalid_sse_event",
    "decompression_limit",
    "unsupported_encoding",
    "connect_or_protocol_failed",
}


def safe_network_failure(exc: NetworkError, stage: str) -> dict:
    """Only known codes and bounded numbers; no response bodies/headers/messages."""
    status, retry = exc.status, exc.retry_after
    return {
        "stage": stage if stage in {"P2", "P3", "P5", "P7", "P8"} else "model",
        "code": exc.code
        if type(exc.code) is str and exc.code in SAFE_NETWORK_CODES
        else "transport_error",
        "http_status": status if type(status) is int and 100 <= status <= 599 else None,
        "retry_after_seconds": retry if type(retry) is int and 0 <= retry <= 60 else None,
    }


REPAIR_HINTS = {
    "explicit_operation_deferral_requires_question": "The author explicitly postpones choosing add/replace/delete. Return unresolved with a concrete operation question; do not silently select an operation. A human override is a separate clarification.",
    "bound_conflict_requires_two_citations": "A conflicting judgment uses exactly TWO representative citations with distinct block_ids "
    "from this effective side. Return only that opposing pair; the full approved source context "
    "remains unchanged. Do not invent a source or change its meaning to meet the count.",
    "invented_or_out_of_scope_evidence": "Fix the evidence entry at validation_paths. Its quote must be an exact contiguous substring "
    "of its supplied same-side block, paired with that block's ID. Copy the complete supplied "
    "block when unsure. Do not paraphrase, combine nonadjacent sentences, translate or normalize "
    "whitespace. Unknown facts require a question; they never permit invented evidence.",
    "conflicting_judgment_requires_unknown_and_two_sources": "Conflicting support needs at least two DISTINCT block_ids from this effective side. "
    "Two quotes from one block are one source. Missing design uses not_addressed for "
    "known scope, or unknown/insufficient with a question for unresolved facts. A clear "
    "violation of a requirement has direct/inferred support, not conflicting. Reassess "
    "the supplied statements; never invent another opposing source.",
    "known_judgment_requires_nonconflicting_evidence": "Satisfied/contradicted judgments need direct/inferred positive literal evidence. "
    "Conflicting evidence requires unknown and a question; missing design is not a pass.",
}


def _field_names():
    result = {"assessment", "evidence_state", "value", "kind"}
    for model in (OperationsOutput, FactsOutput, JudgmentsOutput, PatchesOutput, CritiqueOutput):
        stack = [model.model_json_schema()]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                result.update(node.get("properties", {}))
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return result


SAFE_FIELDS = _field_names()
SAFE_SCHEMA_KEYWORDS = {
    "type",
    "enum",
    "const",
    "required",
    "additionalProperties",
    "anyOf",
    "oneOf",
    "allOf",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "pattern",
    "format",
    "uniqueItems",
    "not",
    "dependentRequired",
    "minProperties",
    "maxProperties",
    "multipleOf",
}


def _safe_path(parts):
    return [
        part
        if type(part) is int or type(part) is str and part in SAFE_FIELDS
        else "<untrusted_field>"
        for part in parts[:12]
    ]


def safe_output_rejection(exc, stage, *, repair_permitted, raw_content, contract, output_mode):
    """Refine rejection feedback only; never accept a schema-rejected response."""
    original = safe_rejection(exc, stage, repair_permitted=repair_permitted)
    if not isinstance(exc, OutputSchemaError) or contract not in {
        OperationsOutput,
        FactsOutput,
        JudgmentsOutput,
        PatchesOutput,
        CritiqueOutput,
    }:
        return original
    try:
        value, _ = output_object(repair_text(raw_content), output_mode)
        parsed = contract.model_validate(value)
        if contract is JudgmentsOutput:
            for index, judgment in enumerate(parsed.judgments):
                if (
                    judgment.evidence_state == "conflicting"
                    and len(judgment.evidence) > CONFLICT_CITATION_COUNT
                ):
                    original["reason_code"] = "bound_conflict_requires_two_citations"
                    original["repair_hint"] = REPAIR_HINTS[original["reason_code"]]
                    original["schema_validation_paths"] = original["validation_paths"]
                    original["validation_paths"] = [
                        {"path": ["judgments", index, "evidence"], "type": "bound_maxItems"}
                    ]
                    return original
    except ValidationError as detailed:
        refined = safe_rejection(detailed, stage, repair_permitted=repair_permitted)
        if refined["reason_code"] != "schema_or_output_contract_validation":
            refined["schema_validation_paths"] = original["validation_paths"]
            refined["diagnostic_only_dto_refinement"] = True
            return refined
    except (ValueError, TypeError):
        pass
    return original


def safe_rejection(exc, stage, *, repair_permitted):
    reason = str(exc) if not isinstance(exc, ValidationError) and str(exc) in SAFE_REASONS else None
    paths = []
    if isinstance(exc, ValidationError):
        for error in exc.errors(include_input=False, include_context=False)[:10]:
            candidate = error["msg"].removeprefix("Value error, ")
            if candidate in SAFE_REASONS and reason is None:
                reason = candidate
            paths.append(
                {
                    "path": _safe_path(error["loc"]),
                    "type": error["type"],
                }
            )
    elif isinstance(exc, EvidenceValidationError):
        paths.append({"path": _safe_path(exc.location), "type": "literal_reference"})
    elif isinstance(exc, OutputSchemaError):
        for issue in exc.validation_issues[:10]:
            location = issue["location"]
            if issue.get("missing_fields"):
                for field in issue["missing_fields"][:10]:
                    paths.append({"path": _safe_path(location + [field]), "type": "missing"})
                    if len(paths) >= 10:
                        break
            else:
                keyword = issue["keyword"]
                paths.append(
                    {
                        "path": _safe_path(location),
                        "type": "schema_" + keyword
                        if keyword in SAFE_SCHEMA_KEYWORDS
                        else "schema_validation",
                    }
                )
            if len(paths) >= 10:
                break
    result = {
        "stage": stage,
        "reason_code": reason or "schema_or_output_contract_validation",
        "repair_permitted": repair_permitted,
        "validation_paths": paths,
    }
    if reason in REPAIR_HINTS:
        result["repair_hint"] = REPAIR_HINTS[reason]
    if reason == "model_output_truncated":
        result["recovery_hint"] = (
            "Provider marked finish_reason=length. Inspect retained raw usage; reduce context/batch "
            "or explicitly adjust the output limit and loaded model context before a new run."
        )
    return result
