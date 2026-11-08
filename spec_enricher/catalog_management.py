"""Metadata coverage, explicit domain selection and immutable catalogue updates."""

from __future__ import annotations

import copy
import json
from collections import Counter

from .catalog import digest, semantic_hash


def coverage(value):
    domains: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    for record in value["requirements"]:
        domain = domains.setdefault(
            record["domain"],
            {"id": record["domain"], "records": 0, "active_records": 0, "normative_clauses": 0},
        )
        domain["records"] += 1
        domain["active_records"] += record["status"] == "active"
        origin = record["origin"]
        path = origin.get("path") or "embedded:user-authored"
        source = sources.setdefault(
            path,
            {
                "path": path,
                "records": 0,
                "active_records": 0,
                "context_clauses": 0,
                "force_counts": Counter(),
                "nature_counts": Counter(),
                "licenses": set(),
                "record_ids": [],
            },
        )
        source["records"] += 1
        source["active_records"] += record["status"] == "active"
        source["record_ids"].append(record["id"])
        source["licenses"].add(origin.get("license"))
        for norm in record["clause_normativity"].values():
            if norm["force"] == "not_a_requirement" or norm["nature"] == "context":
                source["context_clauses"] += 1
            elif record["status"] == "active":
                source["force_counts"][norm["force"]] += 1
                source["nature_counts"][norm["nature"]] += 1
                domain["normative_clauses"] += 1
    # Source catalogue metadata can include a source with no selected records.
    known_sources = [
        {key: source.get(key) for key in ("source_id", "path", "file_sha256", "license")}
        for source in value.get("sources", [])
    ]
    for source in sources.values():
        source["licenses"] = sorted(source["licenses"], key=lambda item: item or "")
        source["record_count_preview"] = len(source["record_ids"])
        source["record_ids_preview"] = sorted(source.pop("record_ids"))[:25]
    return {
        "catalog_id": value["catalog_id"],
        "catalog_version": value["catalog_version"],
        "schema_version": value["schema_version"],
        "catalog_sha256": digest(value),
        "records": len(value["requirements"]),
        "active_records": sum(r["status"] == "active" for r in value["requirements"]),
        "domains": sorted(domains.values(), key=lambda item: item["id"]),
        "filter_values": {
            "domain": sorted(domains),
            "profile": sorted(
                {record["source_context"]["profile"] for record in value["requirements"]}
            ),
            "level": sorted({record["level"] for record in value["requirements"]}),
            "source_kind": sorted({record["source_kind"] for record in value["requirements"]}),
        },
        "declared_sources": known_sources,
        "source_coverage": sorted(sources.values(), key=lambda item: item["path"]),
        "basis": "Prepared catalogue metadata, not a new semantic source audit or a count of all controls in the upstream documents.",
        "implementation_verification": "not_verified",
        "notice": "Force/nature counted per active clause. Context excluded from normative counts. Review/process duties retained. Distinct stable IDs/profiles are not merged into a security score.",
    }


def domain_subset(value, selected_domains):
    selected = sorted(set(selected_domains))
    known = {record["domain"] for record in value["requirements"]}
    if set(selected) - known or len(selected) != len(selected_domains):
        raise ValueError("unknown_or_duplicate_domain")
    if not selected or set(selected) == known:
        return copy.deepcopy(value), []
    records = {record["id"]: record for record in value["requirements"]}
    chosen = {rid for rid, record in records.items() if record["domain"] in selected}
    original = set(chosen)
    queue = list(chosen)
    while queue:
        record = records[queue.pop()]
        for dependency in record.get("dependencies", []):
            target_id = dependency["requirement_id"]
            target = records.get(target_id)
            if (
                target is None
                or target["version"] != dependency["version"]
                or semantic_hash(target) != dependency["target_semantic_sha256"]
            ):
                raise ValueError("subset_dependency_unavailable_or_changed")
            if target_id not in chosen:
                chosen.add(target_id)
                queue.append(target_id)
    result = copy.deepcopy(value)
    result.update(
        catalog_id=value["catalog_id"] + ".domains-" + digest(selected)[:12],
        scope="domain_subset",
        domain_filter=selected,
        requirements=[record for record in result["requirements"] if record["id"] in chosen],
    )
    result["origin"] = {
        **result["origin"],
        "subset_of": {
            "catalog_id": value["catalog_id"],
            "version": value["catalog_version"],
            "sha256": digest(value),
        },
    }
    additions = sorted(chosen - original)
    result["limitations"] = [
        *result["limitations"],
        "Explicit domain subset with dependency closure; other domain controls are not covered.",
    ]
    return result, additions


