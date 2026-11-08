"""One catalog 4.0 contract for imports, editor and API.

Structural and reference checks do not claim a semantic source audit.
No source path, URL, algorithm, or imported schema is executed or fetched.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .i18n import translate

ROOT = Path(__file__).resolve().parents[1]


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def semantic_hash(record: dict) -> str:
    return digest(
        {
            k: v
            for k, v in record.items()
            if k not in {"review", "normativity_review", "dependencies"}
        }
    )


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads((ROOT / "catalog/requirements.schema.json").read_text())


def pointer(parts) -> str:
    return "/" + "/".join(str(x).replace("~", "~0").replace("/", "~1") for x in parts)


def validate(catalog: object) -> list[dict]:
    issues: list[dict] = []

    def fail(path: str, code: str, message: str):
        if len(issues) < 200:
            issues.append({"path": path, "code": code, "message": message})

    if not isinstance(catalog, dict) or catalog.get("schema_version") != "4.0":
        return [
            {
                "path": "/schema_version",
                "code": "unsupported_schema",
                "message": translate(
                    "Активный каталог требует schema_version 4.0; для старого формата доступен preview миграции."
                ),
            }
        ]
    validator = Draft202012Validator(schema(), format_checker=FormatChecker())
    for error in validator.iter_errors(catalog):
        fail(pointer(error.absolute_path), "schema_error", error.message)
        if len(issues) >= 200:
            break
    if issues:
        return issues
    records = catalog["requirements"]
    if len(records) > 5000:
        fail("/requirements", "record_limit", translate("Максимум 5000 требований."))
        return issues
    by_id: dict[str, dict] = {}
    for index, record in enumerate(records):
        rid = record["id"]
        if rid in by_id:
            fail(f"/requirements/{index}/id", "duplicate_id", translate("Повтор ID требования."))
        by_id[rid] = record
    edges: dict[str, list[str]] = {rid: [] for rid in by_id}
    for index, r in enumerate(records):
        p = f"/requirements/{index}"
        refs = {s["id"] for s in r["source_refs"]}
        predicates = {x["id"] for x in r["applicability"]["predicates"]}
        clause_ids = {x["id"] for x in r["clauses"]}
        for name, items in [
            ("source_refs", r["source_refs"]),
            ("clauses", r["clauses"]),
            ("applicability/predicates", r["applicability"]["predicates"]),
        ]:
            if len(items) != len({x["id"] for x in items}):
                fail(p + "/" + name, "duplicate_local_id", translate("Повтор локального ID."))

        def check_refs(values, allowed, path):
            if set(values) - allowed:
                fail(path, "dangling_reference", translate("Ссылка на необъявленный ID."))

        def ast(node, path, *, clause_tree=False, predicates=predicates, clause_ids=clause_ids):
            if isinstance(node, dict):
                if "predicate" in node:
                    check_refs([node["predicate"]], predicates, path)
                if "clause" in node:
                    check_refs([node["clause"]], clause_ids, path)
                for key, value in node.items():
                    if isinstance(value, (dict, list)):
                        ast(value, path + "/" + key, clause_tree=clause_tree)
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    ast(value, path + "/" + str(i), clause_tree=clause_tree)

        ast(r["applicability"]["expression"], p + "/applicability/expression")
        ast(r["clause_logic"], p + "/clause_logic", clause_tree=True)
        norm = r["clause_normativity"]
        if set(norm) != clause_ids:
            fail(
                p + "/clause_normativity",
                "normativity_coverage",
                translate("Карта силы должна точно покрывать clauses."),
            )
        for key, n in norm.items():
            check_refs(n["source_ref_ids"], refs, p + "/clause_normativity/" + key)
            if n["force"] != "undetermined" and (
                (n["force"] == "not_a_requirement") != (n["nature"] == "context")
            ):
                fail(
                    p + "/clause_normativity/" + key,
                    "context_force",
                    translate("Context и not_a_requirement должны совпадать."),
                )
        if r["classification_status"] == "context_only" and r["status"] != "deprecated":
            fail(
                p + "/status",
                "active_context",
                translate("Контекстная ссылка не может быть активным требованием."),
            )
        for part in r["clauses"] + r["applicability"]["predicates"]:
            check_refs(part["source_ref_ids"], refs, p)
        for key in ("framing_ref_ids", "qualifier_ref_ids"):
            check_refs(r["source_context"][key], refs, p + "/source_context/" + key)
        for plan_name in ("specification", "source_level"):
            plan = r["verification"][plan_name]
            covered: set[str] = set()
            if len(plan["steps"]) != len({step["id"] for step in plan["steps"]}):
                fail(
                    p + "/verification/" + plan_name, "duplicate_step", translate("Повтор ID шага.")
                )
            for step in plan["steps"]:
                covered.update(step["clause_ids"])
                check_refs(step["clause_ids"], clause_ids, p + "/verification/" + plan_name)
                if "when" in step:
                    ast(step["when"], p + "/verification/" + plan_name + "/when")
            required_coverage = {
                cid for cid, n in norm.items() if n["force"] != "not_a_requirement"
            }
            if required_coverage - covered:
                fail(
                    p + "/verification/" + plan_name,
                    "uncovered_clauses",
                    translate("План проверки не покрывает все нормативные clauses."),
                )
            for item in plan.get("conditional_inputs", []):
                check_refs(item["clause_ids"], clause_ids, p + "/verification/" + plan_name)
                ast(item["when"], p + "/verification/" + plan_name)
        if r["origin"]["kind"] != "upstream_source":
            lines = r["origin"]["embedded_source"].splitlines()
            for ref in r["source_refs"]:
                if (
                    ref["line_end"] < ref["line_start"]
                    or "\n".join(lines[ref["line_start"] - 1 : ref["line_end"]]) != ref["quote"]
                ):
                    fail(
                        p + "/source_refs",
                        "quote_mismatch",
                        translate("Цитата не совпадает со строками embedded source."),
                    )
        for dep in r.get("dependencies", []):
            target = by_id.get(dep["requirement_id"])
            check_refs(dep["originating_source_ref_ids"], refs, p + "/dependencies")
            if target is None:
                fail(
                    p + "/dependencies",
                    "missing_dependency",
                    translate("Нужен pinned dependency target; каталог остаётся заблокированным."),
                )
            else:
                edges[r["id"]].append(target["id"])
                if (
                    target["version"] != dep["version"]
                    or semantic_hash(target) != dep["target_semantic_sha256"]
                ):
                    fail(
                        p + "/dependencies",
                        "stale_dependency",
                        translate("Версия или semantic hash target не совпадает."),
                    )
    # Iterative DFS is bounded even for the maximum-size imported graph.
    colors: dict[str, int] = {}
    for root in edges:
        if colors.get(root):
            continue
        stack = [(root, False)]
        while stack:
            node, done = stack.pop()
            if done:
                colors[node] = 2
                continue
            if colors.get(node) == 1:
                fail(
                    "/requirements", "dependency_cycle", translate("Цикл нормативных dependencies.")
                )
                continue
            if colors.get(node) == 2:
                continue
            colors[node] = 1
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(edges[node]))
    return issues


def migration_preview(value: dict) -> dict:
    """Legacy data never becomes active by guessing normative force."""
    version = value.get("schema_version")
    if version not in {"1.0", "1.1", "2.0", "3.0"}:
        raise ValueError(translate("Неподдерживаемая схема миграции."))
    records = value.get("requirements", value.get("controls", []))
    if (
        not isinstance(records, list)
        or len(records) > 5000
        or any(not isinstance(record, dict) for record in records)
    ):
        raise ValueError("legacy_preview_requires_bounded_record_objects")
    from .builtin import load_verified

    _, current = load_verified()
    aliases: dict[str, list[dict]] = {}
    for record in current["requirements"]:
        reference = {
            "id": record["id"],
            "version": record["version"],
            "target_semantic_sha256": semantic_hash(record),
            "clause_ids": [clause["id"] for clause in record["clauses"]],
        }
        for alias in [record["id"], *record.get("legacy_ids", [])]:
            aliases.setdefault(alias, []).append(reference)
    return {
        "source_schema": version,
        "target_schema": "4.0",
        "status": "draft_only",
        "rejected_source": version == "2.0",
        "active": False,
        "mapping_basis": {
            "catalog_id": current["catalog_id"],
            "version": current["catalog_version"],
            "sha256": digest(current),
            "kind": "explicit_stable_ids_and_legacy_aliases_only",
        },
        "records": [
            {
                "source_id": r.get("id"),
                "source_version": r.get("version"),
                "original": r,
                "current_targets": aliases.get(r["id"], []) if isinstance(r.get("id"), str) else [],
                "mapping_requires_semantic_review": True,
                "unresolved_fields": [
                    "clause_normativity",
                    "guards_and_alternatives",
                    "provenance",
                    "verification",
                    "identity_mapping",
                ],
                "questions": [
                    translate(
                        "Заполните силу каждой clause и проверьте split/merge/exclusions; source_kind не задаёт force."
                    )
                ],
            }
            for r in records
            if isinstance(r, dict)
        ],
    }
