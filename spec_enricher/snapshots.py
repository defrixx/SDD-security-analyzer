"""Immutable input hashes and selective, preconditioned text changes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Patch:
    id: str
    start: int
    end: int
    original: str
    replacement: str


class PatchConflict(ValueError):
    pass


def apply(original: bytes, expected_sha256: str, patches: list[Patch], accepted: set[str]) -> bytes:
    if sha256(original) != expected_sha256:
        raise PatchConflict("original hash changed")
    text = original.decode("utf-8")
    ids = [p.id for p in patches]
    if len(ids) != len(set(ids)) or not accepted.issubset(ids):
        raise PatchConflict("duplicate or unknown patch id")
    selected = sorted((p for p in patches if p.id in accepted), key=lambda p: (p.start, p.end))
    previous_end = 0
    for p in selected:
        if p.start < previous_end or p.start < 0 or p.end < p.start or p.end > len(text):
            raise PatchConflict("invalid or overlapping patch")
        if text[p.start : p.end] != p.original:
            raise PatchConflict("patch anchor mismatch")
        previous_end = p.end
    for p in reversed(selected):
        text = text[: p.start] + p.replacement + text[p.end :]
    return text.encode("utf-8")
