"""Short-lived bounded parser process; never handles model or tool execution."""

from __future__ import annotations

from .i18n import translate
from .inputs import InputError
from .workers import WorkerError, bounded


def _parse(data: bytes, fmt: str, catalog: bool):
    from .inputs import parse

    value = parse(data, fmt, catalog=catalog)
    issues = []
    if catalog:
        from .catalog import validate

        issues = validate(value)
    return value, issues


def parse_bounded(data: bytes, fmt: str, *, catalog: bool = False):
    limit = 32 * 1024 * 1024 if catalog else 1024 * 1024
    if len(data) > limit:
        raise InputError("file size limit exceeded")
    try:
        return bounded(_parse, data, fmt, catalog, timeout=15 if catalog else 2)
    except WorkerError as exc:
        raise InputError(
            translate("Файл отклонён: формат, лимиты или схема не поддерживаются.")
        ) from exc
