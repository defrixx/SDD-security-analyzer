"""Apply explicitly resolved delta operations without changing baseline snapshots."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .snapshots import sha256


@dataclass(frozen=True)
class Block:
    id: str
    snapshot_id: str
    text: str
    start_line: int
    end_line: int
    hash: str
    heading: str = ""
    byte_start: int = 0
    byte_end: int = 0


def blocks(snapshot_id: str, content: bytes) -> list[Block]:
    """Split paragraphs while preserving line-based anchors and exact content."""
    text = content.decode("utf-8")
    lines = text.splitlines(keepends=True)
    result = []
    start = None
    heading = ""
    byte_offsets = [0]
    for line in lines:
        byte_offsets.append(byte_offsets[-1] + len(line.encode("utf-8")))
    for i in range(len(lines) + 1):
        if i < len(lines) and lines[i].strip():
            match = re.match(r"^#{1,6}\s+(.+?)\s*$", lines[i])
            if match:
                heading = match[1]
            if start is None:
                start = i
        elif start is not None:
            value = "".join(lines[start:i])
            digest = sha256(value.encode("utf-8"))
            result.append(
                Block(
                    f"{snapshot_id}:{start + 1}:{digest[:12]}",
                    snapshot_id,
                    value,
                    start + 1,
                    i,
                    digest,
                    heading,
                    byte_offsets[start],
                    byte_offsets[i],
                )
            )
            start = None
    bounded = []
    for block in result:
        raw = block.text.encode()
        cursor = 0
        while cursor < len(raw):
            part = raw[cursor : cursor + 4096].decode("utf-8", errors="ignore")
            if not part:
                raise ValueError("invalid UTF-8 block boundary")
            end = cursor + len(part.encode())
            if cursor == 0 and end == len(raw):
                bounded.append(block)
            else:
                start_line = block.start_line + raw[:cursor].count(b"\n")
                end_line = start_line + part.count("\n") - int(part.endswith("\n"))
                checksum = sha256(part.encode())
                bounded.append(
                    Block(
                        f"{snapshot_id}:b{block.byte_start + cursor}:{checksum[:12]}",
                        snapshot_id,
                        part,
                        start_line,
                        end_line,
                        checksum,
                        block.heading,
                        block.byte_start + cursor,
                        block.byte_start + end,
                    )
                )
            cursor = end
    return bounded


@dataclass(frozen=True)
class Operation:
    id: str
    kind: str
    delta_block_id: str
    target_block_id: str | None = None
    target_hash: str | None = None
    replacement: str | None = None
    target_quote: str | None = None


@dataclass(frozen=True)
class EffectiveState:
    blocks: tuple[Block, ...]
    unresolved: tuple[dict, ...]


def target_quote(source: Block, target: Block, selected: str | None) -> str:
    """An explicit quoted target narrows a paragraph operation, never its hash.

    Quotes are inert literal data. No semantic expansion or generated replacement
    is accepted here. A selected span must occur exactly once in the pinned block.
    """
    pattern = r'"([^"\n]+)"|\'([^\'\n]+)\'|«([^»\n]+)»|“([^”\n]+)”|`([^`\n]+)`'
    hints = {
        value
        for match in re.finditer(pattern, source.text)
        for value in match.groups()
        if value
        and value in target.text
        and re.search(
            r"(?:\b(?:replace|delete|remove)\s+(?:(?:the|exactly|this|baseline|old|sentence|statement|text|phrase)\s+)*|(?:заменить|удалить|убрать)\s+(?:(?:фразу|утверждение|предложение|текст)\s+)*)$",
            source.text[: match.start()],
            re.IGNORECASE,
        )
    }
    if len(hints) == 1:
        explicit = next(iter(hints))
        if selected is not None and selected != explicit:
            raise ValueError("target_quote_conflicts_with_explicit_delta_quote")
        selected = explicit
    selected = target.text if selected is None else selected
    if not selected or target.text.count(selected) != 1:
        raise ValueError("target_quote_missing_or_ambiguous")
    return selected


def fragment(block: Block, start: int, end: int) -> Block:
    """Preserve a literal source span with its original UTF-8 byte coordinates."""
    if start == 0 and end == len(block.text):
        return block
    text = block.text[start:end]
    checksum = sha256(text.encode())
    byte_start = block.byte_start + len(block.text[:start].encode())
    line = block.start_line + block.text[:start].count("\n")
    return Block(
        f"{block.snapshot_id}:b{byte_start}:{checksum[:12]}",
        block.snapshot_id,
        text,
        line,
        line + text.count("\n") - int(text.endswith("\n")),
        checksum,
        block.heading,
        byte_start,
        byte_start + len(text.encode()),
    )


def resolve(
    baseline: list[Block], delta: list[Block], operations: list[Operation]
) -> EffectiveState:
    """Apply only uniquely anchored operations; unresolved ones remain questions.

    Unmentioned baseline stays present. Unclassified delta is not silently
    interpreted as an add, replacement, or deletion.
    """
    current = {b.id: b for b in baseline}
    incoming = {b.id: b for b in delta}
    if len(current) != len(baseline) or len(incoming) != len(delta):
        raise ValueError("duplicate block ID")
    if current.keys() & incoming.keys():
        raise ValueError("baseline and delta block IDs overlap")
    counts = Counter(op.delta_block_id for op in operations)
    problems: dict[str, str] = {}
    plans: dict[str, list[tuple[int, int, Operation, Block]]] = {}
    additions = {}
    invalid_targets = set()
    seen_operations = set()
    seen_delta = set()
    for op in operations:
        if op.id in seen_operations:
            raise ValueError("duplicate operation ID")
        seen_operations.add(op.id)
        source = incoming.get(op.delta_block_id)
        problem = None
        if source is None:
            problem = "unknown_delta_block"
        elif counts[op.delta_block_id] > 1:
            problem = "duplicate_delta_operation"
        elif op.kind == "add":
            if (
                op.target_block_id is not None
                or op.target_hash is not None
                or op.target_quote is not None
            ):
                problem = "unexpected_add_target"
            elif op.replacement is not None and op.replacement != source.text:
                problem = "unanchored_replacement"
            else:
                additions[source.id] = source
        elif op.kind in {"replace", "delete"}:
            target_id = op.target_block_id
            target = current.get(target_id) if target_id else None
            if target_id is None or target is None:
                problem = "missing_or_reused_target"
            elif op.target_hash != target.hash:
                problem = "target_hash_mismatch"
            elif op.replacement is not None and op.replacement != source.text:
                problem = "unanchored_replacement"
            else:
                try:
                    quote = target_quote(source, target, op.target_quote)
                    start = target.text.index(quote)
                    plans.setdefault(target.id, []).append((start, start + len(quote), op, source))
                except ValueError as exc:
                    problem = str(exc)
        else:
            problem = "ambiguous_operation"
        if source is not None:
            seen_delta.add(source.id)
        if problem:
            problems[op.id] = problem
            if op.kind in {"replace", "delete"} and op.target_block_id:
                invalid_targets.add(op.target_block_id)
    resolved = []
    for target in baseline:
        changes = sorted(plans.get(target.id, []), key=lambda item: (item[0], item[1]))
        overlapping = any(
            left[1] > right[0] for left, right in zip(changes, changes[1:], strict=False)
        )
        if target.id in invalid_targets or overlapping:
            for _, _, op, _ in changes:
                problems[op.id] = "missing_or_reused_target"
            resolved.append(target)
            continue
        cursor = 0
        for start, end, op, source in changes:
            if start > cursor:
                resolved.append(fragment(target, cursor, start))
            if op.kind == "replace":
                resolved.append(source)
            cursor = end
        if cursor < len(target.text):
            resolved.append(fragment(target, cursor, len(target.text)))
    resolved.extend(additions.values())
    unresolved: list[dict[str, str | None]] = [
        {"operation_id": op.id, "delta_block_id": op.delta_block_id, "reason": problems[op.id]}
        for op in operations
        if op.id in problems
    ]
    for block in delta:
        if block.id not in seen_delta:
            unresolved.append(
                {"operation_id": None, "delta_block_id": block.id, "reason": "unclassified_delta"}
            )
    return EffectiveState(tuple(resolved), tuple(unresolved))
