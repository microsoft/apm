"""Download and safely extract archive packages (zip / tar.gz).

Generic archive helper, decoupled from the marketplace layer: any
URL-sourced package installer can reuse it without importing marketplace
internals. Keeps the zip-slip / path-traversal / decompression-bomb guards
here so they apply to every caller (see #692 forward-compat constraints)."""

from __future__ import annotations

import contextlib
import io
import os
import tarfile
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    validate_path_segments,
)

_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_DOWNLOAD_BYTES = _MAX_UNCOMPRESSED_BYTES
_COPY_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class ArchiveError(Exception):
    """Raised when an archive cannot be downloaded or extracted safely."""


def _copy_member_within_limit(src: object, dst: object, running_total: int) -> int:
    """Stream *src* into *dst*, enforcing the cumulative uncompressed cap.

    Counts the ACTUAL bytes read from the (decompressed) member stream and
    aborts mid-stream once the running total exceeds the cap, rather than
    trusting the archive's header-declared member sizes. A crafted archive
    that under-declares its sizes therefore cannot slip past the
    decompression-bomb guard, and a single huge member is never buffered
    whole in memory.
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
    extracted: list[str] = []
    total_size = 0
    for info in archive.infolist():
        if info.filename.endswith("/"):
            continue
        _check_archive_member(info.filename)
        destination = _safe_destination(dest_dir, info.filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, open(destination, "wb") as dst:
            total_size = _copy_member_within_limit(src, dst, total_size)
        extracted.append(info.filename)
    return extracted


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


def download_and_extract_archive(url: str, dest_dir: str) -> list[str]:
    """Download an HTTPS archive URL and extract it safely into *dest_dir*."""
    if urlparse(url).scheme.lower() != "https":
        raise ArchiveError(f"Only HTTPS URLs are supported for archive download: {url!r}")

    destination_root = Path(dest_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    download_path = destination_root / f".apm-archive-download-{uuid.uuid4().hex}"
    response = None
    try:
        response = requests.get(url, headers={"User-Agent": "apm-cli"}, timeout=60, stream=True)
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
        with contextlib.suppress(OSError):
            download_path.unlink()
