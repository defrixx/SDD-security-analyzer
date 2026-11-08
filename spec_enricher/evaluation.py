"""Guard-aware documentary and enforcement matrices, independent of model vendors."""

from __future__ import annotations

from .domain import clauses, condition


def active_clauses(record: dict, facts: dict) -> tuple[set[str], set[str]]:
    certain: set[str] = set()
    uncertain: set[str] = set()

    def walk(node, known=True):
        if node == "not_applicable_branch":
            return
        if "clause" in node:
            cid = node["clause"]
            if record["clause_normativity"][cid]["force"] != "not_a_requirement":
                (certain if known else uncertain).add(cid)
        elif "when" in node:
            value = condition(node["when"], facts)
            if value is True:
                walk(node["then"], known)
            elif value is False:
                walk(node["else"], known)
            else:
                walk(node["then"], False)
                walk(node["else"], False)
        else:
            for child in node.get("all", node.get("any", [])):
                walk(child, known)

    walk(record["clause_logic"])
    return certain, uncertain - certain


def verification_input(record: dict, facts: dict, active: set[str]):
    plan = record["verification"]["specification"]
    steps = []
    skipped = []
    for step in plan["steps"]:
        if not set(step["clause_ids"]) & active or (
            "when" in step and condition(step["when"], facts) is not True
        ):
            skipped.append({"id": step["id"], "reason": "inactive_or_unknown_branch"})
        else:
            steps.append(step | {"clause_ids": sorted(set(step["clause_ids"]) & active)})
    return {
        "possible_documentary_sources": plan["inputs"],
        "steps": steps,
        "skipped_steps": skipped,
        "conditional_inputs": [
            item
            for item in plan.get("conditional_inputs", [])
            if condition(item["when"], facts) is True and set(item["clause_ids"]) & active
        ],
        "outcome_conditions": plan["outcome_conditions"],
        "source_level_execution": "not_available_plan_only",
        "implementation_verification": "not_verified",
    }


def context_only(record: dict) -> bool:
    return all(
        value["force"] == "not_a_requirement" for value in record["clause_normativity"].values()
    )


def matrix_row(
    record: dict,
    facts: dict,
    judgments: dict[str, dict],
    *,
    reason: str | None = None,
    adoption: dict | None = None,
    source_conflict: bool = False,
):
    applicability = condition(record["applicability"]["expression"], facts)
    row = {
        "applicability": "unknown"
        if applicability is None
        else "applicable"
        if applicability
        else "not_applicable",
        "assessment": "unknown",
        "implementation_verification": "not_verified",
        "evidence_state": "insufficient",
        "clause_assessments": judgments,
        "clause_normativity": record["clause_normativity"],
        "enforcement": "unknown",
        "reason": reason,
        "incomplete": reason is not None,
        "adoption": adoption
        or {
            "mode": "required",
            "reason": "Accepted source profile includes its required defaults.",
        },
    }
    outcomes = {cid: value["assessment"] for cid, value in judgments.items()}
    active, _ = active_clauses(record, facts)
    conflicting_clauses = sorted(
        cid
        for cid, value in judgments.items()
        if cid in active and value.get("evidence_state") == "conflicting"
    )
    documentary = clauses(record["clause_logic"], facts, outcomes, record["clause_normativity"])
    if documentary.reason == "no_active_clauses":
        row.update(
            assessment="not_assessed", enforcement="not_assessed", reason="no_active_clauses"
        )
    elif applicability is False:
        row.update(assessment="not_assessed", enforcement="not_assessed", reason="not_applicable")
    elif applicability is None:
        row.update(reason=reason or "unknown_applicability", incomplete=True)
    elif reason:
        row.update(assessment="not_assessed", enforcement="not_assessed")
    else:
        enforcement = clauses(
            record["clause_logic"], facts, outcomes, record["clause_normativity"], enforcement=True
        )
        row.update(
            assessment=documentary.outcome,
            enforcement=enforcement.outcome,
            reason=documentary.reason,
            enforcement_reason=enforcement.reason,
            incomplete=documentary.incomplete or enforcement.incomplete,
            evidence_state="conflicting"
            if any(value.get("evidence_state") == "conflicting" for value in judgments.values())
            else "direct"
            if judgments
            and all(value.get("evidence_state") == "direct" for value in judgments.values())
            else "inferred"
            if judgments
            else "insufficient",
        )
    if conflicting_clauses:
        # Clause-level uncertainty participates in the existing typed logic.
        # It cannot erase an independent ALL violation or a satisfied ANY.
        row.update(source_conflict_scope="clauses", conflicting_clause_ids=conflicting_clauses)
        if row["assessment"] == "unknown" and row["reason"] is None:
            row["reason"] = "source_conflict"
    if (
        source_conflict
        and not reason
        and applicability is not False
        and documentary.reason != "no_active_clauses"
    ):
        # Conflicting control descriptions do not erase an independently known
        # architectural scope. Scope conflicts already arrive as unknown facts.
        row.update(
            assessment="unknown",
            enforcement="unknown",
            reason="source_conflict",
            evidence_state="conflicting",
            incomplete=True,
            source_conflict_scope="record",
        )
    if row["adoption"]["mode"] in {"waived", "advisory"}:
        row["enforcement"] = row["adoption"]["mode"]
    elif row["adoption"]["mode"] == "undecided":
        row.update(enforcement="unknown", enforcement_reason="adoption_undecided", incomplete=True)
    return row
