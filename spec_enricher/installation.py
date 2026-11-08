"""Local lifecycle locks and atomic, code-owned data-generation selection."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path


class Lease:
    def __init__(self, root: Path, name: str, *, exclusive: bool):
        import fcntl

        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink():
            raise ValueError("installation_root_symlink")
        self.descriptor: int | None = os.open(
            root / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        if not stat.S_ISREG(os.fstat(self.descriptor).st_mode):
            self.close()
            raise ValueError("invalid_lifecycle_lock")
        try:
            fcntl.flock(
                self.descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            )
        except OSError:
            self.close()
            raise ValueError("lifecycle_operation_already_active") from None

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def active_data(root: Path) -> Path:
    pointer = root / "CURRENT"
    if not pointer.exists():
        if pointer.is_symlink():
            raise ValueError("invalid_generation_pointer")
        return root
    descriptor = os.open(pointer, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        generation = stream.read(128).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ValueError("invalid_generation_pointer")
    parent = root / ".generations"
    selected = parent / generation
    if parent.is_symlink() or selected.is_symlink() or not selected.is_dir():
        raise ValueError("invalid_data_generation")
    return selected


def switch_data(root: Path, generation: str):
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ValueError("invalid_generation_id")
    temporary = root / (".CURRENT-" + generation)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(generation + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, root / "CURRENT")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
