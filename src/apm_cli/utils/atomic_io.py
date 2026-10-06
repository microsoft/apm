"""Atomic file writes and exclusive directory publication for APM.

Writes go to a temp file in the same directory as the target, then are
renamed via :func:`os.replace`. A crash mid-write cannot leave a half-
written destination, and on POSIX the rename is atomic with respect to
concurrent readers.

This is the single canonical implementation; both
``apm_cli.commands._helpers._atomic_write`` (kept as an alias for
backward compatibility with existing tests) and
``apm_cli.compilation.output_writer`` route through here.
Source packages use ``publish_directory_noreplace`` to publish staged
directories without replacing concurrently created consumer output.
"""

import contextlib
import errno
import os
import stat
import sys
import tempfile
from pathlib import Path


def publish_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically move a staged directory without replacing any destination.

    Both paths must be on the same filesystem. Unsupported platforms,
    libraries and filesystems fail closed; an exists-check plus ordinary
    POSIX rename cannot provide this guarantee.
    """
    if sys.platform == "win32":
        # Windows rename refuses existing destinations, including empty dirs.
        os.rename(source, destination)
        return
    if sys.platform not in {"linux", "darwin"}:
        raise OSError(errno.ENOTSUP, "Atomic no-replace directory publication is unavailable")

    import ctypes

    source_bytes, destination_bytes = os.fsencode(source), os.fsencode(destination)
    if b"\0" in source_bytes or b"\0" in destination_bytes:
        raise ValueError("Directory publication paths cannot contain NUL bytes")
    libc = ctypes.CDLL(None, use_errno=True)
    arguments: tuple[bytes | int, ...]
    if sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        argument_types = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        arguments = (source_bytes, destination_bytes, 0x00000004)  # RENAME_EXCL
    else:
        rename = getattr(libc, "renameat2", None)
        argument_types = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        arguments = (-100, source_bytes, -100, destination_bytes, 1)  # AT_FDCWD, RENAME_NOREPLACE
    if rename is None:
        raise OSError(errno.ENOTSUP, "Atomic no-replace directory publication is unavailable")
    rename.argtypes = argument_types
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(source), None, os.fspath(destination))


def _replace_atomic_file(source: str, destination: Path) -> None:
    """Replace one atomic temp file, with a narrow installed-binary test seam."""
    if os.environ.get("APM_TEST_FAIL_LOCK_REPLACE") == "1" and destination.name == "apm.lock.yaml":
        raise OSError("lockfile replace blocked by test")
    os.replace(source, destination)


def normalize_crlf_to_lf(data: str) -> str:
    """Normalize CRLF to LF (leaves bare CR alone, like the drift normalizer).

    Mirrors :func:`apm_cli.utils.normalization._normalize_line_endings` at the
    string level. Combined with ``newline=""`` on the open() call, this keeps
    written bytes platform-independent so deployed-file hashes do not diverge
    between Windows (which would otherwise translate ``\\n`` -> ``\\r\\n``) and
    POSIX.
    """
    return data.replace("\r\n", "\n")


def write_text_lf(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` as UTF-8 with deterministic LF line endings.

    Non-atomic counterpart to :func:`atomic_write_text`. Caller must ensure
    ``path.parent`` exists. ``newline=""`` disables the platform newline
    translation that ``Path.write_text`` performs in text mode, so the on-disk
    bytes (and therefore their content hash) are identical on every OS.
    """
    path.write_text(normalize_crlf_to_lf(data), encoding="utf-8", newline="")


def _validate_temp_name_fragment(fragment: str, parameter: str) -> None:
    """Reject temp-name fragments that could escape the target directory."""
    if "\x00" in fragment or "/" in fragment or "\\" in fragment or ":" in fragment:
        raise ValueError(f"{parameter} must be a portable filename fragment")


def atomic_write_text(
    path: Path,
    data: str,
    *,
    new_file_mode: int | None = None,
    normalize_line_endings: bool = True,
    temp_prefix: str = "apm-atomic-",
    temp_suffix: str = "",
) -> None:
    """Atomically write ``data`` (UTF-8) to ``path``.

    The temp file is created in ``path.parent`` so the eventual
    ``os.replace`` is a same-filesystem rename. Caller is responsible
    for ensuring the parent directory exists.

    If ``new_file_mode`` is given and ``path`` does not yet exist,
    the temp file's POSIX mode bits are set to that value before
    the rename so the destination is created with the requested
    permissions. Existing files keep their pre-existing mode (we
    do not downgrade nor upgrade perms). The mode hint is silently
    ignored on platforms where ``os.fchmod`` is unavailable
    (e.g. Windows), where POSIX mode bits are not enforced anyway.

    By default CRLF sequences are normalized to LF for deterministic
    generated output. Callers preserving hand-authored byte ranges can
    disable normalization with ``normalize_line_endings=False``.

    ``temp_prefix`` and ``temp_suffix`` let compatibility wrappers retain
    an established sibling-file naming contract without reimplementing the
    atomic write.

    On any failure, the temp file is removed and the original target
    file (if any) remains untouched.
    """
    existed = path.exists()
    existing_mode = stat.S_IMODE(path.stat().st_mode) if existed else None
    _validate_temp_name_fragment(temp_prefix, "temp_prefix")
    _validate_temp_name_fragment(temp_suffix, "temp_suffix")
    fd, tmp_name = tempfile.mkstemp(
        prefix=temp_prefix,
        suffix=temp_suffix,
        dir=str(path.parent),
    )
    fd_wrapped = False
    try:
        mode = existing_mode if existed else new_file_mode
        if mode is not None and hasattr(os, "fchmod"):
            with contextlib.suppress(OSError):
                os.fchmod(fd, mode)
        fh = os.fdopen(fd, "w", encoding="utf-8", newline="")
        fd_wrapped = True
        with fh:
            fh.write(normalize_crlf_to_lf(data) if normalize_line_endings else data)
        _replace_atomic_file(tmp_name, path)
    except Exception:
        if not fd_wrapped:
            # fdopen never took ownership of the descriptor; close it so
            # Windows can release its lock and the tmp file can be unlinked.
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
