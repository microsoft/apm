"""Download, write, and safely extract archive packages (zip / tar.gz).

Generic archive helpers, decoupled from marketplace internals. URL-sourced
installers and local bundle/archive paths reuse the same zip-slip,
path-traversal, and decompression-bomb guards.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tarfile
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import IO, TypeVar
from urllib.parse import urlparse

import requests

from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)

MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_UNCOMPRESSED = 512 * 1024 * 1024
SUPPORTED_ARCHIVE_FORMATS = frozenset({"zip", "tar.gz"})

_MAX_UNCOMPRESSED_BYTES = MAX_ZIP_UNCOMPRESSED
_MAX_ARCHIVE_DOWNLOAD_BYTES = _MAX_UNCOMPRESSED_BYTES
_COPY_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_ARCHIVE_SESSION = requests.Session()
_ARCHIVE_SESSION.max_redirects = 5

_ErrorT = TypeVar("_ErrorT", bound=Exception)


class ArchiveError(Exception):
    """Raised when an archive cannot be downloaded or extracted safely."""


def validate_archive_format(archive_format: str) -> None:
    """Raise ValueError unless *archive_format* is supported."""
    if archive_format not in SUPPORTED_ARCHIVE_FORMATS:
        raise ValueError(f"Unknown archive_format: {archive_format!r}. Must be 'zip' or 'tar.gz'.")


def projected_archive_path(output_dir: Path, bundle_name: str, archive_format: str) -> Path:
    """Return the archive path that pack would write for *bundle_name*."""
    validate_archive_format(archive_format)
    suffix = ".tar.gz" if archive_format == "tar.gz" else ".zip"
    return output_dir / f"{bundle_name}{suffix}"


def write_tar_archive(bundle_dir: Path, archive_path: Path) -> None:
    """Write *bundle_dir* to a gzipped tar archive, excluding symlinks."""
    ensure_path_within(archive_path, archive_path.parent)
    with tarfile.open(archive_path, "w:gz") as tf:
        for fp in sorted(bundle_dir.rglob("*")):
            if fp.is_symlink() or not fp.is_file():
                continue
            tf.add(fp, arcname=f"{bundle_dir.name}/{fp.relative_to(bundle_dir).as_posix()}")


def write_zip_archive(bundle_dir: Path, archive_path: Path) -> None:
    """Write *bundle_dir* to a compressed zip archive, excluding symlinks."""
    ensure_path_within(archive_path, archive_path.parent)
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as zf:
        for fp in sorted(bundle_dir.rglob("*")):
            if fp.is_symlink() or not fp.is_file():
                continue
            zf.write(fp, arcname=f"{bundle_dir.name}/{fp.relative_to(bundle_dir).as_posix()}")


def _raise(error_type: type[_ErrorT], message: str) -> None:
    raise error_type(message)


def _copy_member_within_limit(src: IO[bytes], dst: IO[bytes], running_total: int) -> int:
    """Stream *src* into *dst*, enforcing the cumulative uncompressed cap.

    Counts the actual bytes read from the decompressed member stream and aborts
    mid-stream once the running total exceeds the cap, rather than trusting
    archive header-declared member sizes.
    """
    total = running_total
    while True:
        chunk = src.read(_COPY_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_UNCOMPRESSED_BYTES:
            raise ArchiveError(f"Archive exceeds size limit of {_MAX_UNCOMPRESSED_BYTES} bytes")
        dst.write(chunk)
    return total


def _check_archive_member(member_path: str) -> None:
    """Validate one archive member path before extraction."""
    if "\x00" in member_path:
        raise ArchiveError(f"Archive member path contains null byte: {member_path!r}")
    if os.path.isabs(member_path):
        raise ArchiveError(f"Archive member has absolute path: {member_path!r}")
    normalized = member_path.replace("\\", "/")
    if normalized.startswith("//") or (
        len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha()
    ):
        raise ArchiveError(f"Archive member has absolute path: {member_path!r}")
    try:
        validate_path_segments(member_path, context="archive member")
    except PathTraversalError as exc:
        raise ArchiveError(str(exc)) from exc


def _detect_archive_format(content_type: str, url: str) -> str:
    """Return ``tar.gz`` or ``zip`` from Content-Type or URL extension."""
    media_type = content_type.lower().split(";", 1)[0].strip()
    if media_type == "application/x-tar":
        raise ArchiveError(
            "Uncompressed tar archives are not supported; "
            "only gzip-compressed tarballs (.tar.gz) and zip archives are supported"
        )
    if media_type in {"application/gzip", "application/x-gzip"}:
        return "tar.gz"
    if media_type in {"application/zip", "application/x-zip-compressed"}:
        return "zip"

    lower_url = url.lower().split("?", 1)[0]
    if lower_url.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if lower_url.endswith(".zip"):
        return "zip"
    raise ArchiveError(
        f"Cannot determine archive format from Content-Type={content_type!r} and URL={url!r}"
    )


def _safe_destination(dest_dir: str, member_name: str) -> Path:
    """Return a contained destination path for an archive member."""
    destination_root = Path(dest_dir)
    destination = destination_root / member_name
    try:
        return ensure_path_within(destination, destination_root)
    except PathTraversalError as exc:
        raise ArchiveError(
            f"Archive member would extract outside destination: {member_name!r}"
        ) from exc


def _zip_member_target(
    member_name: str,
    dest_root: Path,
    *,
    error_type: type[_ErrorT],
) -> Path | None:
    """Return a safe extraction target for a zip member, or None for empty dirs."""
    if not member_name or member_name in (".", "/"):
        return None
    if (
        member_name.startswith(("/", "\\"))
        or PureWindowsPath(member_name).drive
        or PureWindowsPath(member_name).is_absolute()
    ):
        _raise(error_type, f"Refusing to extract path-traversal entry: {member_name}")
    try:
        validate_path_segments(member_name, context="zip member")
    except PathTraversalError:
        _raise(error_type, f"Refusing to extract path-traversal entry: {member_name}")
    target = dest_root / member_name
    try:
        ensure_path_within(target, dest_root)
    except PathTraversalError:
        _raise(error_type, f"Refusing to extract path-traversal entry: {member_name}")
    return target


def safe_extract_zip(
    zf: zipfile.ZipFile,
    dest_root: Path,
    *,
    max_entries: int = MAX_ZIP_ENTRIES,
    max_uncompressed: int = MAX_ZIP_UNCOMPRESSED,
    error_type: type[_ErrorT] = ValueError,
    member_name_transform: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Safely stream-extract *zf* under *dest_root* with zip-bomb limits.

    The uncompressed-size limit is enforced against bytes actually read from
    each entry, not against attacker-controlled ZipInfo.file_size metadata.
    ``member_name_transform`` can strip or reject names before validation; the
    returned list contains the transformed member names that were written.
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    members = zf.infolist()
    if len(members) > max_entries:
        _raise(error_type, f"ZIP archive has {len(members)} entries (limit {max_entries})")

    extracted: list[str] = []
    total_uncompressed = 0
    for info in members:
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and (unix_mode & 0xF000) == 0xA000:
            _raise(error_type, f"Refusing to extract symlink: {info.filename}")
        member_name = info.filename
        if member_name_transform is not None:
            member_name = member_name_transform(member_name)
        if member_name is None:
            continue
        target = _zip_member_target(member_name, dest_root, error_type=error_type)
        if target is None:
            continue
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info, "r") as src, open(target, "wb") as fh:
            while True:
                chunk = src.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                next_total = total_uncompressed + len(chunk)
                if next_total > max_uncompressed:
                    limit_mb = max_uncompressed // (1024 * 1024)
                    actual_mb = next_total // (1024 * 1024)
                    _raise(
                        error_type,
                        f"ZIP archive uncompressed size exceeds size limit: {actual_mb} MB > {limit_mb} MB",
                    )
                fh.write(chunk)
                total_uncompressed = next_total
        if unix_mode:
            os.chmod(target, unix_mode & 0o755)
        extracted.append(member_name)
    return extracted


def _extract_tar_archive(archive: tarfile.TarFile, dest_dir: str) -> list[str]:
    """Extract an opened tar archive into *dest_dir* with safety checks."""
    extracted: list[str] = []
    total_size = 0
    for member in archive.getmembers():
        if member.isdir():
            continue
        if member.issym() or member.islnk():
            raise ArchiveError(f"Symlinks and hard links are not supported: {member.name!r}")
        if not member.isreg():
            continue
        _check_archive_member(member.name)
        destination = _safe_destination(dest_dir, member.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        src = archive.extractfile(member)
        if src is None:
            continue
        with src, open(destination, "wb") as dst:
            total_size = _copy_member_within_limit(src, dst, total_size)
        extracted.append(member.name)
    return extracted


def _extract_tar_gz(data: bytes, dest_dir: str) -> list[str]:
    """Extract a tar.gz archive into *dest_dir* with safety checks."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            return _extract_tar_archive(archive, dest_dir)
    except tarfile.TarError as exc:
        raise ArchiveError(f"Failed to read tar.gz archive: {exc}") from exc


