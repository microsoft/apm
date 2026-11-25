"""Prove a hook file gone from the deploy root, or keep its claim (#4851).

Uninstall drops a removed file's lock claim after an aborted hook sync only
when the file is provably absent under the deploy root it pinned before
changing anything; anything it cannot prove keeps the claim.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


def no_follow_handles() -> bool:
    """Whether the platform opens directory handles that never follow a symlink."""
    return (
        os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )


def root_identity(deploy_root: Path) -> tuple[int, int] | None:
    """The deploy root's ``(st_dev, st_ino)``, pinned before uninstall changes anything."""
    try:
        status = os.stat(deploy_root)
    except OSError:
        return None
    return (status.st_dev, status.st_ino)


def absence_proven(deploy_root: Path, relative: str, *, pinned: tuple[int, int] | None) -> bool:
    """Return whether *relative* is provably gone under *deploy_root* (#4851).

    Absence counts only through real directories: a symlinked or
    non-directory parent, one that cannot be inspected, or a ``.``/``..``
    part leaves the file's fate unknown, so its claim stays. Each parent is
    opened as a no-follow directory handle and the next lookup goes through
    that handle, so a parent swapped for a symlink mid-walk cannot redirect
    it. The opened root must be the directory *pinned* before uninstall
    began (:func:`root_identity`), so a root reached through a swapped
    ancestor proves nothing. Without such handles (Windows, where a junction
    lstats as a directory) or a pinned identity, nothing is proven: the claim
    stays.
    """
    path = PurePosixPath(str(relative).replace("\\", "/"))
    parts = path.parts
    # An absolute or UNC-style ('//server/share') path escapes the deploy root.
    if pinned is None or not no_follow_handles() or path.is_absolute() or not parts:
        return False
    if ".." in parts:
        return False
    return _absence_proven_by_handle(deploy_root, parts, pinned)


def _absence_proven_by_handle(
    deploy_root: Path, parts: tuple[str, ...], pinned: tuple[int, int]
) -> bool:
    """:func:`absence_proven` through no-follow directory handles."""
    try:
        # The root as given, never resolved: a root that is (or became) a link proves nothing.
        handle = os.open(deploy_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        status = os.fstat(handle)
        if (status.st_dev, status.st_ino) != pinned:
            return False  # an ancestor now leads elsewhere
        for part in parts[:-1]:
            # A symlinked (ELOOP) or non-directory (ENOTDIR) parent raises OSError.
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
            os.close(handle)
            handle = child
        os.stat(parts[-1], dir_fd=handle, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    finally:
        os.close(handle)
    return False
