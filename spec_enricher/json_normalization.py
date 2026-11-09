"""One bounded syntax-only normalization for an explicit native fact wire contract.

This is not a general JSON repairer and does not establish model intent. It never
edits strings, values or identifiers. Full schema and semantic checks remain the
caller's responsibility after the unique strict parse succeeds.
"""

import json
import re

from .inputs import InputError, parse

MAX_BYTES = 64 * 1024
MAX_CANDIDATES = 32


def missing_array_object_open(content: str):
    if len(content.encode("utf-8")) > MAX_BYTES or not content.lstrip().startswith("{"):
        return None
    # Tokenize strings as indivisible values, so quoted punctuation cannot
    # create a candidate. No Markdown fences or simultaneous normalizations.
    tokens = []
    pos = 0
    try:
        while pos < len(content):
            char = content[pos]
            if char.isspace():
                pos += 1
                continue
            start = pos
            if char == '"':
                _, pos = json.decoder.scanstring(content, pos + 1, True)
                tokens.append(("string", start))
            elif char in "{}[],:":
                tokens.append((char, start))
                pos += 1
            else:
                match = re.match(r"(?:true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)", content[pos:])
                if not match:
                    return None
                tokens.append(("scalar", start))
                pos += len(match[0])
    except (ValueError, UnicodeError):
        return None
    stack, candidates = [], []
    for index, (kind, offset) in enumerate(tokens):
        if (kind == "string" and stack and stack[-1] == "["
                and index and tokens[index - 1][0] in {"[", ","}
                and index + 1 < len(tokens) and tokens[index + 1][0] == ":"):
            candidates.append(offset)
            if len(candidates) > MAX_CANDIDATES:
                return None
        if kind in {"{", "["}:
            stack.append(kind)
            if len(stack) > 32:
                return None
        elif kind in {"}", "]"}:
            if not stack or stack[-1] != ("{" if kind == "}" else "["):
                # The absent opener's closing delimiter is the first mismatch.
                # A later candidate cannot fix this already-invalid prefix.
                break
            stack.pop()
    accepted = None
    for offset in candidates:
        candidate = content[:offset] + "{" + content[offset:]
        try:
            # Cheap prefilter only; the existing bounded strict parser remains
            # authoritative for duplicate keys, depth, numbers and full input.
            json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        try:
            value = parse(candidate.encode(), "json", catalog=True)
        except InputError:
            continue
        if not isinstance(value, dict):
            continue
        if accepted is not None:
            return None
        accepted = (value, f"single_missing_array_object_open_brace:char_offset={offset}")
    return accepted
