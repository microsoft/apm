"""The per-user secret that authenticates verified-state records (QL D1, #4977).

32 random bytes in the APM config directory, created on first use, readable by
the current user only. A key that is not a regular file, not owned by the
current user, or readable by anyone else is no key: no record is trusted.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

KEY_BYTES = 32


def _private(path: Path, st: os.stat_result) -> bool:
    if not stat.S_ISREG(st.st_mode) or st.st_size != KEY_BYTES:
        return False
    if os.name == "nt":
        from apm_cli.install import verdict_key_windows

        return verdict_key_windows.private_to_current_user(path)
    return stat.S_IMODE(st.st_mode) == 0o600 and st.st_uid == os.geteuid()


def _create(path: Path) -> None:
    """Create empty, restrict, re-verify, then write the secret (W3d ordering)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.name == "nt":
            from apm_cli.install import verdict_key_windows

            if not verdict_key_windows.restrict_to_current_user(path):
                raise OSError("could not restrict the verdict key")
            if not verdict_key_windows.private_to_current_user(path):
                raise OSError("the verdict key is not private")
        os.write(fd, os.urandom(KEY_BYTES))
    finally:
        os.close(fd)


def load(path: Path, *, create: bool) -> bytes | None:
    """The key at *path*, or None when absent (and not created) or not private.

    The key is opened without following a final symlink and without blocking,
    and judged on the opened file (fstat), so a swap after the check is moot.
    """
    try:
        if not os.path.lexists(path):
            if not create:
                return None
            _create(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as handle:
            if not _private(path, os.fstat(handle.fileno())):
                return None
            key = handle.read(KEY_BYTES + 1)
    except (OSError, ValueError):
        return None
    return key if len(key) == KEY_BYTES else None
