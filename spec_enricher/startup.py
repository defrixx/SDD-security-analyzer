"""First-initialization config import; never overwrites saved configuration."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .catalog import canonical, digest
from .settings import import_settings, parse_bundle
from .storage import Store

MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def _record(store: Store, value: dict):
    with store.connection:
        store.connection.execute(
            "UPDATE instance_settings SET value=? WHERE key='bootstrap_configuration'",
            (canonical(value).decode(),),
        )


def _read(config_dir: Path) -> bytes | None:
    try:
        directory = os.open(config_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(
                "settings-bundle.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BUNDLE_BYTES:
                raise ValueError("bootstrap_configuration_file_rejected")
            raw = stream.read(MAX_BUNDLE_BYTES + 1)
            if len(raw) > MAX_BUNDLE_BYTES:
                raise ValueError("bootstrap_configuration_file_rejected")
            return raw
    finally:
        os.close(directory)


def initialize(store: Store, config_dir: Path):
    state = json.loads(
        store.connection.execute(
            "SELECT value FROM instance_settings WHERE key='bootstrap_configuration'"
        ).fetchone()[0]
    )
    if state["status"] != "pending":
        return
    checksum = state.get("bundle_sha256")
    # Recover a crash between the atomic import/receipt commit and completion.
    if (
        checksum
        and store.connection.execute(
            "SELECT 1 FROM settings_imports WHERE bundle_sha256=?", (checksum,)
        ).fetchone()
    ):
        _record(store, {"status": "imported", "bundle_sha256": checksum})
        return
    try:
        raw = _read(config_dir)
        if raw is None:
            if checksum:
                raise ValueError("bootstrap_configuration_changed_during_initialization")
            _record(store, {"status": "empty"})
            return
        bundle = parse_bundle(raw)
        current = digest(bundle)
        if checksum and current != checksum:
            raise ValueError("bootstrap_configuration_changed_during_initialization")
        _record(store, {"status": "pending", "bundle_sha256": current})
        import_settings(store, bundle)
        _record(store, {"status": "imported", "bundle_sha256": current})
    except (ValueError, OSError):
        # No data-bearing parser/OS messages or input bytes in startup logs.
        raise ValueError(
            "bootstrap_configuration_rejected: check read-only /config/settings-bundle.json; "
            "existing data retained; correct the file and restart"
        ) from None
