"""Compare documentary results only after displaying pinned-context differences."""

from __future__ import annotations

import json
from collections import Counter

from .catalog import digest
from .i18n import translate


def compare_runs(repository, project_id, left_id, right_id):
    left, right = (repository.get(project_id, rid) for rid in (left_id, right_id))
    if left is None or right is None:
        raise LookupError("run_not_found_in_project")

    def basis(run):
        manifest = run["manifest"]
        request = manifest.get("request", {})
        return {
            "catalogues": sorted(
                (
                    c.get("catalog_id"),
                    c.get("version"),
                    c.get("schema_version"),
                    c.get("sha256"),
                    c.get("adoption_receipt", {}).get("sha256"),
                )
                for c in manifest.get("catalogs", [])
            ),
            "methodology_hash": manifest.get("methodology_hash"),
            "model_profiles": {
                stage: cfg.get("sha256", digest(cfg.get("content", {})))
                for stage, cfg in manifest.get("connections", {}).items()
            },
            "baseline_and_context": sorted(
                (d["role"], d["sha256"])
                for d in manifest.get("documents", [])
                if d["role"] != "delta"
            ),
            "source_snapshots": sorted(
                s["raw_sha256"] for s in manifest.get("source_snapshots", [])
            ),
            "adoption_overrides": request.get("adoption_overrides", []),
            "fact_overrides": request.get("fact_overrides", []),
            "algorithm_hashes": manifest.get("algorithm_hashes", {}),
            "execution_mode": manifest.get("execution_mode"),
        }

    a, b = basis(left), basis(right)
    differences = [{"field": key, "left": a[key], "right": b[key]} for key in a if a[key] != b[key]]
    # Missing evidence does not establish comparability for imported/legacy runs.
    complete_basis = all(
        (a["methodology_hash"], b["methodology_hash"], a["catalogues"], b["catalogues"])
    )
    compatible = not differences and complete_basis

    def results(rid):
        return {
            r[0]: {
                "version": r[1],
                "baseline": json.loads(r[2]),
                "proposed": json.loads(r[3]),
                "change_effect": r[4],
            }
            for r in repository.store.connection.execute(
                "SELECT requirement_id,requirement_version,baseline,proposed,change_effect FROM requirement_results WHERE project_id=? AND run_id=? ORDER BY requirement_id",
                (project_id, rid),
            )
        }

    x, y = results(left_id), results(right_id)
    changes = []
    counters: dict[str, Counter[str]] = {"left": Counter(), "right": Counter()}
    for label, values in (("left", x), ("right", y)):
        for value in values.values():
            counters[label][value["proposed"].get("assessment", "unknown")] += 1
    for rid in sorted(x.keys() | y.keys()):
        before, after = x.get(rid), y.get(rid)
        if before != after:
            changes.append(
                {
                    "requirement_id": rid,
                    "kind": "added"
                    if before is None
                    else "removed"
                    if after is None
                    else "changed",
                    "left": before,
                    "right": after,
                    "applicability_changed": bool(
                        before
                        and after
                        and before["proposed"].get("applicability")
                        != after["proposed"].get("applicability")
                    ),
                    "assessment_changed": bool(
                        before
                        and after
                        and before["proposed"].get("assessment")
                        != after["proposed"].get("assessment")
                    ),
                }
            )
    return {
        "project_id": project_id,
        "left_run_id": left_id,
        "right_run_id": right_id,
        "comparable_context": compatible,
        "differences": differences,
        "counts": counters,
        "changes": changes,
        "notice": translate(
            "Одинаковые pinned catalogue/methodology/context; переходы documentary assessment показаны раздельно от applicability. Модельные результаты могут различаться между повторами; implementation не проверена."
        )
        if compatible
        else translate(
            "Контекст/методика/версии различаются или не полностью закреплены. Разницу счётчиков нельзя считать эффектом delta. Сравнение ниже информационное."
        ),
        "security_score": None,
    }
