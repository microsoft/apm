"""Atomic file-write primitive for APM.

Writes go to a temp file in the same directory as the target, then are
renamed via :func:`os.replace`. A crash mid-write cannot leave a half-
written destination, and on POSIX the rename is atomic with respect to
concurrent readers.

This is the single canonical implementation; both
``apm_cli.commands._helpers._atomic_write`` (kept as an alias for
backward compatibility with existing tests) and
``apm_cli.compilation.output_writer`` route through here.
"""

import contextlib
import os
import secrets
import stat
import tempfile
from pathlib import Path


def umask_file_mode(path: Path) -> int | None:
    """Return the mode a plain ``open()`` would give ``path`` if created now.

    Shared, committed artifacts pass this as ``new_file_mode`` so they do not
    inherit ``mkstemp``'s owner-only 0600. The mode is observed from a
    short-lived probe file in ``path.parent`` rather than by reading the
    umask, because reading it requires changing the process-wide umask, which
    unrelated concurrent file or directory creation would observe. Returns
    ``None`` when ``path`` already exists (its mode is preserved anyway) or
    where ``os.fchmod`` is unavailable, e.g. Windows.
    """
    if not hasattr(os, "fchmod") or path.exists():
        return None
    probe = path.parent / f".apm-mode-probe-{secrets.token_hex(8)}"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except OSError:
        return None
    try:
        return stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(probe)


def _replace_atomic_file(source: str, destination: Path) -> None:
    """Replace one atomic temp file, with a narrow installed-binary test seam."""
    if os.environ.get("APM_TEST_FAIL_LOCK_REPLACE") == "1" and destination.name == "apm.lock.yaml":
        raise OSError("lockfile replace blocked by test")
    os.replace(source, destination)


def atomic_copy(source: Path, destination: Path, *, metadata: bool = True) -> Path:
    """Copy *source* over *destination* atomically (#4691).

    The bytes are written through the descriptor of a temp file created in the
    destination's directory, which is then renamed over the destination with
    :func:`os.replace`, so a reader sees the old file or the new one, never a
    partial one. The staging path is never reopened, and it is checked to
    still be that same regular file before publication. With *metadata*
    (``shutil.copy2``) the source's mode and times are kept; without it
    (``shutil.copyfile``) an existing destination keeps its mode and a new
    one gets the mode a plain ``open()`` would give it. A symlinked
    destination is replaced at its target, as an in-place copy writes
    through the link. On failure the temp file is removed (made writable
    first, for Windows) and the destination is untouched.
    """
    target = Path(os.path.realpath(destination)) if destination.is_symlink() else destination
    source_stat = os.stat(source)
    if metadata:
        mode: int | None = stat.S_IMODE(source_stat.st_mode)
    else:
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else umask_file_mode(target)
    fd, tmp_name = tempfile.mkstemp(prefix="apm-atomic-", dir=str(target.parent))
    try:
        try:
            _stage_copy(source, fd)
            _stage_metadata(fd, tmp_name, mode, source_stat if metadata else None)
            staged = os.fstat(fd)
            current = os.lstat(tmp_name)
            if (current.st_dev, current.st_ino) != (staged.st_dev, staged.st_ino) or not (
                stat.S_ISREG(current.st_mode)
            ):
                raise OSError(f"Staging file {tmp_name} was replaced before publication")
        finally:
            os.close(fd)
        os.replace(tmp_name, target)
    except BaseException:
        _discard_staging(tmp_name)
        raise
    return destination


def _stage_copy(source: Path, fd: int) -> None:
    """Copy *source*'s bytes into the open staging descriptor *fd*."""
    with open(source, "rb") as reader:
        while chunk := reader.read(1 << 20):
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view) :]


def _stage_metadata(fd: int, tmp_name: str, mode: int | None, times: os.stat_result | None) -> None:
    """Apply *mode* and the source *times* to the staging file, by descriptor where possible."""
    if times is not None:
        target: int | str = fd if os.utime in os.supports_fd else tmp_name
        os.utime(target, ns=(times.st_atime_ns, times.st_mtime_ns))
    if mode is None:
        return
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)
    else:
        os.chmod(tmp_name, mode)


def _discard_staging(tmp_name: str) -> None:
    """Remove a staging file, even a read-only one on Windows."""
    with contextlib.suppress(OSError):
        os.chmod(tmp_name, stat.S_IREAD | stat.S_IWRITE)
    with contextlib.suppress(OSError):
        os.unlink(tmp_name)


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
    create_only: bool = False,
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

    ``create_only`` publishes the completed temp file with an exclusive hard
    link instead of replacement. An existing destination, including one that
    appears during staging, is never overwritten. Unsupported filesystems fail
    explicitly rather than falling back to a racy check followed by replacement.

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
        if create_only:
            os.link(tmp_name, path)
            os.unlink(tmp_name)
        else:
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
