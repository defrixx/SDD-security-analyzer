"""Secret values remain in process memory or an explicitly mounted file."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path

_JSON_ESCAPE = re.compile(rb'\\(?:u00([0-9a-fA-F]{2})|(["\\/bfnrt]))')
_SIMPLE_ESCAPES = {
    b'"': b'"',
    b"\\": b"\\",
    b"/": b"/",
    b"b": b"\b",
    b"f": b"\f",
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
}


def _decode_scan_escape(match: re.Match[bytes]) -> bytes:
    if match[1] is not None:
        value = int(match[1], 16)
        return bytes([value]) if value < 128 else match[0]
    return _SIMPLE_ESCAPES[match[2]]


class SecretReferenceError(ValueError):
    """Unavailable/invalid reference; never expose a credential or host path."""


class SecretStore:
    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}
        self.file_values: dict[tuple[str, str], str] = {}
        self.lock = threading.Lock()
        self.files = {}
        if os.environ.get("APP_SECRET_MODE", "session") == "files":
            configured = os.environ.get("APP_LLM_API_KEY_FILE", "")
            if configured:
                self.files["file:llm_api_key"] = Path(configured)

    def put(self, project_id: str, value: str) -> str:
        if not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError("invalid secret")
        reference = "session:" + str(uuid.uuid4())
        with self.lock:
            self.values[(project_id, reference)] = value
        return reference

    @staticmethod
    def header_credentials(
        headers: dict[str, str], names: Iterable[str], *, bearer: bool
    ) -> tuple[str, ...]:
        """Freeze credentials actually sent, including an in-flight retired key."""
        values = [headers[name] for name in names]
        if bearer:
            values.append(headers["Authorization"][len("Bearer ") :])
        return tuple(values)

    def reject_known(
        self, data: bytes, project_id: str | None = None, *, used_credentials: tuple[str, ...] = ()
    ):
        with self.lock:
            values = [
                value
                for (pid, _), value in (self.values | self.file_values).items()
                if project_id is None or pid == project_id
            ]
        values.extend(used_credentials)
        for value in values:
            # Model content is itself a JSON string inside the HTTP envelope.
            # Quotes/backslashes may therefore be escaped twice before the raw
            # response callback. Check bounded representations without decoding
            # or executing untrusted content; ordinary tokens need one check.
            candidate = value
            for _ in range(3):
                if candidate.encode() in data:
                    raise ValueError("credential_echo_rejected")
                escaped = json.dumps(candidate)[1:-1]
                if escaped == candidate:
                    break
                candidate = escaped
        # Scan a copy through bounded JSON escape layers; never alter stored
        # source bytes or invoke a general-purpose decoder on untrusted data.
        # Credentials are ASCII. Non-ASCII escapes and invalid bytes stay intact.
        scan = data
        needles = {value.encode() for value in values if value}
        if not needles:
            return
        for _ in range(3):
            decoded = _JSON_ESCAPE.sub(_decode_scan_escape, scan)
            if decoded == scan:
                break
            if any(value in decoded for value in needles):
                raise ValueError("credential_echo_rejected")
            scan = decoded

    def get(self, project_id: str, reference: str) -> str:
        with self.lock:
            if (project_id, reference) in self.values:
                return self.values[(project_id, reference)]
            if (project_id, reference) in self.file_values:
                return self.file_values[(project_id, reference)]
        path = self.files.get(reference)
        if path is not None:
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                raise SecretReferenceError("credential_reference_unavailable") from None
            if not resolved.is_relative_to(Path("/run/secrets")) or path.is_symlink():
                raise SecretReferenceError("credential_reference_unavailable")
            try:
                descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 4098:
                        raise SecretReferenceError("credential_reference_unavailable")
                    raw = stream.read(4099)
                if len(raw) > 4098:
                    raise SecretReferenceError("credential_reference_unavailable")
                value = raw.decode("ascii").strip()
            except (OSError, UnicodeError):
                raise SecretReferenceError("credential_reference_unavailable") from None
            if not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise SecretReferenceError("credential_reference_unavailable")
            with self.lock:
                self.file_values[(project_id, reference)] = value
            return value
        raise SecretReferenceError("credential_reference_unavailable")

    def remove(self, project_id: str, reference: str):
        with self.lock:
            self.values.pop((project_id, reference), None)

    def clear_project(self, project_id: str):
        with self.lock:
            self.values = {key: value for key, value in self.values.items() if key[0] != project_id}
            self.file_values = {
                key: value for key, value in self.file_values.items() if key[0] != project_id
            }

    def reload_file(self, project_id: str, reference: str):
        if reference not in self.files:
            raise ValueError("file secret reference unresolved")
        with self.lock:
            self.file_values.pop((project_id, reference), None)