def update_preview(
    store,
    project_id,
    previous_id,
    candidate,
    *,
    clause_mappings=(),
    record_mappings=(),
    retirements=None,
):
    previous = store.catalog(project_id, previous_id)
    if previous is None:
        raise LookupError("previous_catalogue_not_found")
    if (
        previous["catalog_id"] != candidate["catalog_id"]
        or previous["schema_version"] != candidate["schema_version"]
    ):
        raise ValueError("update_requires_same_catalogue_and_active_schema")
    old = {record["id"]: record for record in previous["requirements"]}
    new = {record["id"]: record for record in candidate["requirements"]}
    retirements = retirements or {}
    seen_clauses, seen_records = set(), set()
    mapped_clauses, mapped_records = [], []
    for mapping in clause_mappings:
        rid, cid = mapping["old_requirement_id"], mapping["old_clause_id"]
        key = (rid, cid)
        if (
            rid not in old
            or cid not in {part["id"] for part in old[rid]["clauses"]}
            or key in seen_clauses
        ):
            raise ValueError("unknown_or_duplicate_old_clause_mapping")
        seen_clauses.add(key)
        targets, target_keys = [], set()
        for target in mapping["targets"]:
            target_id, clause_id = target["requirement_id"], target["clause_id"]
            target_key = (target_id, clause_id)
            if (
                target_id not in new
                or clause_id not in {part["id"] for part in new[target_id]["clauses"]}
                or target_key in target_keys
            ):
                raise ValueError("unknown_or_duplicate_new_clause_mapping")
            target_keys.add(target_key)
            targets.append(
                target
                | {
                    "version": new[target_id]["version"],
                    "target_semantic_sha256": semantic_hash(new[target_id]),
                }
            )
        mapped_clauses.append(
            mapping
            | {
                "old_version": old[rid]["version"],
                "old_semantic_sha256": semantic_hash(old[rid]),
                "targets": targets,
            }
        )
    for mapping in record_mappings:
        rid = mapping["old_requirement_id"]
        if (
            rid not in old
            or rid in seen_records
            or len(set(mapping["target_requirement_ids"])) != len(mapping["target_requirement_ids"])
            or any(target not in new for target in mapping["target_requirement_ids"])
        ):
            raise ValueError("unknown_or_duplicate_requirement_mapping")
        seen_records.add(rid)
        mapped_records.append(
            mapping
            | {
                "old_version": old[rid]["version"],
                "old_semantic_sha256": semantic_hash(old[rid]),
                "targets": [
                    {
                        "id": target,
                        "version": new[target]["version"],
                        "target_semantic_sha256": semantic_hash(new[target]),
                    }
                    for target in mapping["target_requirement_ids"]
                ],
            }
        )
    if any(
        rid not in old or rid in new or not reason.strip() for rid, reason in retirements.items()
    ):
        raise ValueError("invalid_requirement_retirement_reason")
    missing_clause_mappings = [
        {"old_requirement_id": rid, "old_clause_id": clause["id"]}
        for rid in old.keys() & new.keys()
        for clause in old[rid]["clauses"]
        if clause["id"] not in {part["id"] for part in new[rid]["clauses"]}
        and (rid, clause["id"]) not in seen_clauses
    ]
    missing_record_mappings = sorted(
        rid
        for rid in old.keys() - new.keys()
        if old[rid]["status"] == "active" and rid not in seen_records and rid not in retirements
    )
    modified, version_conflicts = [], []
    for rid in sorted(old.keys() & new.keys()):
        if digest(old[rid]) == digest(new[rid]):
            continue
        before, after = old[rid], new[rid]
        semantic_changed = semantic_hash(before) != semantic_hash(after)
        if semantic_changed and before["version"] == after["version"]:
            version_conflicts.append(rid)
        old_clauses = {part["id"] for part in before["clauses"]}
        new_clauses = {part["id"] for part in after["clauses"]}
        modified.append(
            {
                "requirement_id": rid,
                "old_version": before["version"],
                "new_version": after["version"],
                "semantic_content_changed": semantic_changed,
                "applicability_changed": before["applicability"] != after["applicability"],
                "force_or_nature_changed": before["clause_normativity"]
                != after["clause_normativity"],
                "severity_changed": before["severity"] != after["severity"],
                "dependencies_changed": before.get("dependencies", [])
                != after.get("dependencies", []),
                "clause_ids_retained": sorted(old_clauses & new_clauses),
                "clause_ids_removed": sorted(old_clauses - new_clauses),
                "clause_ids_added": sorted(new_clauses - old_clauses),
                "declared_mappings": after.get("policy_scope", {}).get("clause_lineage", {}),
                "old_semantic_sha256": semantic_hash(before),
                "new_semantic_sha256": semantic_hash(after),
            }
        )
    conflicts = []
    for row in store.connection.execute(
        "SELECT r.catalog_version_id,r.requirement_id,r.content FROM requirement_index r JOIN catalog_bindings b ON r.project_id=b.project_id AND r.catalog_version_id=b.catalog_version_id WHERE r.project_id=? AND b.enabled=1 AND r.catalog_version_id!=?",
        (project_id, previous_id),
    ):
        record = json.loads(row[2])
        override = record.get("policy_scope", {}).get("override_of")
        if override and override.get("catalog_version_id") == previous_id:
            target = new.get(override.get("requirement_id"))
            if target is None or semantic_hash(target) != override.get("target_semantic_sha256"):
                conflicts.append(
                    {
                        "catalog_version_id": row[0],
                        "requirement_id": row[1],
                        "kind": "local_override_target_changed",
                        "target_requirement_id": override.get("requirement_id"),
                    }
                )
        for dep in record.get("dependencies", []):
            if dep["requirement_id"] in old:
                target = new.get(dep["requirement_id"])
                if (
                    target is None
                    or target["version"] != dep["version"]
                    or semantic_hash(target) != dep["target_semantic_sha256"]
                ):
                    conflicts.append(
                        {
                            "catalog_version_id": row[0],
                            "requirement_id": row[1],
                            "kind": "active_dependency_requires_previous_snapshot",
                            "target_requirement_id": dep["requirement_id"],
                        }
                    )
    plan = {
        "previous_catalog_version_id": previous_id,
        "previous_sha256": digest(previous),
        "candidate_sha256": digest(candidate),
        "catalog_id": candidate["catalog_id"],
        "previous_version": previous["catalog_version"],
        "candidate_version": candidate["catalog_version"],
        "added_ids": sorted(new.keys() - old.keys()),
        "removed_ids": sorted(old.keys() - new.keys()),
        "deprecated_ids": sorted(
            rid
            for rid in new
            if new[rid]["status"] != "active" and (rid not in old or old[rid]["status"] == "active")
        ),
        "modified": modified,
        "requirement_version_reuse_conflicts": version_conflicts,
        "local_conflicts": conflicts,
        "clause_mappings": mapped_clauses,
        "record_mappings": mapped_records,
        "retirements": retirements,
        "missing_clause_mappings": missing_clause_mappings,
        "missing_record_mappings": missing_record_mappings,
        "can_activate": not conflicts
        and not version_conflicts
        and not missing_clause_mappings
        and not missing_record_mappings,
        "history_preserved": True,
        "notice": "No automatic rebase or rewriting of overrides/history. Install an inactive version for review, or explicitly replace the binding only after resolving active conflicts. New statements require new requirement versions; stable IDs and declared clause mappings remain visible.",
    }
    return plan | {"sha256": digest(plan)}
