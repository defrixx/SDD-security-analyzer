"""Pure, fail-closed evaluation of catalog 4.0 expressions.

The catalog is data: neither expressions nor verification plans are executed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

UNKNOWN = None
SKIPPED = "skipped"
SATISFIED = "satisfied_by_spec"
CONTRADICTED = "contradicted_by_spec"
NOT_ADDRESSED = "not_addressed"
NOT_ASSESSED = "not_assessed"
UNKNOWN_ASSESSMENT = "unknown"
OUTCOMES = {SATISFIED, CONTRADICTED, NOT_ADDRESSED, NOT_ASSESSED, UNKNOWN_ASSESSMENT}


class ExpressionError(ValueError):
    """An expression is malformed or uses an unsupported operator."""


def condition(expr: object, facts: Mapping[str, bool | None]) -> bool | None:
    if not isinstance(expr, dict) or len(expr) != 1:
        raise ExpressionError("condition must have exactly one operator")
    op, arg = next(iter(expr.items()))
    if op == "predicate":
        if not isinstance(arg, str) or not arg:
            raise ExpressionError("invalid predicate reference")
        value = facts.get(arg)
        if value is not None and type(value) is not bool:
            raise ExpressionError("predicate value must be true, false or unknown")
        return value
    if op == "not":
        value = condition(arg, facts)
        return None if value is None else not value
    if op in {"all", "any"}:
        if not isinstance(arg, list) or not arg:
            raise ExpressionError("all/any require a nonempty array")
        values = [condition(child, facts) for child in arg]
        if op == "all":
            return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False
    raise ExpressionError(f"unsupported condition operator: {op}")


@dataclass(frozen=True)
class Evaluation:
    outcome: str
    reason: str | None = None
    incomplete: bool = False


def clauses(
    expr: object,
    facts: Mapping[str, bool | None],
    outcomes: Mapping[str, str],
    normativity: Mapping[str, dict],
    *,
    enforcement: bool = False,
) -> Evaluation:
    """Evaluate explicit clause tree, excluding context before aggregation.

    Enforcement deliberately refuses ambiguous ANY alternatives mixing forces.
    """

    def potentially_active(node: object) -> bool:
        if node == "not_applicable_branch":
            return False
        if not isinstance(node, dict):
            raise ExpressionError("invalid clause expression")
        if set(node) == {"clause"}:
            ref = node["clause"]
            if not isinstance(ref, str) or ref not in normativity:
                raise ExpressionError("unknown clause reference")
            force = normativity[ref].get("force")
            if force not in {"required", "recommended", "not_a_requirement", "undetermined"}:
                raise ExpressionError("missing or unsupported clause force")
            return force != "not_a_requirement" and not (enforcement and force == "recommended")
        if set(node) == {"when", "then", "else"}:
            return potentially_active(node["then"]) or potentially_active(node["else"])
        if len(node) == 1:
            op, children = next(iter(node.items()))
            if op in {"all", "any"} and isinstance(children, list) and children:
                return any(potentially_active(child) for child in children)
        raise ExpressionError("invalid clause expression")

    def walk(node: object) -> Evaluation:
        if not isinstance(node, dict):
            raise ExpressionError("clause node must be an object")
        if set(node) == {"clause"}:
            ref = node["clause"]
            if not isinstance(ref, str) or ref not in normativity:
                raise ExpressionError("unknown clause reference")
            force = normativity[ref].get("force")
            if force not in {"required", "recommended", "not_a_requirement", "undetermined"}:
                raise ExpressionError("missing or unsupported clause force")
            if force == "not_a_requirement" or (enforcement and force == "recommended"):
                return Evaluation(SKIPPED, "excluded_force")
            if enforcement and force == "undetermined":
                return Evaluation(UNKNOWN_ASSESSMENT, "undetermined_force", True)
            outcome = outcomes.get(ref, UNKNOWN_ASSESSMENT)
            if outcome not in OUTCOMES:
                raise ExpressionError("invalid clause outcome")
            return Evaluation(outcome, None, outcome in {UNKNOWN_ASSESSMENT, NOT_ASSESSED})
        if set(node) == {"when", "then", "else"}:
            if not potentially_active(node["then"]) and not potentially_active(node["else"]):
                return Evaluation(SKIPPED, "excluded_force")
            value = condition(node["when"], facts)
            if value is None:
                return Evaluation(UNKNOWN_ASSESSMENT, "unknown_guard", True)
            branch = node["then"] if value else node["else"]
            return (
                Evaluation(SKIPPED, "inactive_branch")
                if branch == "not_applicable_branch"
                else walk(branch)
            )
        if len(node) != 1:
            raise ExpressionError("invalid clause expression")
        op, children = next(iter(node.items()))
        if op not in {"all", "any"} or not isinstance(children, list) or not children:
            raise ExpressionError("invalid clause operator")
        # Mixed required/advisory alternatives cannot be reduced safely to an
        # obligatory alternative; ask for review rather than inventing force.
        if enforcement and op == "any":
            forces = referenced_forces(children)
            if "required" in forces and (forces & {"recommended", "undetermined"}):
                return Evaluation(UNKNOWN_ASSESSMENT, "mixed_force_alternatives", True)
        results = [walk(child) for child in children]
        active = [item for item in results if item.outcome != SKIPPED]
        if not active:
            return Evaluation(SKIPPED, "no_active_clauses")
        precedence = (
            [CONTRADICTED, UNKNOWN_ASSESSMENT, NOT_ASSESSED, NOT_ADDRESSED, SATISFIED]
            if op == "all"
            else [SATISFIED, UNKNOWN_ASSESSMENT, NOT_ASSESSED, NOT_ADDRESSED, CONTRADICTED]
        )
        result = next(value for value in precedence if any(x.outcome == value for x in active))
        return Evaluation(result, None, any(x.incomplete for x in active))

    def referenced_forces(nodes: list) -> set[str]:
        refs: set[str] = set()

        def collect(node: object) -> None:
            if isinstance(node, dict):
                if set(node) == {"clause"}:
                    ref = node["clause"]
                    if not isinstance(ref, str) or ref not in normativity:
                        raise ExpressionError("unknown clause reference")
                    force = normativity[ref].get("force")
                    if force not in {
                        "required",
                        "recommended",
                        "not_a_requirement",
                        "undetermined",
                    }:
                        raise ExpressionError("missing or unsupported clause force")
                    refs.add(force)
                elif set(node) == {"when", "then", "else"}:
                    value = condition(node["when"], facts)
                    if value is not False:
                        collect(node["then"])
                    if value is not True:
                        collect(node["else"])
                else:
                    for key, value in node.items():
                        if key != "when":
                            collect(value)
            elif isinstance(node, list):
                for item in node:
                    collect(item)

        collect(nodes)
        return refs

    result = walk(expr)
    return (
        Evaluation(NOT_ASSESSED, "no_active_clauses", result.incomplete)
        if result.outcome == SKIPPED
        else result
    )
