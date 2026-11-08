"""A remote/custom JSON Schema cannot monopolize the API or orchestration thread."""

from __future__ import annotations

from .schema_definition import check_definition
from .workers import WorkerError, bounded, in_bounded_worker


class SchemaValidationUnavailable(ValueError):
    """Local validation did not complete; no conclusion about a payload is valid."""

    def __init__(self):
        super().__init__("local_schema_validation_unavailable")


def _validate(schema, value):
    from jsonschema import Draft202012Validator

    from .catalog import pointer

    if not check_definition(schema):
        raise ValueError("invalid_schema")
    issues = []
    for error in Draft202012Validator(schema).iter_errors(value):
        issue = {
            "path": pointer(error.absolute_path),
            "location": list(error.absolute_path)[:12],
            "keyword": error.validator,
        }
        if error.validator == "required" and isinstance(error.instance, dict):
            issue["missing_fields"] = [
                name for name in error.validator_value if name not in error.instance
            ][:20]
        issues.append(issue)
        if len(issues) >= 20:
            break
    return issues


def validate_definition(schema: object, *, timeout: float = 5):
    try:
        # Settings/catalogue parsers already run in a memory/time-bounded child.
        # Reuse that boundary; daemon children must not spawn grandchildren.
        valid = (
            check_definition(schema)
            if in_bounded_worker()
            else bounded(check_definition, schema, timeout=timeout)
        )
    except WorkerError as exc:
        raise SchemaValidationUnavailable() from exc
    if not valid:
        raise ValueError("invalid_schema")


def validate_payload(schema: dict, value: object):
    try:
        # Includes spawn/import overhead. Documentary parsing keeps its separate
        # 2-second bound; catalogue parsing/validation keeps its 15-second bound.
        return bounded(_validate, schema, value, timeout=5)
    except WorkerError as exc:
        raise SchemaValidationUnavailable() from exc
