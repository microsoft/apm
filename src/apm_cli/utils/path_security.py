"""Centralised path-security helpers for APM CLI.

Every filesystem operation whose target is derived from user-controlled
input (dependency strings, ``virtual_path``, ``apm.yml`` fields) **must**
pass through one of these guards before touching the disk.

Design
------
* ``validate_path_segments`` rejects traversal sequences (``.`` / ``..``)
  at parse time -- before any path is constructed or written.
* ``ensure_path_within`` is the single predicate for filesystem
  containment -- resolves both paths and asserts via
  ``Path.is_relative_to``.
* ``safe_rmtree`` wraps ``robust_rmtree`` with an ``ensure_path_within``
  check so callers get a drop-in replacement.
* ``PathTraversalError`` is a ``ValueError`` subclass for clear error
  semantics and easy ``except`` targeting.
"""

from __future__ import annotations

from pathlib import Path

from .file_ops import robust_rmtree


class PathTraversalError(ValueError):
    """Raised when a computed path escapes its expected base directory."""


def validate_path_segments(
    path_str: str,
    *,
    context: str = "path",
    reject_empty: bool = False,
    allow_current_dir: bool = False,
) -> None:
    """Reject path strings containing traversal sequences.

    Normalises backslashes to forward slashes, splits on ``/``, and
    rejects any segment that is ``.`` or ``..``.  Optionally rejects
    empty segments (from ``//`` or trailing ``/``).

    Parameters
    ----------
    path_str : str
        Path-like string to validate (repo URL, virtual path, etc.).
    context : str
        Human-readable label for error messages.
    reject_empty : bool
        If *True*, also reject empty segments.
    allow_current_dir : bool
        If *True*, ``.`` segments are accepted (e.g. for shell command
        strings like ``./bin/my-server`` where "here" is meaningful).
        ``..`` is still rejected.  Defaults to *False* so the strict
        rule applies to the dependency / virtual-path call sites.

    Raises
    ------
    PathTraversalError
        If any segment fails validation.
    """
    reject = {".."} if allow_current_dir else {".", ".."}
    for segment in path_str.replace("\\", "/").split("/"):
        if segment in reject:
            raise PathTraversalError(
                f"Invalid {context} '{path_str}': "
                f"segment '{segment}' is a traversal sequence"
            )
        if reject_empty and not segment:
            raise PathTraversalError(
                f"Invalid {context} '{path_str}': "
                f"path segments must not be empty"
            )


def ensure_path_within(path: Path, base_dir: Path) -> Path:
    """Resolve *path* and assert it lives inside *base_dir*.

    Returns the resolved path on success.  Raises
    :class:`PathTraversalError` if the resolved path escapes *base_dir*.

    This is intentionally strict: symlinks are resolved so that a link
    pointing outside the base is caught as well.
    """
    resolved = path.resolve()
    resolved_base = base_dir.resolve()
    try:
        if not resolved.is_relative_to(resolved_base):
            raise PathTraversalError(
                f"Path '{path}' resolves to '{resolved}' which is outside "
                f"the allowed base directory '{resolved_base}'"
            )
    except (TypeError, ValueError) as exc:
        raise PathTraversalError(
            f"Cannot verify containment of '{path}' within '{base_dir}': {exc}"
        ) from exc
    return resolved


def safe_rmtree(path: Path, base_dir: Path) -> None:
    """Remove *path* only if it resolves within *base_dir*.

    Drop-in replacement for ``shutil.rmtree(path)`` at sites where the
    target is derived from user-controlled input.  Uses retry logic for
    transient file-lock errors (e.g. antivirus scanning on Windows).
    """
    ensure_path_within(path, base_dir)
    robust_rmtree(path)
