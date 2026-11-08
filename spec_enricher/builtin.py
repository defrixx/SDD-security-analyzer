"""Pinned offline data package integrity, separate from semantic review."""

from __future__ import annotations

import json
from pathlib import Path

from .catalog import ROOT
from .snapshots import sha256

ACCEPTED_CATALOG_SHA256 = "855da895d4fc517d1a37c43f9808b8747fddf7e77098410d89ea00f5d811b059"


def load_verified(root: Path = ROOT) -> tuple[bytes, dict]:
    data = (root / "catalog/requirements.full.json").read_bytes()
    if sha256(data) != ACCEPTED_CATALOG_SHA256:
        raise ValueError("builtin acceptance hash changed; recheck required")
    value = json.loads(data)
    snapshots: dict[str, tuple[str, list[str]]] = {}
    for record in value["requirements"]:
        origin = record["origin"]
        if origin["kind"] != "upstream_source":
            continue
        relative = origin["source_snapshot"]
        if relative not in snapshots:
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to((root / "sources").resolve()) or path.is_symlink():
                raise ValueError("source snapshot outside offline package")
            raw = path.read_bytes()
            snapshots[relative] = (sha256(raw), raw.decode().splitlines())
        checksum, lines = snapshots[relative]
        if checksum != origin["file_sha256"]:
            raise ValueError("source snapshot hash changed; recheck required")
        for ref in record["source_refs"]:
            if "\n".join(lines[ref["line_start"] - 1 : ref["line_end"]]) != ref["quote"]:
                raise ValueError("pinned citation mismatch")
    return data, value
