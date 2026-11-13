"""Retry-aware file operations for cross-platform reliability.

On Windows, antivirus and endpoint-protection software (e.g. Symantec,
Windows Defender) briefly lock files while scanning them in temp
directories.  This causes ``[WinError 32] The process cannot access the
file because it is being used by another process`` during ``apm install``.

This module provides drop-in replacements for :func:`shutil.rmtree`,
:func:`shutil.copytree`, :func:`shutil.copy2`, and :meth:`Path.replace`
that transparently retry on transient lock errors with exponential backoff.

Design
------
* ``_is_transient_lock_error`` -- single predicate for classifying OSError.
* ``_retry_on_lock`` -- generic retry loop (not a decorator, because
  cleanup-between-retries varies per operation).
* ``robust_rmtree`` / ``robust_copytree`` / ``robust_copy2`` /
  ``robust_replace`` -- public API.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Defaults -- tuned for AV scan locks (sub-second to ~3 s total wait)
# ---------------------------------------------------------------------------
_DEFAULT_MAX_RETRIES: int = 5
_DEFAULT_INITIAL_DELAY: float = 0.1  # seconds
_DEFAULT_MAX_DELAY: float = 2.0  # seconds
_DEFAULT_BACKOFF_FACTOR: float = 2.0


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _is_transient_lock_error(exc: OSError) -> bool:
    """Return True if *exc* looks like a transient file-lock error.

    Windows
    -------
    * ``winerror 32`` -- ERROR_SHARING_VIOLATION (another process has the
      file open).  This is the canonical AV-scan lock.
    * ``winerror 5`` -- ERROR_ACCESS_DENIED.  Raised by some endpoint
      protection tools that temporarily deny access during scanning.
      Only treated as transient on Windows where ``winerror`` is set;
      on Unix EACCES is almost always a real permission problem.

    Unix
    ----
    * ``errno.EBUSY`` -- device or resource busy (e.g. NFS silly-rename,
      mount point).  Rare but retriable.
    """
    if sys.platform == "win32":
        winerror = getattr(exc, "winerror", None)
        if winerror in (32, 5):
            return True
    return getattr(exc, "errno", None) == errno.EBUSY


# EEXIST matches only when a non-directory ancestor is found below the root.
_OBSTRUCTION_ERRNOS = frozenset({errno.EISDIR, errno.ENOTDIR, errno.EEXIST})


def obstructed_path_message(path: Path, root: Path, *, blocker: str) -> str:
    """Name a path whose existing *blocker* ("file"/"directory") stops a write."""
    from .paths import portable_relpath

    return (
        f"{portable_relpath(path, root)} is a {blocker} that blocks an APM-managed "
        "path. Move it aside, then run the command again."
    )


def describe_path_obstruction(error: BaseException, root: Path) -> str | None:
    """Return :func:`obstructed_path_message` when *error* is a path obstruction.

    Covers a directory where a file is written (EISDIR) and a file where a
    directory is needed (ENOTDIR/EEXIST, found by walking up to *root*).
    Returns ``None`` for any other error so callers keep the original text.
    """
    if not isinstance(error, OSError) or error.errno not in _OBSTRUCTION_ERRNOS:
        return None
    if not error.filename:
        return None
    path = Path(os.fsdecode(error.filename)).resolve()
    root = root.resolve()
    if error.errno == errno.EISDIR:
        return obstructed_path_message(path, root, blocker="directory")
    for candidate in (path, *path.parents):
        if candidate == root or root not in candidate.parents:
            break
        if candidate.exists() and not candidate.is_dir():
            return obstructed_path_message(candidate, root, blocker="file")
    return None


# ---------------------------------------------------------------------------
# Retry core
# ---------------------------------------------------------------------------


def _retry_on_lock(
    operation: Callable[[], T],
    description: str,
    *,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    initial_delay: float = _DEFAULT_INITIAL_DELAY,
    max_delay: float = _DEFAULT_MAX_DELAY,
    backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
    before_retry: Callable[[], None] | None = None,
) -> T:
    """Execute *operation*, retrying on transient file-lock errors.

    Parameters
    ----------
    operation:
        Zero-arg callable that performs the file operation.
    description:
        Human-readable operation label. It is not emitted because file paths
        may contain secrets.
    max_retries:
        Total attempts = 1 (initial) + *max_retries*.
    initial_delay:
        Sleep before first retry, in seconds.
    max_delay:
        Upper bound on sleep, in seconds.
    backoff_factor:
        Multiply delay by this after each retry.
    before_retry:
        Optional cleanup to run before each retry (e.g. remove a partial
        copytree destination).  Exceptions here are suppressed.

    Returns
    -------
    Whatever *operation* returns on success.

    Raises
    ------
    OSError
        The original exception, if all retries are exhausted.
    """
    delay = initial_delay
    last_exc: OSError | None = None

    for attempt in range(1 + max_retries):
        try:
            return operation()
        except OSError as exc:
            last_exc = exc
            if not _is_transient_lock_error(exc) or attempt == max_retries:
                raise
            _debug_file_op(
                f"Transient file lock (attempt {attempt + 1}/{max_retries}); "
                f"retrying in {delay:.2f}s"
            )
            if before_retry is not None:
                try:  # noqa: SIM105
                    before_retry()
                except OSError:
                    pass
            time.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)

    # Unreachable: the loop always returns or raises on the last attempt.
    # This guard satisfies type-checkers that analyse control-flow.
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Debug output (matches github_downloader._debug pattern)
# ---------------------------------------------------------------------------


def _debug_file_op(message: str) -> None:
    """Print debug message when APM_DEBUG is set."""
    if os.environ.get("APM_DEBUG"):
        print(f"[DEBUG] {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Read-only callback for rmtree
# ---------------------------------------------------------------------------


def _on_readonly_retry(func: Callable, path: str, _exc_info: Any) -> None:
    """``onerror`` callback: chmod writable and retry the failing removal."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def robust_rmtree(
    path: Path | str,
    *,
    ignore_errors: bool = False,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> None:
    """Remove a directory tree, retrying on transient lock errors.

    Handles read-only files (git pack/index) via an ``onerror`` callback
    and retries the full ``rmtree`` on transient lock errors.

    Parameters
    ----------
    path:
        Directory to remove.
    ignore_errors:
        If True, suppress any ``OSError`` after retries are exhausted
        (matches the existing ``_rmtree`` behaviour in github_downloader).
    max_retries:
        Maximum retry attempts for transient lock errors.
    """
    path_s = str(path)

    def _do_rmtree() -> None:
        failures: list[OSError] = []

        def recover_or_record(func: Callable, failed_path: str, exc_info: Any) -> None:
            try:
                _on_readonly_retry(func, failed_path, exc_info)
            except OSError as exc:
                failures.append(exc)

        shutil.rmtree(path_s, onerror=recover_or_record)
        if failures:
            # Finish independent removals, but never report a partial tree as removed.
            raise failures[0]

    try:
        _retry_on_lock(_do_rmtree, f"rmtree {path_s}", max_retries=max_retries)
    except OSError:
        if not ignore_errors:
            raise


