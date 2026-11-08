"""Independent assessment dimensions and comparison across one catalog version."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Assessment:
    applicability: str
    assessment: str
    implementation_verification: str = "not_verified"
    reason: str = ""

    def __post_init__(self):
        if self.applicability not in {"applicable", "not_applicable", "unknown"}:
            raise ValueError("invalid applicability")
        if self.assessment not in {
            "satisfied_by_spec",
            "contradicted_by_spec",
            "not_addressed",
            "unknown",
            "not_assessed",
        }:
            raise ValueError("invalid assessment")
        if self.implementation_verification != "not_verified":
            raise ValueError("document-only evaluation cannot verify implementation")
        if self.applicability == "not_applicable" and self.assessment != "not_assessed":
            raise ValueError("not applicable must be not assessed")
        if (
            self.applicability == "unknown"
            and self.assessment != "unknown"
            and not (self.assessment == "not_assessed" and self.reason == "no_active_clauses")
        ):
            raise ValueError("unknown applicability requires unknown assessment")


def effect(before: Assessment, after: Assessment) -> str:
    if all(
        item.assessment == "not_assessed" and item.reason == "no_active_clauses"
        for item in (before, after)
    ):
        return "unchanged"
    if "unknown" in (
        before.applicability,
        after.applicability,
        before.assessment,
        after.assessment,
    ):
        return "unresolved"
    if after.applicability == "applicable" and before.applicability == "not_applicable":
        return "introduced"
    if before == after or (before.applicability, before.assessment) == (
        after.applicability,
        after.assessment,
    ):
        return "unchanged"
    if "not_assessed" in (before.assessment, after.assessment):
        return "unresolved"
    if before.assessment == "satisfied_by_spec" and after.assessment in {
        "not_addressed",
        "contradicted_by_spec",
    }:
        return "regressed"
    if (
        before.assessment in {"not_addressed", "contradicted_by_spec"}
        and after.assessment == "satisfied_by_spec"
    ):
        return "improved"
    return "unresolved"


def record_effect(record, before, after, before_facts, after_facts, *, additions_only=False):
    """Compare independent conjuncts while treating an ANY as one obligation.

    Existing debt in one ALL conjunct must not hide a changed second conjunct.
    A lawful change between ANY alternatives must not become a regression.
    Unknown/changed guards remain atomic, avoiding invented branch identity.
    """
    from .domain import clauses, condition
    from .evaluation import active_clauses

    result = effect(
        Assessment(
            before["applicability"], before["assessment"], reason=before.get("reason") or ""
        ),
        Assessment(after["applicability"], after["assessment"], reason=after.get("reason") or ""),
    )
    if before["applicability"] != "applicable" or after["applicability"] != "applicable":
        return result, []
    if any(
        row.get("reason") not in {None, "no_active_clauses", "unknown_guard"}
        and not (
            row.get("reason") == "source_conflict" and row.get("source_conflict_scope") == "clauses"
        )
        for row in (before, after)
    ):
        return result, []

    def units(node, path):
        if node == "not_applicable_branch":
            return []
        if "all" in node:
            return [
                unit
                for index, child in enumerate(node["all"])
                for unit in units(child, f"{path}/all/{index}")
            ]
        if "when" in node:
            previous, proposed = (
                condition(node["when"], before_facts),
                condition(node["when"], after_facts),
            )
            if previous is not None and previous == proposed:
                branch = "then" if previous else "else"
                return [
                    (
                        key,
                        {
                            "when": node["when"],
                            "then": expr if previous else "not_applicable_branch",
                            "else": "not_applicable_branch" if previous else expr,
                        },
                    )
                    for key, expr in units(node[branch], path + "/" + branch)
                ]
        return [(path, node)]

    outcomes = [
        {cid: judgment["assessment"] for cid, judgment in row.get("clause_assessments", {}).items()}
        for row in (before, after)
    ]
    active_both = active_clauses(record, before_facts)[0] & active_clauses(record, after_facts)[0]
    removals = {
        cid: judgment["control_removal"]
        for cid, judgment in after.get("clause_assessments", {}).items()
        if cid in active_both
        and judgment.get("control_removal")
        and judgment["assessment"] == "contradicted_by_spec"
    }

    def referenced(node):
        if node == "not_applicable_branch":
            return set()
        if "clause" in node:
            return {node["clause"]}
        children = (
            [node["then"], node["else"]] if "when" in node else node.get("all", node.get("any", []))
        )
        return set().union(*(referenced(child) for child in children))

    changes = []
    for path, expression in units(record["clause_logic"], "/clause_logic"):
        evaluations = [
            clauses(expression, facts, values, record["clause_normativity"])
            for facts, values in zip((before_facts, after_facts), outcomes, strict=True)
        ]
        assessments = [
            Assessment(
                "not_applicable" if item.reason == "no_active_clauses" else "applicable",
                item.outcome,
            )
            for item in evaluations
        ]
        change = effect(*assessments)
        proof = {cid: removals[cid] for cid in referenced(expression) & removals.keys()}
        expanded = (
            additions_only
            and not proof
            and (
                change == "regressed"
                or (
                    evaluations[0].outcome == "not_addressed"
                    and evaluations[1].outcome == "contradicted_by_spec"
                )
            )
        )
        if expanded:
            # A resolved addition retains every baseline assertion. A new path
            # with missing/contradictory protection expands the assessed scope;
            # it does not remove an existing path's described safeguard.
            change = "introduced"
        if (
            proof
            and evaluations[1].outcome == "contradicted_by_spec"
            and evaluations[0].outcome
            in {"satisfied_by_spec", "not_addressed", "contradicted_by_spec"}
        ):
            # A broad canonical clause may retain other baseline debt while a
            # positively described safeguard is explicitly removed. The proof
            # was side/quote/delta-validated by P5; baseline satisfaction is not
            # promoted. An ANY that remains satisfied never enters this branch.
            change = "regressed"
        if change != "unchanged":
            changes.append(
                {
                    "path": path,
                    "expression": expression,
                    "baseline": evaluations[0].outcome,
                    "proposed": evaluations[1].outcome,
                    "change_effect": change,
                    **(
                        {"basis": "resolved_additions_preserve_existing_baseline"}
                        if expanded
                        else {}
                    ),
                    **({"control_removals": proof} if proof and change == "regressed" else {}),
                }
            )
    directions = {item["change_effect"] for item in changes} & {"regressed", "improved"}
    if changes and {item["change_effect"] for item in changes} == {"introduced"}:
        # Activating a previously guarded obligation introduces coverage; it
        # does not remove a protection that was already required in baseline.
        return "introduced", changes
    if result in {"unchanged", "unresolved"}:
        if len(directions) == 1:
            result = directions.pop()
            if result == "improved" and any(
                item["change_effect"] == "introduced" for item in changes
            ):
                result = "unresolved"
        elif len(directions) > 1:
            result = "unresolved"
    return result, changes
