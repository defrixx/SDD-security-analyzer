"""Consistent sensitive backups and bounded restore into atomic generations."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from .artifacts import ArtifactStore
from .catalog import canonical
from .inputs import parse
from .installation import Lease, active_data, switch_data
from .registry import Registry
from .stage_contracts import Contract
from .storage import SCHEMA_VERSION, Store
from .workers import bounded

MAX_TOTAL = 2 * 1024**3
MAX_FILES = 50000


class BackupFile(Contract):
    path: str = Field(pattern=r"^(database\.sqlite3|artifacts/[0-9a-f-]{36}/[0-9a-f]{64})$")
    byte_size: int = Field(ge=0, le=MAX_TOTAL)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BackupManifest(Contract):
    format_version: int = Field(ge=1, le=1)
    app_version: Literal["1.0.0"]
    schema_version: int = Field(ge=1, le=SCHEMA_VERSION)
    created_at: str
    sensitive: Literal[True]
    encrypted: Literal[False]
    secrets_included: Literal[False]
    files: list[BackupFile] = Field(min_length=1, max_length=MAX_FILES)


def file_hash(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TOTAL:
            raise ValueError("invalid_backup_file")
        checksum = hashlib.sha256()
        while part := stream.read(1024 * 1024):
            checksum.update(part)
    return info.st_size, checksum.hexdigest()


def maintenance(store: Store, enabled: bool, *, allow_abandoned=False):
    with store.connection:
        store.connection.execute("BEGIN IMMEDIATE")
        if (
            enabled
            and not allow_abandoned
            and store.connection.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running','waiting_approval') LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("finish_or_cancel_active_run_before_backup")
        store.connection.execute(
            "UPDATE instance_settings SET value=? WHERE key='maintenance'",
            ("true" if enabled else "false",),
        )


def archive_path(path: Path, root: Path):
    if (
        path.parent.absolute() != root.absolute()
        or path.name in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,150}\.tar\.gz", path.name)
    ):
        raise ValueError("backup_path_must_be_filename_under_configured_root")
    if root.is_symlink():
        raise ValueError("backup_root_symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)


def create_backup(installation: Path, output: Path, backup_root: Path, *, _maintenance_held=False):
    archive_path(output, backup_root)
    with (
        nullcontext()
        if _maintenance_held
        else Lease(installation, ".maintenance.lock", exclusive=True)
    ):
        data = active_data(installation)
        store = Store(data / "app.sqlite3")
        entered = False
        pending = output.parent / (".backup-" + uuid.uuid4().hex)
        try:
            maintenance(store, True, allow_abandoned=_maintenance_held)
            entered = True
            with tempfile.TemporaryDirectory(prefix=".backup-", dir=data) as temporary:
                database = Path(temporary) / "database.sqlite3"
                snapshot = sqlite3.connect(database)
                try:
                    store.connection.backup(snapshot)
                    if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("backup_database_integrity")
                    refs = snapshot.execute(
                        "SELECT DISTINCT project_id,sha256 FROM artifacts ORDER BY project_id,sha256"
                    ).fetchall()
                finally:
                    snapshot.close()
                os.chmod(database, 0o600)
                if len(refs) + 1 > MAX_FILES:
                    raise ValueError("backup_file_count_limit")
                files = [("database.sqlite3", database)]
                artifacts = ArtifactStore(data / "artifacts")
                for pid, checksum in refs:
                    artifacts.read(pid, checksum)  # verifies namespace, symlinks and content hash
                    files.append(
                        ("artifacts/" + pid + "/" + checksum, data / "artifacts" / pid / checksum)
                    )
                entries = []
                for name, path in files:
                    size, checksum = file_hash(path)
                    entries.append({"path": name, "byte_size": size, "sha256": checksum})
                if sum(item["byte_size"] for item in entries) > MAX_TOTAL:
                    raise ValueError("backup_total_limit")
                manifest = BackupManifest(
                    format_version=1,
                    app_version="1.0.0",
                    schema_version=SCHEMA_VERSION,
                    created_at=datetime.now(UTC).isoformat(),
                    sensitive=True,
                    encrypted=False,
                    secrets_included=False,
                    files=[BackupFile.model_validate(entry) for entry in entries],
                )
                descriptor = os.open(
                    pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(descriptor, "wb") as stream:
                    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                        body = canonical(manifest.model_dump())
                        item = tarfile.TarInfo("manifest.json")
                        item.size = len(body)
                        item.mode = 0o600
                        archive.addfile(item, io.BytesIO(body))
                        for name, path in files:
                            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                            with os.fdopen(descriptor, "rb") as source:
                                item = tarfile.TarInfo(name)
                                item.size = os.fstat(source.fileno()).st_size
                                item.mode = 0o600
                                archive.addfile(item, source)
                    stream.flush()
                    os.fsync(stream.fileno())
                # No overwrite, even if the destination was created after preview.
                os.link(pending, output, follow_symlinks=False)
                directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                return {
                    "filename": output.name,
                    "sha256": file_hash(output)[1],
                    "manifest": manifest.model_dump(),
                    "notice": "Sensitive documents and payloads; archive is NOT encrypted. Credentials/session tokens and mounted secret files are excluded.",
                }
        finally:
            if pending.exists():
                pending.unlink()
            try:
                if entered:
                    maintenance(store, False)
            finally:
                store.close()


def _extract(archive_path_value: str, staging_value: str):
    """Runs in bounded worker; no extract/extractall and no archive-controlled directories."""
    staging = Path(staging_value)
    descriptor = os.open(archive_path_value, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TOTAL:
            raise ValueError("archive_input_limit")
        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
            members = iter(archive)
            first = next(members, None)
            if (
                first is None
                or first.name != "manifest.json"
                or not first.isfile()
                or first.size > 8 * 1024**2
            ):
                raise ValueError("manifest_must_be_first_bounded_regular_file")
            manifest_stream = archive.extractfile(first)
            if manifest_stream is None:
                raise ValueError("missing_manifest_stream")
            manifest = BackupManifest.model_validate(
                parse(manifest_stream.read(), "json", catalog=True)
            )
            expected = {item.path: item for item in manifest.files}
            if (
                len(expected) != len(manifest.files)
                or "database.sqlite3" not in expected
                or sum(item.byte_size for item in manifest.files) > MAX_TOTAL
            ):
                raise ValueError("invalid_manifest_file_set")
            if (
                shutil.disk_usage(staging).free
                < sum(item.byte_size for item in manifest.files) + 64 * 1024**2
            ):
                raise ValueError("insufficient_restore_space")
            seen = set()
            for member in members:
                if (
                    member.name in seen
                    or member.name not in expected
                    or not member.isfile()
                    or member.size != expected[member.name].byte_size
                ):
                    raise ValueError("archive_member_rejected")
                seen.add(member.name)
                parts = member.name.split("/")
                if len(parts) > 1:
                    if str(uuid.UUID(parts[1])) != parts[1]:
                        raise ValueError("artifact_project_namespace")
                    directory = staging / "artifacts" / parts[1]
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination = staging.joinpath(*parts)
                checksum = hashlib.sha256()
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("missing_member_stream")
                descriptor = os.open(
                    destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(descriptor, "wb") as target:
                    count = 0
                    while part := source.read(1024 * 1024):
                        count += len(part)
                        if count > member.size:
                            raise ValueError("member_size_limit")
                        target.write(part)
                        checksum.update(part)
                    target.flush()
                    os.fsync(target.fileno())
                if count != member.size or checksum.hexdigest() != expected[member.name].sha256:
                    raise ValueError("backup_checksum_mismatch")
            if seen != set(expected):
                raise ValueError("missing_backup_members")
    return manifest.model_dump()


def schema_signature(connection):
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


def validate_database(path: Path, manifest: dict, staging: Path):
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA user_version").fetchone()[0] != manifest["schema_version"]:
            raise ValueError("backup_database_version_mismatch")
        if (
            connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or connection.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise ValueError("backup_database_integrity")
    finally:
        connection.close()
    store = Store(path)  # supported old versions migrate only inside staging
    try:
        golden = Store(staging / ".schema-reference.sqlite3")
        try:
            if schema_signature(store.connection) != schema_signature(golden.connection):
                raise ValueError("unexpected_database_schema")
        finally:
            golden.close()
        refs = {
            ("artifacts/" + pid + "/" + checksum)
            for pid, checksum in store.connection.execute(
                "SELECT DISTINCT project_id,sha256 FROM artifacts"
            )
        }
        if refs != {
            entry["path"] for entry in manifest["files"] if entry["path"].startswith("artifacts/")
        }:
            raise ValueError("backup_artifact_reference_coverage")
        for checksum, content in store.connection.execute("SELECT sha256,content FROM documents"):
            if hashlib.sha256(content).hexdigest() != checksum:
                raise ValueError("backup_document_hash")
        for checksum, content in store.connection.execute(
            "SELECT raw_sha256,raw_content FROM source_snapshots"
        ):
            if hashlib.sha256(content).hexdigest() != checksum:
                raise ValueError("backup_source_hash")
        # Frozen config/history is preserved. New heads are disabled; old grants are revoked.
        with store.connection:
            store.connection.execute(
                "UPDATE instance_settings SET value='false' WHERE key IN ('maintenance','allow_external_egress')"
            )
            store.connection.execute(
                "UPDATE instance_settings SET value=? WHERE key='bootstrap_configuration'",
                (canonical({"status": "restored_requires_explicit_import"}).decode(),),
            )
            store.connection.execute("DELETE FROM egress_grants")
            store.connection.execute("DELETE FROM capability_reports")
            store.connection.execute(
                "UPDATE runs SET status='interrupted',cancel_requested=0,error_code='restored_requires_explicit_review' WHERE status IN ('queued','running','waiting_approval')"
            )
        registry = Registry(store)
        for pid, kind, object_id, revision in store.connection.execute(
            "SELECT project_id,kind,id,revision FROM registry_heads WHERE kind IN ('connection','source','skill')"
        ).fetchall():
            original = registry.get(pid, kind, object_id, revision)
            content = original["content"] | {"enabled": False}
            if kind == "connection":
                content.update(auth="none", secret_ref=None, secret_headers={})
            elif kind == "source":
                content.update(auth="none", secret_ref=None)
            registry.save(pid, kind, content, object_id=object_id, expected_revision=revision)
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        store.close()
    for suffix in ("", "-wal", "-shm"):
        reference = staging / (".schema-reference.sqlite3" + suffix)
        if reference.exists():
            reference.unlink()


def _validate_database_worker(path: str, manifest: dict, staging: str):
    validate_database(Path(path), manifest, Path(staging))
    return True


def restore_backup(installation: Path, source: Path, backup_root: Path, *, replace_existing=False):
    archive_path(source, backup_root)
    installation.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (
        Lease(installation, ".service.lock", exclusive=True),
        Lease(installation, ".maintenance.lock", exclusive=True),
    ):
        data = active_data(installation)
        existing = (data / "app.sqlite3").exists()
        if existing and not replace_existing:
            raise ValueError("restore_target_not_empty_use_explicit_replace_with_backup")
        if not existing and any(
            item.name not in {".service.lock", ".maintenance.lock", ".generations"}
            for item in installation.iterdir()
        ):
            raise ValueError("restore_target_not_empty")
        generation = uuid.uuid4().hex
        parent = installation / ".generations"
        if parent.is_symlink():
            raise ValueError("generation_root_symlink")
        parent.mkdir(mode=0o700, exist_ok=True)
        staging = parent / generation
        staging.mkdir(mode=0o700)
        try:
            manifest = bounded(_extract, str(source), str(staging), timeout=120)
            (staging / "database.sqlite3").rename(staging / "app.sqlite3")
            bounded(
                _validate_database_worker,
                str(staging / "app.sqlite3"),
                manifest,
                str(staging),
                timeout=120,
            )
            # Validate first. Existing target is backed up before any active pointer changes.
            previous = None
            if existing:
                previous = backup_root / ("pre-restore-" + uuid.uuid4().hex + ".tar.gz")
                create_backup(installation, previous, backup_root, _maintenance_held=True)
            (staging / "restore-manifest.json").write_bytes(
                canonical(
                    {
                        "source_sha256": file_hash(source)[1],
                        "original_manifest": manifest,
                        "connections_disabled": True,
                        "secrets_require_reconfiguration": True,
                    }
                )
            )
            # Persist directory entries before publishing the generation pointer.
            for directory, _, names in os.walk(staging, followlinks=False):
                for name in names:
                    descriptor = os.open(Path(directory) / name, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            switch_data(installation, generation)
            return {
                "restored": True,
                "generation": generation,
                "files_verified": len(manifest["files"]),
                "previous_backup": None if previous is None else previous.name,
                "secrets_restored": False,
                "connections_enabled": False,
            }
        except BaseException:
            # Only this newly created, never-active generation is removed.
            if active_data(installation) != staging:
                shutil.rmtree(staging)
            raise