def _reflink_copy_file(src: str, dst: str, *, follow_symlinks: bool = True) -> str:
    """``shutil.copy2`` work-alike that tries a reflink clone first.

    Falls through to ``shutil.copy2`` when the clone fails for any
    reason. The fallback path is always executed when reflinks are
    unsupported on the destination filesystem (cached per ``st_dev``)
    or when ``APM_NO_REFLINK`` is set.

    Used as the ``copy_function`` argument to :func:`shutil.copytree` so
    every regular file in a tree benefits from copy-on-write when
    available. Symlinks and special files are routed straight to
    ``shutil.copy2`` because clone primitives only target regular files.
    """
    from .reflink import clone_file

    try:
        if follow_symlinks and not os.path.islink(src):
            if clone_file(src, dst):
                shutil.copystat(src, dst, follow_symlinks=follow_symlinks)
                return dst
    except OSError:
        # Defensive: clone_file is documented as never-raises but the
        # underlying ctypes/ioctl path could surface an os-level error.
        pass
    return shutil.copy2(src, dst, follow_symlinks=follow_symlinks)


def robust_copytree(
    src: Path | str,
    dst: Path | str,
    *,
    symlinks: bool = False,
    ignore: Any = None,
    dirs_exist_ok: bool = False,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> Path:
    """Copy a directory tree, retrying on transient lock errors.

    Per-file copies attempt a copy-on-write reflink clone first
    (transparent on filesystems that support it) and fall back to
    ``shutil.copy2`` otherwise.

    On retry, any partial destination is removed first (clean-slate),
    unless *dirs_exist_ok* is True.

    Parameters
    ----------
    src, dst:
        Source and destination paths (same semantics as ``shutil.copytree``).
    symlinks:
        Preserve symlinks instead of following them.
    ignore:
        ``shutil.copytree`` ignore callable (e.g. ``shutil.ignore_patterns``).
    dirs_exist_ok:
        Passed through to ``shutil.copytree``.  When False (default),
        partial destinations are cleaned up before retry.
    max_retries:
        Maximum retry attempts for transient lock errors.

    Returns
    -------
    Path to the destination directory.
    """
    src_s, dst_s = str(src), str(dst)

    def _do_copytree() -> str:
        return shutil.copytree(
            src_s,
            dst_s,
            symlinks=symlinks,
            ignore=ignore,
            copy_function=_reflink_copy_file,
            dirs_exist_ok=dirs_exist_ok,
        )

    def _cleanup_partial() -> None:
        if not dirs_exist_ok and os.path.isdir(dst_s):
            try:  # noqa: SIM105
                shutil.rmtree(dst_s, onerror=_on_readonly_retry)
            except OSError:
                pass

    result = _retry_on_lock(
        _do_copytree,
        f"copytree {src_s} -> {dst_s}",
        max_retries=max_retries,
        before_retry=_cleanup_partial,
    )
    return Path(result)


def robust_copy2(
    src: Path | str,
    dst: Path | str,
    *,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> Path:
    """Copy a single file with metadata, retrying on transient lock errors.

    Parameters
    ----------
    src, dst:
        Source and destination paths (same semantics as ``shutil.copy2``).
    max_retries:
        Maximum retry attempts for transient lock errors.

    Returns
    -------
    Path to the destination file.
    """
    src_s, dst_s = str(src), str(dst)

    def _do_copy2() -> str:
        return _reflink_copy_file(src_s, dst_s)

    result = _retry_on_lock(
        _do_copy2,
        f"copy2 {src_s} -> {dst_s}",
        max_retries=max_retries,
    )
    return Path(result)


def robust_replace(
    src: Path | str,
    dst: Path | str,
    *,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> Path:
    """Atomically rename *src* to *dst*, retrying on transient lock errors.

    A failed rename moves nothing, so each retry repeats the same atomic
    operation. Persistent errors re-raise the original exception unchanged.

    Parameters
    ----------
    src, dst:
        Source and destination paths (same semantics as :meth:`Path.replace`).
    max_retries:
        Maximum retry attempts for transient lock errors.

    Returns
    -------
    Path to the destination.
    """
    src_p, dst_p = Path(src), Path(dst)
    return _retry_on_lock(
        lambda: src_p.replace(dst_p),
        f"replace {src_p} -> {dst_p}",
        max_retries=max_retries,
    )
