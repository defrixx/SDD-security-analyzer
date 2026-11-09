"""Review-only exclusion authority; model claims remain immutable evidence, not scope decisions."""

from .domain import condition
from .evaluation import active_clauses, context_only

POLICY = "review-scope-confirmation.v1"


def exclusion_guard(record, facts, explicit_facts):
    """Only explicit user decisions may exclude model-dependent scope/obligations.

    This cannot establish semantic truth from a quotation. A model's negative
    applicability conclusion, including not(true), requires human confirmation.
    The same boundary applies to conditional obligations hidden by either value.
    """
    applicability = condition(record["applicability"]["expression"], facts)
    trusted_applicability = condition(record["applicability"]["expression"], explicit_facts)
    active, uncertain = active_clauses(record, facts)
    trusted_active, trusted_uncertain = active_clauses(record, explicit_facts)
    excluded = (trusted_active | trusted_uncertain) - (active | uncertain)
    held = not context_only(record) and trusted_applicability is not False and (
        applicability is False or bool(excluded)
    )
    return {
        "policy": POLICY,
        "requires_confirmation": held,
        "model_applicability": applicability,
        "effective_applicability": None if held and applicability is False else applicability,
        "excluded_clause_ids": sorted(excluded) if held else [],
        "reason": "review_scope_exclusion_requires_confirmation" if held else None,
    }
