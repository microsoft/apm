"""Tarball extraction with sha256 verification.

Per docs/proposals/registry-api.md §5.2 and §6.1: the client MUST verify the
sha256 digest of the tarball against the value advertised by ``GET /versions``
or recorded in the lockfile *before* extracting. A mismatch fails closed —
this is the only security-critical check on the install path.

Layout: tarballs are extracted into ``apm_modules/{owner}/{repo}/`` (the same
shape the Git resolver produces after ``git clone``). Path-traversal entries
are rejected — see ``_safe_extract``.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
from pathlib import Path
from typing import Optional


class HashMismatchError(Exception):
    """Raised when a tarball's sha256 does not match the expected digest."""


class UnsafeTarballError(Exception):
    """Raised when a tarball entry would escape the extraction root."""


def _normalize_digest(digest: str) -> str:
    """Strip the ``sha256:`` / ``sha256=`` prefix if present and lowercase."""
    s = digest.strip().lower()
    for prefix in ("sha256:", "sha256="):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def verify_sha256(data: bytes, expected_digest: str) -> str:
    """Verify *data*'s sha256 matches *expected_digest*.

    Accepts the digest with or without a ``sha256:`` / ``sha256=`` prefix.
    Returns the actual hex digest on success; raises ``HashMismatchError`` on
    mismatch.
    """
    actual = hashlib.sha256(data).hexdigest()
    expected = _normalize_digest(expected_digest)
    if actual != expected:
        raise HashMismatchError(
            f"tarball sha256 mismatch: expected {expected}, got {actual}"
        )
    return actual


def _safe_member_path(member_name: str, dest_root: Path) -> Optional[Path]:
    """Return the absolute extraction path for *member_name* if safe.

    Rejects:
    - Absolute paths (``/etc/passwd``)
    - Path traversal via ``..`` segments
    - Symlink-style escapes (caller should also reject symlinks via type check)

    Returns ``None`` if the member should be skipped (empty name).
    """
    if not member_name or member_name in (".", "/"):
        return None
    # Tarball member names use forward slashes regardless of platform; reject
    # anything that looks like an absolute path on either side.
    if member_name.startswith(("/", "\\")) or (
        len(member_name) >= 2 and member_name[1] == ":"
    ):
        raise UnsafeTarballError(
            f"absolute path in tarball: {member_name!r}"
        )
    candidate = (dest_root / member_name).resolve()
    dest_resolved = dest_root.resolve()
    try:
        candidate.relative_to(dest_resolved)
    except ValueError as exc:
        raise UnsafeTarballError(
            f"tarball entry {member_name!r} escapes extraction root"
        ) from exc
    return candidate


def _safe_extract(tar: tarfile.TarFile, dest_root: Path) -> None:
    """Extract *tar* into *dest_root* with traversal/symlink rejection."""
    dest_root.mkdir(parents=True, exist_ok=True)
    for member in tar.getmembers():
        # Reject device files, FIFOs, symlinks, hard links — keep extraction
        # to plain files and dirs only. Symlinks are rejected because they
        # are the simplest path-traversal vector inside a tarball.
        if member.isdev() or member.issym() or member.islnk():
            raise UnsafeTarballError(
                f"unsupported tarball entry type: {member.name!r}"
            )
        target = _safe_member_path(member.name, dest_root)
        if target is None:
            continue
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Stream the file contents through tarfile's extractor, but write to
        # the verified path explicitly so we never call extract() with the
        # raw member name (which is what would honor a symlink).
        src = tar.extractfile(member)
        if src is None:
            continue
        with open(target, "wb") as fh:
            while True:
                chunk = src.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        # Preserve mode bits but drop setuid/setgid/sticky for safety.
        os.chmod(target, member.mode & 0o755)


def extract_tarball(
    data: bytes,
    expected_digest: str,
    dest_root: Path,
) -> str:
    """Verify *data*'s sha256 then extract its gzipped tar contents into *dest_root*.

    Returns the actual hex digest of *data* on success. Raises
    ``HashMismatchError`` if the digest doesn't match, or
    ``UnsafeTarballError`` if any member would escape *dest_root*.
    """
    actual = verify_sha256(data, expected_digest)
    import io  # local import — only needed on the install path

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        _safe_extract(tar, Path(dest_root))
    return actual
