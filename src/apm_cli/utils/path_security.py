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

import urllib.parse as _up
from pathlib import Path

from .file_ops import robust_rmtree


class PathTraversalError(ValueError):
    """Raised when a computed path escapes its expected base directory."""


def decode_url_path_segments(
    raw_path: str,
    *,
    context: str = "URL path",
) -> tuple[str, ...]:
    """Strictly decode safe URL path segments without changing URL structure.

    Callers must parse a URL before passing its ``path`` component here.  The
    returned values are decoded identity material; callers retain ``raw_path``
    when they need to render or transport the original encoded URL.

    A literal ``/`` separates segments.  Percent escapes are validated before
    decoding, decoded as strict UTF-8, and may not introduce separators,
    traversal names, empty values, or another percent escape.  Rejecting a
    residual percent escape prevents multi-encoded traversal and separator
    payloads from being accepted after a bounded number of decode passes.
    """
    _, decoded_segments = parse_url_path_segments(raw_path, context=context)
    return decoded_segments


def parse_url_path_segments(
    raw_path: str,
    *,
    context: str = "URL path",
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return validated raw and decoded URL path segments.

    The raw segments retain their encoded presentation for URL transport. The
    decoded segments are suitable only for provider identities that require
    decoded values, such as Azure DevOps organization and project names.
    """
    if not isinstance(raw_path, str):
        raise PathTraversalError(f"Invalid {context}: URL path must be a string")

    path = raw_path[1:] if raw_path.startswith("/") else raw_path
    if not path:
        raise PathTraversalError(f"Invalid {context}: path segments must not be empty")

    raw_segments = tuple(path.split("/"))
    decoded_segments: list[str] = []
    for raw_segment in raw_segments:
        if not raw_segment:
            raise PathTraversalError(f"Invalid {context}: path segments must not be empty")
        if "\\" in raw_segment:
            raise PathTraversalError(
                f"Invalid {context}: path segments must not contain path separators"
            )
        index = 0
        while index < len(raw_segment):
            character = raw_segment[index]
            if ord(character) < 0x21 or ord(character) > 0x7E:
                raise PathTraversalError(
                    f"Invalid {context}: path segments must use percent-encoded UTF-8 bytes"
                )
            if character == "%":
                if (
                    index + 2 >= len(raw_segment)
                    or raw_segment[index + 1] not in "0123456789abcdefABCDEF"
                    or raw_segment[index + 2] not in "0123456789abcdefABCDEF"
                ):
                    raise PathTraversalError(f"Invalid {context}: malformed percent-encoding")
                index += 3
            else:
                index += 1
        try:
            decoded = _up.unquote_to_bytes(raw_segment).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PathTraversalError(
                f"Invalid {context}: percent-encoding must be valid UTF-8"
            ) from exc
        if not decoded:
            raise PathTraversalError(f"Invalid {context}: path segments must not be empty")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded):
            raise PathTraversalError(
                f"Invalid {context}: percent-encoding must not decode to control characters"
            )
        if "%" in decoded:
            raise PathTraversalError(f"Invalid {context}: residual percent-encoding is not allowed")
        if "/" in decoded or "\\" in decoded:
            raise PathTraversalError(
                f"Invalid {context}: percent-encoding must not decode to a path separator"
            )
        if decoded in {".", ".."}:
            raise PathTraversalError(
                f"Invalid {context}: segment '{raw_segment}' is a traversal sequence"
            )
        decoded_segments.append(decoded)
    return raw_segments, tuple(decoded_segments)


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
        # Iteratively percent-decode each segment so multi-encoded traversal
        # markers (e.g. '%252e%252e' -> '%2e%2e' -> '..') cannot sneak past
        # the reject-set check. Defends every caller of this guard.
        decoded = segment
        for _ in range(8):
            nxt = _up.unquote(decoded)
            if nxt == decoded:
                break
            decoded = nxt
        if segment in reject or decoded in reject:
            raise PathTraversalError(
                f"Invalid {context} '{path_str}': segment '{segment}' is a traversal sequence"
            )
        if reject_empty and not segment:
            raise PathTraversalError(
                f"Invalid {context} '{path_str}': path segments must not be empty"
            )


def _strip_extended_prefix(p: Path) -> Path:
    """Strip the ``\\\\?\\`` extended-length prefix that Windows' resolve() may add.

    On Windows, ``Path.resolve()`` can inconsistently add the prefix to
    one path but not another, making ``is_relative_to`` fail even when
    both paths share the same physical root (#886).
    """
    s = str(p)
    if s.startswith("\\\\?\\"):
        return Path(s[4:])
    return p


def ensure_path_within(path: Path, base_dir: Path) -> Path:
    """Resolve *path* and assert it lives inside *base_dir*.

    Returns the resolved path on success.  Raises
    :class:`PathTraversalError` if the resolved path escapes *base_dir*.

    This is intentionally strict: symlinks are resolved so that a link
    pointing outside the base is caught as well.
    """
    return ensure_path_within_resolved(
        path,
        _strip_extended_prefix(base_dir.resolve()),
    )


def ensure_path_within_resolved(path: Path, resolved_base: Path) -> Path:
    """Resolve *path* and assert containment against a pre-resolved base."""
    resolved = _strip_extended_prefix(path.resolve())
    resolved_base = _strip_extended_prefix(resolved_base)
    try:
        if not resolved.is_relative_to(resolved_base):
            raise PathTraversalError(
                f"Path '{path}' resolves to '{resolved}' which is outside "
                f"the allowed base directory '{resolved_base}'"
            )
    except (TypeError, ValueError) as exc:
        raise PathTraversalError(
            f"Cannot verify containment of '{path}' within '{resolved_base}': {exc}"
        ) from exc
    return resolved


def has_symlink_component(base_dir: Path, path: Path) -> bool:
    """Return whether any component of *path* below *base_dir* is a symlink."""
    try:
        relative = path.relative_to(base_dir)
        current = base_dir
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return True
        return False
    except (OSError, ValueError):
        return True


def safe_rmtree(path: Path, base_dir: Path) -> None:
    """Remove *path* only if it resolves within *base_dir*.

    Drop-in replacement for ``shutil.rmtree(path)`` at sites where the
    target is derived from user-controlled input.  Uses retry logic for
    transient file-lock errors (e.g. antivirus scanning on Windows).
    """
    ensure_path_within(path, base_dir)
    robust_rmtree(path)
