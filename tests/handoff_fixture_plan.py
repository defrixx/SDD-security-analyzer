"""Explicit handoff input allowlist. No expected results are read here."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

AUTHZ = "PSH3-APP-6C4147420216"
LOGGING = "PSH3-APP-CEE40834DA59"
MCP = "PSH3-AI-50AE690ADCC4"
REQUIREMENT_IDS = (AUTHZ, LOGGING, MCP)
INPUT_ROOT = Path("fixtures/synthetic/inputs")
DEFAULT_BASELINE = "project-a/master.md"
CASES = {
    "safe": (DEFAULT_BASELINE, "cases/safe.delta.md", None),
    "authz-regression": (DEFAULT_BASELINE, "cases/authz-regression.delta.md", None),
    "logging-regression": (DEFAULT_BASELINE, "cases/logging-regression.delta.md", None),
    "unknown-mcp": (DEFAULT_BASELINE, "cases/unknown.delta.md", None),
    "existing-debt": ("cases/existing-debt.master.md", "cases/unchanged-debt.delta.md", None),
    "fix-debt": ("cases/existing-debt.master.md", "cases/fix-debt.delta.md", None),
    "missing-control": (DEFAULT_BASELINE, "cases/missing-control.delta.md", None),
    "ambiguous": (DEFAULT_BASELINE, "cases/ambiguous.delta.md", None),
    "injection": (DEFAULT_BASELINE, "cases/injection.delta.md", None),
    "source-conflict": (DEFAULT_BASELINE, "cases/safe.delta.md", "cases/conflicting-source.md"),
}
EXTRA_INPUTS = ("project-b/master.md", "documents/context.json", "documents/context.yaml")


def inputs(root: Path) -> dict[str, bytes]:
    names = sorted({name for paths in CASES.values() for name in paths if name} | set(EXTRA_INPUTS))
    return {name: (root / INPUT_ROOT / name).read_bytes() for name in names}


def catalogue(root: Path) -> dict:
    raw = (root / "catalog/requirements.full.json").read_bytes()
    full = json.loads(raw)
    subset = copy.deepcopy(full)
    subset["requirements"] = [r for r in full["requirements"] if r["id"] in REQUIREMENT_IDS]
    if {r["id"] for r in subset["requirements"]} != set(REQUIREMENT_IDS):
        raise ValueError("handoff_requirement_missing")
    subset.update(
        catalog_id=full["catalog_id"] + ".handoff-evaluation",
        title="Explicit three-record handoff evaluation subset; unchanged schema 4.0 records",
        scope="user_catalogue",
        domain_filter=[],
    )
    subset["origin"]["subset_of"] = {
        "catalog_id": full["catalog_id"],
        "catalog_version": full["catalog_version"],
        "distribution_sha256": hashlib.sha256(raw).hexdigest(),
        "selected_requirement_ids": list(REQUIREMENT_IDS),
    }
    subset["limitations"].append(
        "Test-only explicit subset, not full domain coverage. Record versions, clauses, force, nature, guards and provenance are unchanged. Legacy fixture aggregate labels need a separate compatibility review."
    )
    return subset
