"""TOCTOU-safe byte reads for integrator content-identity checks.

Extracted from ``base_integrator`` so the security-sensitive symlink-race
primitive lives in one cohesive module. ``base_integrator`` re-exports both
names, so ``apm_cli.integration.base_integrator._read_bytes_no_follow`` and
``_SymlinkRaceError`` remain importable / patchable from their original path.
"""

import errno
import os
from pathlib import Path


class _SymlinkRaceError(OSError):
    """Raised by ``_read_bytes_no_follow`` when the path becomes a symlink
    between the pre-check and the open(). Caught locally; never bubbles."""


def _read_bytes_no_follow(path: Path) -> bytes:
    """Read *path* with ``O_NOFOLLOW`` semantics where supported.

    On POSIX, opens the file with ``os.O_NOFOLLOW`` so the kernel
    rejects the open atomically if the final path component is a
    symlink. This closes the TOCTOU race between
    ``Path.is_symlink()`` and ``Path.read_bytes()`` exploited by a
    co-tenant who can swap files for symlinks.

    On Windows (no ``O_NOFOLLOW``), falls back to a plain read; the
    caller's upfront ``is_symlink()`` check plus ``ensure_path_within``
    at the integrator call sites provide the containment guarantee.
    """
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags |= nofollow
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        # ELOOP is the canonical errno for "O_NOFOLLOW refused to open
        # a symlink"; some Linux kernels return EMLINK or ELOOP-equivalent.
        if nofollow and exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise _SymlinkRaceError(exc.errno, f"Refused to follow symlink at {path}") from exc
        raise
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
