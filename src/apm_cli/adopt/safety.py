"""Bounded, non-following filesystem reads for onboarding."""

from __future__ import annotations

import stat
from pathlib import Path

from apm_cli.utils.path_security import ensure_path_within, has_symlink_component

MAX_ENTRIES = 2000
MAX_FILE_BYTES = 1024 * 1024
MAX_DEPTH = 16


def checked_path(path: Path, root: Path) -> Path:
    """Reject symlinks and escapes before inspecting a discovery path."""
    if has_symlink_component(root, path):
        raise ValueError("Symlinked paths are not onboarded; use a regular directory.")
    return ensure_path_within(path, root)


def checked_file(path: Path, root: Path) -> None:
    """Bound parser inputs and reject device files without opening them."""
    checked_path(path, root)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise ValueError("Not a regular file or exceeds the 1 MiB discovery read limit.")


def checked_tree(path: Path, root: Path) -> None:
    """Validate package trees without reading payloads or following symlinks."""
    checked_path(path, root)
    pending = [(path, 0)]
    count = 0
    while pending:
        directory, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise ValueError(
                "Package exceeds the discovery depth limit; prepare a smaller package."
            )
        for child in directory.iterdir():
            count += 1
            if count > MAX_ENTRIES:
                raise ValueError(
                    "Package exceeds the discovery entry limit; prepare a smaller package."
                )
            checked_path(child, root)
            if child.is_dir():
                pending.append((child, depth + 1))
            else:
                checked_file(child, root)
