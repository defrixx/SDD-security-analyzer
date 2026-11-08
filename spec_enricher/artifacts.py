"""Project-scoped, hash-addressed immutable artifacts, written atomically."""

from __future__ import annotations

import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from .snapshots import sha256

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink():
            raise ValueError("artifact root cannot be a symlink")

    @contextmanager
    def namespace(self, project_id: str, *, exclusive=False):
        """Order: namespace lease, then SQLite transaction, for writers and GC."""
        import fcntl

        with self.directory(project_id) as directory:
            descriptor = os.open(
                ".io.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                yield
            finally:
                os.close(descriptor)

    @contextmanager
    def directory(self, project_id: str):
        if str(uuid.UUID(project_id)) != project_id:
            raise ValueError("invalid project namespace")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = os.open(self.root, flags)
        project = None
        try:
            try:
                os.mkdir(project_id, mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            project = os.open(project_id, flags, dir_fd=root)
            yield project
        finally:
            if project is not None:
                os.close(project)
            os.close(root)

    def put(self, project_id: str, data: bytes) -> str:
        with self.namespace(project_id):
            return self._put(project_id, data)

    def _put(self, project_id: str, data: bytes) -> str:
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact size limit exceeded")
        checksum = sha256(data)
        with self.directory(project_id) as directory:
            temporary = ".pending-" + str(uuid.uuid4())
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temporary,
                        checksum,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    if self._read(directory, checksum) != data:
                        raise ValueError("artifact hash conflict") from None
                os.fsync(directory)
            finally:
                os.unlink(temporary, dir_fd=directory)
        return checksum

    def _read(self, directory: int, checksum: str) -> bytes:
        descriptor = os.open(checksum, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
                raise ValueError("invalid artifact file")
            data = stream.read(MAX_ARTIFACT_BYTES + 1)
        if sha256(data) != checksum:
            raise ValueError("artifact checksum mismatch")
        return data

    def read(self, project_id: str, checksum: str) -> bytes:
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("invalid artifact checksum")
        with self.namespace(project_id), self.directory(project_id) as directory:
            return self._read(directory, checksum)
