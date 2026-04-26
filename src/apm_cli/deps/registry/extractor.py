"""Archive extraction with sha256 verification.

Per docs/proposals/registry-api.md §5.2 and §6.1: the client MUST verify the
sha256 digest of the archive against the value advertised by ``GET /versions``
or recorded in the lockfile *before* extracting. A mismatch fails closed —
this is the only security-critical check on the install path.

Two archive formats are supported, dispatched by Content-Type from
``RegistryClient.download_archive``:

- ``application/gzip`` — gzipped tar (default APM ``apm pack`` output)
- ``application/zip``  — zip archive (Anthropic / open-claude-skills format)

The ``extract_archive`` dispatcher picks the right path and applies the same
security gates to both: no absolute paths, no path traversal, no symlinks or
hardlinks. The hash is checked against the raw bytes regardless of format —
a wrong-format guess produces a clean error, not a security issue.

Layout: archives are extracted into ``apm_modules/{owner}/{repo}/`` (the same
shape the Git resolver produces after ``git clone``).
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Optional


class HashMismatchError(Exception):
    """Raised when an archive's sha256 does not match the expected digest."""


class UnsafeTarballError(Exception):
    """Raised when an archive entry would escape the extraction root.

    Name kept for backward compat; covers both tar and zip cases.
    """


class UnknownArchiveFormatError(Exception):
    """Raised when the archive format can't be inferred from content type or magic bytes."""


# Archive-format magic-byte sniffers — used as a fallback when Content-Type
# isn't reliable. Both formats have unambiguous prefixes.
_GZIP_MAGIC = b"\x1f\x8b"
_ZIP_MAGIC = b"PK\x03\x04"


def _detect_format(data: bytes, content_type: Optional[str]) -> str:
    """Return ``"tar+gzip"`` or ``"zip"``. Content-Type wins; magic bytes are fallback."""
    if content_type:
        ct = content_type.lower().strip()
        if ct in ("application/gzip", "application/x-gzip", "application/x-tar+gzip"):
            return "tar+gzip"
        if ct in ("application/zip", "application/x-zip-compressed"):
            return "zip"
    # Fallback to magic bytes
    if data.startswith(_GZIP_MAGIC):
        return "tar+gzip"
    if data.startswith(_ZIP_MAGIC):
        return "zip"
    raise UnknownArchiveFormatError(
        f"cannot determine archive format from content_type={content_type!r}; "
        f"first bytes were {data[:8]!r}"
    )


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


def _safe_extract_zip(zf: zipfile.ZipFile, dest_root: Path) -> None:
    """Extract *zf* into *dest_root* with the same gates as the tar path.

    Reject absolute paths, path traversal, and any entry that would resolve
    outside *dest_root*. Symlinks in zip files are encoded as a Unix mode bit
    (S_IFLNK in ``external_attr``) — we reject those too.
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    for info in zf.infolist():
        # Symlinks in zip: high 16 bits of external_attr carry Unix mode.
        # 0xA000 == S_IFLNK. Refuse to extract any symlink-typed entry.
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and (unix_mode & 0xF000) == 0xA000:
            raise UnsafeTarballError(
                f"unsupported zip entry type (symlink): {info.filename!r}"
            )
        target = _safe_member_path(info.filename, dest_root)
        if target is None:
            continue
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Stream content explicitly so we never call zf.extract() with the
        # raw filename (avoids any zip-slip surprises in older Pythons).
        with zf.open(info, "r") as src, open(target, "wb") as fh:
            while True:
                chunk = src.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        # Preserve mode bits if present, dropping setuid/setgid/sticky.
        if unix_mode:
            os.chmod(target, unix_mode & 0o755)


def extract_zip(
    data: bytes,
    expected_digest: str,
    dest_root: Path,
) -> str:
    """Verify *data*'s sha256 then extract its zip contents into *dest_root*.

    Returns the actual hex digest of *data* on success. Raises
    ``HashMismatchError`` on mismatch, or ``UnsafeTarballError`` (name kept
    for backward compat) if any member would escape *dest_root*.
    """
    actual = verify_sha256(data, expected_digest)
    import io  # local import — only needed on the install path

    try:
        with zipfile.ZipFile(io.BytesIO(data), mode="r") as zf:
            _safe_extract_zip(zf, Path(dest_root))
    except zipfile.BadZipFile as exc:
        raise UnknownArchiveFormatError(f"malformed zip archive: {exc}") from exc
    return actual


def extract_archive(
    data: bytes,
    expected_digest: str,
    dest_root: Path,
    *,
    content_type: Optional[str] = None,
) -> str:
    """Dispatcher: pick the right extractor based on Content-Type / magic bytes.

    The hash check happens identically for both formats. A mismatch fails
    before extraction even starts; format-detection errors fail before any
    bytes are written.
    """
    fmt = _detect_format(data, content_type)
    if fmt == "tar+gzip":
        return extract_tarball(data, expected_digest, dest_root)
    if fmt == "zip":
        return extract_zip(data, expected_digest, dest_root)
    # _detect_format raises UnknownArchiveFormatError otherwise; this is
    # belt-and-braces.
    raise UnknownArchiveFormatError(f"unsupported archive format: {fmt!r}")