def _extract_tar_gz_file(path: Path, dest_dir: str) -> list[str]:
    """Extract a tar.gz archive file into *dest_dir* with safety checks."""
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            return _extract_tar_archive(archive, dest_dir)
    except tarfile.TarError as exc:
        raise ArchiveError(f"Failed to read tar.gz archive: {exc}") from exc


def _extract_zip_archive(archive: zipfile.ZipFile, dest_dir: str) -> list[str]:
    """Extract an opened zip archive into *dest_dir* with safety checks."""
    return safe_extract_zip(
        archive,
        Path(dest_dir),
        max_entries=MAX_ZIP_ENTRIES,
        max_uncompressed=_MAX_UNCOMPRESSED_BYTES,
        error_type=ArchiveError,
    )


def _extract_zip(data: bytes, dest_dir: str) -> list[str]:
    """Extract a zip archive into *dest_dir* with safety checks."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _extract_zip_archive(archive, dest_dir)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"Failed to read zip archive: {exc}") from exc


def _extract_zip_file(path: Path, dest_dir: str) -> list[str]:
    """Extract a zip archive file into *dest_dir* with safety checks."""
    try:
        with zipfile.ZipFile(path) as archive:
            return _extract_zip_archive(archive, dest_dir)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"Failed to read zip archive: {exc}") from exc


def _stream_download_to_file(response: object, output_path: Path, url: str) -> None:
    """Stream an HTTP response body to *output_path* with a compressed-byte cap."""
    content_length = getattr(response, "headers", {}).get("Content-Length", "")
    if content_length:
        with contextlib.suppress(ValueError):
            if int(content_length) > _MAX_ARCHIVE_DOWNLOAD_BYTES:
                raise ArchiveError(
                    f"Archive download exceeds size limit of {_MAX_ARCHIVE_DOWNLOAD_BYTES} bytes"
                )

    total = 0
    with open(output_path, "wb") as dst:
        for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_ARCHIVE_DOWNLOAD_BYTES:
                raise ArchiveError(
                    f"Archive download exceeds size limit of {_MAX_ARCHIVE_DOWNLOAD_BYTES} bytes"
                )
            dst.write(chunk)
    if total == 0:
        raise ArchiveError(f"Archive download from {url!r} returned an empty body")


def _archive_get(url: str, **kwargs: object):
    """Issue archive HTTP GET through a cookie-cleared session."""
    _ARCHIVE_SESSION.cookies.clear()
    response = _ARCHIVE_SESSION.get(url, **kwargs)
    _ARCHIVE_SESSION.cookies.clear()
    return response


def download_and_extract_archive(
    url: str, dest_dir: str, *, headers: dict[str, str] | None = None
) -> list[str]:
    """Download an HTTPS archive URL and extract it safely into *dest_dir*."""
    if urlparse(url).scheme.lower() != "https":
        raise ArchiveError(f"Only HTTPS URLs are supported for archive download: {url!r}")

    destination_root = Path(dest_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    staging_root = destination_root.parent / f".apm-archive-staging-{uuid.uuid4().hex}"
    staging_root.mkdir(parents=True, exist_ok=False)
    download_path = staging_root / "archive-download"
    response = None
    try:
        request_headers = {"User-Agent": "apm-cli"}
        if headers:
            request_headers.update(headers)
        response = _archive_get(url, headers=request_headers, timeout=60, stream=True)
        response.raise_for_status()

        final_url = getattr(response, "url", url)
        if isinstance(final_url, str) and urlparse(final_url).scheme.lower() != "https":
            raise ArchiveError(f"Redirect to non-HTTPS URL rejected: {final_url!r}")
        detection_url = final_url if isinstance(final_url, str) and final_url else url
        archive_format = _detect_archive_format(
            response.headers.get("Content-Type", ""), detection_url
        )
        _stream_download_to_file(response, download_path, url)
        if archive_format == "tar.gz":
            return _extract_tar_gz_file(download_path, dest_dir)
        return _extract_zip_file(download_path, dest_dir)
    except requests.exceptions.RequestException as exc:
        raise ArchiveError(f"Failed to download archive from {url!r}: {exc}") from exc
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        shutil.rmtree(staging_root, ignore_errors=True)
