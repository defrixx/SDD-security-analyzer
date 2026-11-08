"""Code-owned checks executed only inside the bounded validation boundary."""

from __future__ import annotations


class _BoundBuiltinSchema(dict):
    """In-memory proof from code-owned binding; JSON cannot supply this type."""

    def __init__(self, value: dict):
        from .catalog import digest

        super().__init__(value)
        self.bound_sha256 = digest(value)


def has_builtin_binding(schema: dict, checksum: str) -> bool:
    return type(schema) is _BoundBuiltinSchema and schema.bound_sha256 == checksum


def valid_structure(schema: object) -> bool:
    if not isinstance(schema, dict):
        return False
    stack = [(schema, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > 10000 or depth > 32:
            return False
        if isinstance(node, dict):
            if any(key in node for key in ("$id", "$dynamicRef", "$recursiveRef")):
                return False
            if "$ref" in node and (
                not isinstance(node["$ref"], str) or not node["$ref"].startswith("#/$defs/")
            ):
                return False
            stack.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node)
    return True


def check_definition(schema: object) -> bool:
    from jsonschema import Draft202012Validator, SchemaError

    if not valid_structure(schema):
        return False
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, RecursionError, OverflowError):
        return False
    return True
