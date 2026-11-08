"""Bounded, non-executing parsers for user-supplied structured input."""

from __future__ import annotations

import json
import math

MAX_BYTES = 10 * 1024 * 1024
MAX_NODES = 100_000
MAX_DEPTH = 64
MAX_STRING = 256 * 1024


class InputError(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate key: " + str(key))
        result[key] = value
    return result


def _check(value, max_nodes=MAX_NODES):
    stack = [(value, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > max_nodes or depth > MAX_DEPTH:
            raise InputError("structure limit exceeded")
        if isinstance(node, dict):
            if not all(isinstance(key, str) for key in node):
                raise InputError("object keys must be strings")
            if any(len(key) > MAX_STRING for key in node):
                raise InputError("string limit exceeded")
            stack.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)
        elif node is not None and type(node) not in (str, int, float, bool):
            raise InputError("unsupported value type")
        elif isinstance(node, str) and len(node) > MAX_STRING:
            raise InputError("string limit exceeded")
        elif isinstance(node, float) and not math.isfinite(node):
            raise InputError("non-finite number")
    return value


def parse(data: bytes, fmt: str, *, catalog: bool = False):
    limit = 32 * 1024 * 1024 if catalog else 1024 * 1024
    max_nodes = 2_000_000 if catalog else MAX_NODES
    if len(data) > limit:
        raise InputError("file size limit exceeded")
    try:
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise InputError("NUL characters are forbidden")
        if fmt == "json":
            # Scan structural characters outside strings before allocating the
            # object tree. The byte limit bounds tokens and scalar allocation.
            depth = 0
            quoted = escaped = False
            for char in text:
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char in "[{":
                    depth += 1
                    if depth > MAX_DEPTH:
                        raise InputError("structure limit exceeded")
                elif char in "]}":
                    depth -= 1
            value = json.loads(
                text,
                object_pairs_hook=_pairs,
                parse_constant=lambda x: (_ for _ in ()).throw(InputError("non-finite number")),
            )
        elif fmt in {"yaml", "yml"}:
            try:
                import yaml
            except ImportError as exc:
                raise InputError("YAML support is unavailable") from exc

            class Loader(yaml.SafeLoader):
                node_count = 0
                node_depth = 0

                def compose_node(self, parent, index):
                    self.node_count += 1
                    self.node_depth += 1
                    if self.node_count > max_nodes or self.node_depth > MAX_DEPTH:
                        raise InputError("structure limit exceeded")
                    if self.check_event(yaml.AliasEvent):
                        raise InputError("YAML aliases are forbidden")
                    event = self.peek_event()
                    if (
                        getattr(event, "anchor", None) is not None
                        or getattr(event, "tag", None) is not None
                    ):
                        raise InputError("YAML anchors and tags are forbidden")
                    try:
                        return super().compose_node(parent, index)
                    finally:
                        self.node_depth -= 1

                def construct_mapping(self, node, deep=False):
                    if any(k.value == "<<" for k, _ in node.value):
                        raise InputError("YAML merge keys are forbidden")
                    return _pairs(
                        [
                            (
                                self.construct_object(k, deep=deep),
                                self.construct_object(v, deep=deep),
                            )
                            for k, v in node.value
                        ]
                    )

            value = yaml.load(text, Loader=Loader)
        else:
            raise InputError("unsupported format")
        return _check(value, max_nodes)
    except InputError:
        raise
    except Exception as exc:
        raise InputError("invalid structured input") from exc
