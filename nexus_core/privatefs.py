"""Helpers for keeping NEXUS runtime data private on local filesystems."""

from __future__ import annotations

import os
import stat
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_directory(path: str | Path) -> Path:
    """Create a directory if needed and restrict it to the current user."""
    directory = Path(path)
    if directory.is_symlink():
        raise PermissionError(f"runtime directory must not be a symlink: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"runtime path is not a directory: {directory}")
    os.chmod(directory, PRIVATE_DIR_MODE, follow_symlinks=False)
    return directory


def ensure_private_file(path: str | Path) -> Path:
    """Create an owner-only regular file or restrict an existing one."""
    file_path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(file_path, flags, PRIVATE_FILE_MODE)
    except FileExistsError:
        metadata = file_path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"runtime file must be a regular file: {file_path}")
        os.chmod(file_path, PRIVATE_FILE_MODE, follow_symlinks=False)
    else:
        os.close(descriptor)
    return file_path


def secure_runtime_data_directory(path: str | Path) -> Path:
    """Restrict the data root and its immediate children without following links."""
    root = ensure_private_directory(path)
    for entry in root.iterdir():
        if entry.is_symlink():
            continue
        metadata = entry.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            ensure_private_directory(entry)
        elif stat.S_ISREG(metadata.st_mode):
            ensure_private_file(entry)
    return root
