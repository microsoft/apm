"""Download, write, and safely extract archive packages (zip / tar.gz).

Generic archive helpers, decoupled from marketplace internals. URL-sourced
installers and local bundle/archive paths reuse the same zip-slip,
path-traversal, and decompression-bomb guards.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import os
import re
import shutil
import tarfile
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Iterator
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
# Tar headers, padding and pax records the uncompressed cap does not count.
_TAR_HEADER_ALLOWANCE = 16 * 1024 * 1024
_MAX_ARCHIVE_DOWNLOAD_BYTES = _MAX_UNCOMPRESSED_BYTES
_COPY_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_ARCHIVE_SESSION = requests.Session()
_ARCHIVE_SESSION.max_redirects = 5

_ErrorT = TypeVar("_ErrorT", bound=Exception)


class ArchiveError(Exception):
    """Raised when an archive cannot be downloaded or extracted safely."""


class _TarStreamLimit(tarfile.ReadError):
    """The decompressed tar stream would pass the uncompressed cap plus headers."""


class _BoundedStream:
    """A decompressed tar stream that refuses to read or seek past *limit*.

    ``tarfile`` reads a pax header and a GNU long-name or long-link block into
    memory at the size the header declares, before any member reaches the
    extraction limits. A read that would pass the limit is refused before
    anything is read, so memory stays bounded.
    """

    def __init__(self, stream: IO[bytes], max_uncompressed: int) -> None:
        self._stream = stream
        self._cap = max_uncompressed
        self._limit = max_uncompressed + _TAR_HEADER_ALLOWANCE

    def _refuse(self) -> None:
        raise _TarStreamLimit(
            f"Archive uncompressed size exceeds size limit: {self._cap // (1024 * 1024)} MB"
        )

    def read(self, size: int | None = -1) -> bytes:
        remaining = self._limit - self._stream.tell()
        if size is None or size < 0:
            size = remaining + 1
        elif size > remaining:
            self._refuse()
        data = self._stream.read(size)
        if self._stream.tell() > self._limit:
            self._refuse()
        return data

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET and offset > self._limit:
            self._refuse()
        if whence == io.SEEK_CUR and self._stream.tell() + offset > self._limit:
            self._refuse()
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True


@contextlib.contextmanager
def open_tar_gz(
    *,
    path: Path | None = None,
    fileobj: IO[bytes] | None = None,
    max_uncompressed: int | None = None,
    error_type: type[_ErrorT] = ArchiveError,
) -> Iterator[tarfile.TarFile]:
    """Open a gzip-compressed tar for reading through the bounded stream."""
    cap = _MAX_UNCOMPRESSED_BYTES if max_uncompressed is None else max_uncompressed
    with contextlib.ExitStack() as stack:
        raw = stack.enter_context(open(path, "rb")) if path is not None else fileobj
        compressed = stack.enter_context(gzip.GzipFile(fileobj=raw, mode="rb"))
        stream = _BoundedStream(compressed, cap)
        try:
            tar = stack.enter_context(tarfile.open(fileobj=stream, mode="r:"))
        except _TarStreamLimit as exc:
            raise error_type(str(exc)) from exc
        except OSError as exc:
            raise tarfile.ReadError("not a gzip file") from exc
        yield tar


def _bounded_members(tar: tarfile.TarFile, error_type: type[_ErrorT]) -> Iterator[tarfile.TarInfo]:
    """Iterate *tar*, mapping a refused stream read to *error_type*."""
    try:
        yield from tar
    except _TarStreamLimit as exc:
        _raise(error_type, str(exc))


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


# Windows reserved device names, matched on the text before the first dot;
# COM and LPT also take a superscript digit (U+00B9, U+00B2, U+00B3).
_RESERVED_DEVICE_STEM = re.compile(
    r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9\u00b9\u00b2\u00b3])", re.IGNORECASE
)


def portable_name_problem(name: str) -> str | None:
    """Why *name* would not be written as-is on every OS, or ``None``.

    Windows trims a trailing dot or space, reads ``:`` as an alternate data
    stream and maps reserved device names, so a segment with any of these
    would not land at the path it names. The rule applies on every OS (as
    git's ``core.protectNTFS``) so one archive extracts the same everywhere.
    """
    for segment in name.replace("\\", "/").split("/"):
        if not segment:
            continue
        if not segment.strip("."):
            return "a segment made only of dots"
        if segment[-1] in ". ":
            return "a segment ending in a dot or space"
        if ":" in segment or any(ord(char) < 0x20 for char in segment):
            return "a colon or control character"
        if _RESERVED_DEVICE_STEM.fullmatch(segment.split(".", 1)[0].rstrip(" ")):
            return "a reserved device name"
    return None


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


def _member_key(member_name: str) -> str:
    """Return the collision key for an archive member name.

    Two names that land on the same path on a case-insensitive filesystem,
    that differ only by ``./`` prefixes or separators, or that are NFC and
    NFD spellings of one name (one file on macOS), share one key.
    """
    normalized = member_name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    key = unicodedata.normalize("NFC", "/".join(parts))
    return unicodedata.normalize("NFC", key.casefold())


class _MemberClaims:
    """Path claims for one archive, checked before the first write.

    Refuses a second entry for the same path (exact or case-folded), and a
    file whose path is also used as a directory, in either order.
    """

    def __init__(self) -> None:
        self._files: set[str] = set()
        self._dirs: set[str] = set()
        self._parents: set[str] = set()

    def claim(self, member_name: str, *, is_dir: bool, error_type: type[_ErrorT]) -> None:
        key = _member_key(member_name)
        if key in self._files or key in self._dirs:
            _raise(error_type, f"Refusing to extract duplicate entry: {member_name}")
        if not is_dir and key in self._parents:
            _raise(error_type, f"Refusing to extract conflicting entry: {member_name}")
        parts = key.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if parent in self._files:
                _raise(error_type, f"Refusing to extract conflicting entry: {member_name}")
            self._parents.add(parent)
        (self._dirs if is_dir else self._files).add(key)


_ZIP_FILE_TYPE_MASK = 0xF000
_ZIP_SYMLINK = 0xA000
_ZIP_ALLOWED_FILE_TYPES = frozenset({0, 0x4000, 0x8000})


def _archive_member_target(
    member_name: str,
    dest_root: Path,
    *,
    error_type: type[_ErrorT],
) -> Path | None:
    """Return a safe extraction target for an archive member, or None for empty dirs."""
    if not member_name or member_name in (".", "/", "./"):
        return None
    if "\x00" in member_name:
        _raise(error_type, f"Refusing to extract entry with a null byte: {member_name!r}")
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
    problem = portable_name_problem(member_name)
    if problem is not None:
        _raise(error_type, f"Refusing to extract entry with {problem}: {member_name!r}")
    target = dest_root / member_name
    try:
        ensure_path_within(target, dest_root)
    except PathTraversalError:
        _raise(error_type, f"Refusing to extract path-traversal entry: {member_name}")
    return target


def _stream_member(
    src: IO[bytes],
    target: Path,
    running_total: int,
    max_uncompressed: int,
    *,
    error_type: type[_ErrorT],
) -> int:
    """Stream one member into *target*, enforcing the cumulative uncompressed cap.

    Counts bytes actually read from the decompressed stream, never the
    member size an archive header declares.
    """
    total = running_total
    with open(target, "wb") as fh:
        while True:
            chunk = src.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_uncompressed:
                limit_mb = max_uncompressed // (1024 * 1024)
                actual_mb = total // (1024 * 1024)
                _raise(
                    error_type,
                    f"Archive uncompressed size exceeds size limit: {actual_mb} MB > {limit_mb} MB",
                )
            fh.write(chunk)
    return total


def _write_planned(
    plan: list[tuple[Path, str, Callable[[], IO[bytes] | None], int]],
    max_uncompressed: int,
    *,
    error_type: type[_ErrorT],
) -> list[str]:
    """Write validated members; on any failure remove the files written so far."""
    written: list[Path] = []
    names: list[str] = []
    total_uncompressed = 0
    try:
        for target, name, opener, mode in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            src = opener()
            if src is None:
                continue
            written.append(target)
            with src:
                total_uncompressed = _stream_member(
                    src, target, total_uncompressed, max_uncompressed, error_type=error_type
                )
            if mode:
                os.chmod(target, mode & 0o755)
            names.append(name)
    except BaseException:
        for path in written:
            with contextlib.suppress(OSError):
                path.unlink()
        raise
    return names


def safe_extract_tar(
    tar: tarfile.TarFile,
    dest_root: Path,
    *,
    max_entries: int | None = None,
    max_uncompressed: int | None = None,
    error_type: type[_ErrorT] = ValueError,
) -> list[str]:
    """Safely stream-extract *tar* under *dest_root* with the shared archive limits.

    Applies the same limits as :func:`safe_extract_zip`: an entry-count cap,
    a streamed uncompressed-size cap, and refusal of links, special files,
    absolute or traversing paths and duplicate entries. Every entry is
    validated before the first file is written. Returns the names of the
    regular files written.
    """
    entry_limit = MAX_ZIP_ENTRIES if max_entries is None else max_entries
    size_limit = _MAX_UNCOMPRESSED_BYTES if max_uncompressed is None else max_uncompressed
    claims = _MemberClaims()
    declared_total = 0
    dirs: list[Path] = []
    plan: list[tuple[Path, str, Callable[[], IO[bytes] | None], int]] = []
    # Advancing a compressed tar to the next header decompresses the current
    # member's data, so the declared sizes are capped before each advance.
    for count, member in enumerate(_bounded_members(tar, error_type), start=1):
        if count > entry_limit:
            _raise(error_type, f"Archive has more than {entry_limit} entries (limit {entry_limit})")
        if member.issym() or member.islnk():
            _raise(error_type, f"Symlinks and hard links are not supported: {member.name!r}")
        if not (member.isdir() or member.isreg()):
            _raise(error_type, f"Refusing to extract special file: {member.name!r}")
        # A negative size would move the next header read backward, forever.
        if member.size < 0:
            _raise(error_type, f"Refusing to extract entry with a negative size: {member.name!r}")
        declared_total += member.size
        if declared_total > size_limit:
            _raise(
                error_type,
                f"Archive uncompressed size exceeds size limit: {size_limit // (1024 * 1024)} MB",
            )
        target = _archive_member_target(member.name, dest_root, error_type=error_type)
        if target is None:
            continue
        claims.claim(member.name, is_dir=member.isdir(), error_type=error_type)
        if member.isdir():
            dirs.append(target)
            continue
        plan.append((target, member.name, lambda m=member: tar.extractfile(m), member.mode))
    dest_root.mkdir(parents=True, exist_ok=True)
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
    return _write_planned(plan, size_limit, error_type=error_type)


def safe_extract_zip(
    zf: zipfile.ZipFile,
    dest_root: Path,
    *,
    max_entries: int | None = None,
    max_uncompressed: int | None = None,
    error_type: type[_ErrorT] = ValueError,
    member_name_transform: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Safely stream-extract *zf* under *dest_root* with the shared archive limits.

    The uncompressed-size limit is enforced against bytes actually read from
    each entry, not against attacker-controlled ZipInfo.file_size metadata.
    Every entry is validated before the first file is written.
    ``member_name_transform`` can strip or reject names before validation; the
    returned list contains the transformed member names that were written.
    """
    max_entries = MAX_ZIP_ENTRIES if max_entries is None else max_entries
    max_uncompressed = _MAX_UNCOMPRESSED_BYTES if max_uncompressed is None else max_uncompressed
    members = zf.infolist()
    if len(members) > max_entries:
        _raise(error_type, f"ZIP archive has {len(members)} entries (limit {max_entries})")

    claims = _MemberClaims()
    dirs: list[Path] = []
    plan: list[tuple[Path, str, Callable[[], IO[bytes] | None], int]] = []
    for info in members:
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = unix_mode & _ZIP_FILE_TYPE_MASK
        if file_type == _ZIP_SYMLINK:
            _raise(error_type, f"Refusing to extract symlink: {info.filename}")
        if file_type not in _ZIP_ALLOWED_FILE_TYPES:
            _raise(error_type, f"Refusing to extract special file: {info.filename!r}")
        # zipfile cuts a name at its first NUL; judge the name the archive stores.
        stored = info.orig_filename
        if "\x00" in stored:
            _raise(error_type, f"Refusing to extract entry with a null byte: {stored!r}")
        member_name = info.filename
        if member_name_transform is not None:
            member_name = member_name_transform(member_name)
        if member_name is None:
            continue
        target = _archive_member_target(member_name, dest_root, error_type=error_type)
        if target is None:
            continue
        claims.claim(member_name, is_dir=info.is_dir(), error_type=error_type)
        if info.is_dir():
            dirs.append(target)
            continue
        plan.append((target, member_name, lambda i=info: zf.open(i, "r"), unix_mode))
    dest_root.mkdir(parents=True, exist_ok=True)
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
    return _write_planned(plan, max_uncompressed, error_type=error_type)


def read_zip_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_uncompressed: int | None = None,
    error_type: type[_ErrorT] = ValueError,
) -> bytes:
    """Read one zip member into memory under the shared uncompressed-size cap."""
    limit = _MAX_UNCOMPRESSED_BYTES if max_uncompressed is None else max_uncompressed
    buf = io.BytesIO()
    total = 0
    with zf.open(info, "r") as src:
        while True:
            chunk = src.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                _raise(
                    error_type,
                    f"Archive member exceeds size limit: {info.filename} > {limit // (1024 * 1024)} MB",
                )
            buf.write(chunk)
    return buf.getvalue()


def _extract_tar_archive(archive: tarfile.TarFile, dest_dir: str) -> list[str]:
    """Extract an opened tar archive into *dest_dir* with the shared archive limits."""
    return safe_extract_tar(archive, Path(dest_dir), error_type=ArchiveError)


def _extract_tar_gz(data: bytes, dest_dir: str) -> list[str]:
    """Extract a tar.gz archive into *dest_dir* with safety checks."""
    try:
        with open_tar_gz(fileobj=io.BytesIO(data)) as archive:
            return _extract_tar_archive(archive, dest_dir)
    except tarfile.TarError as exc:
        raise ArchiveError(f"Failed to read tar.gz archive: {exc}") from exc


def _extract_tar_gz_file(path: Path, dest_dir: str) -> list[str]:
    """Extract a tar.gz archive file into *dest_dir* with safety checks."""
    try:
        with open_tar_gz(path=path) as archive:
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
