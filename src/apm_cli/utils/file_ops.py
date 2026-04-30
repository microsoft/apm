"""Retry-aware file operations for cross-platform reliability.

On Windows, antivirus and endpoint-protection software (e.g. Symantec,
Windows Defender) briefly lock files while scanning them in temp
directories.  This causes ``[WinError 32] The process cannot access the
file because it is being used by another process`` during ``apm install``.

This module provides drop-in replacements for :func:`shutil.rmtree`,
:func:`shutil.copytree`, and :func:`shutil.copy2` that transparently
retry on transient lock errors with exponential backoff.

Design
------
* ``_is_transient_lock_error`` -- single predicate for classifying OSError.
* ``_retry_on_lock`` -- generic retry loop (not a decorator, because
  cleanup-between-retries varies per operation).
* ``robust_rmtree`` / ``robust_copytree`` / ``robust_copy2`` -- public API.
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
        Human-readable label for debug messages (e.g. ``"rmtree /tmp/x"``).
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
                f"{description}: transient lock (attempt "
                f"{attempt + 1}/{max_retries}), retrying in {delay:.2f}s "
                f"-- {exc}"
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
    except OSError:
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
        shutil.rmtree(path_s, onerror=_on_readonly_retry)

    try:
        _retry_on_lock(_do_rmtree, f"rmtree {path_s}", max_retries=max_retries)
    except OSError:
        if not ignore_errors:
            raise


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
        return shutil.copy2(src_s, dst_s)

    result = _retry_on_lock(
        _do_copy2,
        f"copy2 {src_s} -> {dst_s}",
        max_retries=max_retries,
    )
    return Path(result)
