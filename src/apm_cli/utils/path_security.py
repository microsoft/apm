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
* ``filesystem_containment`` judges containment by directory identity
  under the file system's case rules (case and Unicode spellings of one
  directory agree); it reports, and leaves policy to the caller.
* ``safe_rmtree`` wraps ``robust_rmtree`` with an ``ensure_path_within``
  check so callers get a drop-in replacement.
* ``PathTraversalError`` is a ``ValueError`` subclass for clear error
  semantics and easy ``except`` targeting.
"""

from __future__ import annotations

import errno
import os
import urllib.parse as _up
from pathlib import Path
from stat import S_ISLNK, S_ISREG
from typing import BinaryIO

from .file_ops import robust_rmtree
from .paths import is_within, relative_posix, relative_to
from .realpath import proven_link_free, resolve_path


class PathTraversalError(ValueError):
    """Raised when a computed path escapes its expected base directory."""


def symlinked_deploy_refusal(link: str) -> str:
    """The one refusal for a deploy reached through a linked directory (#5174, #5287).

    *link* is repository- or user-controlled: it is shown as printable ASCII (#5215).
    """
    from apm_cli.utils.diagnostics import printable_ascii_text

    return (
        f"Refusing deployment through a symlinked path component: {printable_ascii_text(link)}. "
        "Replace the symlink with a regular directory or remove it, then retry."
    )


class SymlinkedDeployError(PathTraversalError):
    """A deploy path crosses a symlinked directory; *link* is relative to its root."""

    def __init__(self, link: str) -> None:
        super().__init__(symlinked_deploy_refusal(link))
        self.link = link


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
    reject_all_dots: bool = False,
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
    reject_all_dots : bool
        If *True*, also reject segments made only of dots (``...``).
        Windows drops trailing dots from names, so such a segment would
        alias its parent directory on disk.

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
        if reject_all_dots and decoded and not decoded.strip("."):
            raise PathTraversalError(
                f"Invalid {context} '{path_str}': segment '{segment}' consists only of dots"
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
        _strip_extended_prefix(resolve_path(base_dir)),
    )


def resolve_for_containment(path: Path) -> Path:
    """Resolve *path* once (symlinks followed) for a containment check made now."""
    return _strip_extended_prefix(resolve_path(path))


def resolved_relative_within(resolved: Path, resolved_base: Path) -> str | None:
    """POSIX path of *resolved* under *resolved_base*, or ``None`` when it escapes.

    Both arguments must come from :func:`resolve_for_containment` taken for
    this check: the filesystem is observed once, at resolution, instead of
    resolving the same two paths again for the containment test and again for
    the relative form (``ensure_path_within`` plus ``portable_relpath``). The
    verdict is identical; only the repeated symlink walks are gone.
    """
    # Strip both sides, as ensure_path_within_resolved does (#886): one side
    # may carry the Windows extended-length prefix while the other does not.
    resolved, resolved_base = (
        _strip_extended_prefix(resolved),
        _strip_extended_prefix(resolved_base),
    )
    try:
        return relative_posix(resolved, resolved_base)
    except (TypeError, ValueError):
        return None


def ensure_path_within_resolved(path: Path, resolved_base: Path) -> Path:
    """Resolve *path* and assert containment against a pre-resolved base."""
    resolved = _strip_extended_prefix(resolve_path(path))
    resolved_base = _strip_extended_prefix(resolved_base)
    try:
        if not is_within(resolved, resolved_base):
            raise PathTraversalError(
                f"Path '{path}' resolves to '{resolved}' which is outside "
                f"the allowed base directory '{resolved_base}'"
            )
    except (TypeError, ValueError) as exc:
        raise PathTraversalError(
            f"Cannot verify containment of '{path}' within '{resolved_base}': {exc}"
        ) from exc
    return resolved


def _caseless(path: Path) -> str:
    """Return *path* folded for a caseless compare (NFC and NFD spellings agree).

    The shared key of :func:`apm_cli.utils.paths.caseless`.
    """
    from .paths import caseless

    return caseless(str(path))


def filesystem_containment(path: Path, root: Path) -> bool | None:
    """Whether *path* is *root* or lies under it, under the file system's case rules.

    ``True`` inside, ``False`` outside, ``None`` when the directory spelled
    like *root* up to case and Unicode form can't be compared with it.
    ``normcase`` folds case where the platform does (Windows); elsewhere a
    case-insensitive volume (default macOS) gives one directory several
    spellings, so that ancestor is asked whether it is the same directory.
    """
    if path == root or path.is_relative_to(root):
        return True
    folded, base = os.path.normcase(path), os.path.normcase(root)
    if folded == base or folded.startswith(os.path.join(base, "")):
        return True
    spelled = _caseless(root)
    for ancestor in (path, *path.parents):
        if _caseless(ancestor) == spelled:
            try:
                return os.path.samefile(ancestor, root)
            except OSError:
                return None
    return False


def _collapse_relative_parts(base_dir: Path, parts: tuple[str, ...]) -> list[str] | None:
    """Collapse ``..`` below *base_dir*; ``None`` when it escapes or steps back over a link."""
    kept: list[str] = []
    for part in parts:
        if part == "..":
            if not kept or _existing_link(base_dir.joinpath(*kept)):
                return None
            kept.pop()
        elif part != ".":
            kept.append(part)
    return kept


# A reparse point whose tag is a name surrogate (a symbolic link, a directory junction,
# a WSL symlink) names another path; data-only tags (cloud files) do not.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_IO_REPARSE_TAG_NAME_SURROGATE = 0x20000000


def _is_link(path: Path) -> bool:
    """Whether *path* is a link of any kind (never its target), from one ``lstat``."""
    return is_link_stat(path.lstat())


def is_link_stat(info: os.stat_result) -> bool:
    """Whether an ``lstat`` result is a link of any kind: a symlink or a name-surrogate
    reparse point (a Windows junction)."""
    if S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    tag = getattr(info, "st_reparse_tag", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT and tag & _IO_REPARSE_TAG_NAME_SURROGATE)


def _existing_link(path: Path) -> bool:
    """:func:`_is_link`, with a missing path counting as no link."""
    try:
        return _is_link(path)
    except FileNotFoundError:
        return False


def first_symlink_component(base_dir: Path, path: Path) -> Path | None:
    """Return the first symlinked component of *path* below *base_dir*, or ``None``.

    ``..`` is collapsed first; the walk stops at the first missing component.
    Raises ``ValueError`` when *path* is not under *base_dir* or a ``..``
    escapes it or steps back over a link, and ``OSError`` when a component
    cannot be inspected; :func:`has_symlink_component` turns both into a refusal.
    """
    parts = _collapse_relative_parts(base_dir, relative_to(path, base_dir).parts)
    if parts is None:
        raise ValueError(f"{path} leaves {base_dir} or steps back over a link")
    if parts and proven_link_free(base_dir.joinpath(*parts[:-1])):
        # Every component up to the leaf's directory was proven link-free this
        # command (utils/realpath.py); only the leaf can be a link.
        leaf = base_dir.joinpath(*parts)
        try:
            return leaf if _is_link(leaf) else None
        except FileNotFoundError:
            return None
    current = base_dir
    for part in parts:
        current /= part
        try:
            linked = _is_link(current)
        except FileNotFoundError:
            return None
        if linked:
            return current
    return None


def open_regular_file(path: Path) -> BinaryIO:
    """Open *path* for binary reading only as a regular file.

    Never through a final-component link and never blocking: the file is
    opened non-blocking, then judged on the opened descriptor, so a FIFO or
    device raises ``OSError`` instead of waiting for a writer. Where
    ``O_NOFOLLOW`` is unavailable (Windows) a link is refused by ``lstat``
    first.
    """
    if not hasattr(os, "O_NOFOLLOW") and _existing_link(Path(path)):
        raise OSError(errno.ELOOP, "refusing to read through a link", str(path))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    try:
        if not S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def has_symlink_component(base_dir: Path, path: Path, *, raise_on_error: bool = False) -> bool:
    """Reject linked components (symbolic links and Windows junctions) below *base_dir*,
    optionally preserving access errors for diagnostics.

    ``..`` is collapsed first, so a missing directory followed by ``..``
    cannot hide a linked component after it.
    """
    try:
        return first_symlink_component(base_dir, path) is not None
    except OSError:
        if raise_on_error:
            raise
        return True
    except ValueError:
        return True


def _collapse_unlinked(path: Path) -> Path | None:
    """Collapse ``..`` in *path*; ``None`` when a ``..`` steps back over a symlink."""
    path = path if path.is_absolute() else Path.cwd() / path
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part == "..":
            if current.is_symlink():
                return None
            current = current.parent
        else:
            current /= part
    return current


def user_config_crosses_link(path: Path, root: Path) -> bool:
    """Whether a user config *path* is, or is reached through, a symlink.

    Below ``$HOME`` every component counts; outside it, *root* and everything
    below it. A ``..`` that steps back over a symlink counts too; otherwise the
    path is checked in its collapsed form, so an equivalent spelling passes.
    """
    home = _collapse_unlinked(Path.home())
    path, root = _collapse_unlinked(path), _collapse_unlinked(root)
    if home is None or path is None or root is None:
        return True
    if path.is_relative_to(home):
        return has_symlink_component(home, path)
    return has_symlink_component(root.parent, path)


def safe_rmtree(path: Path, base_dir: Path) -> None:
    """Remove *path* only if it resolves within *base_dir*.

    Drop-in replacement for ``shutil.rmtree(path)`` at sites where the
    target is derived from user-controlled input.  Uses retry logic for
    transient file-lock errors (e.g. antivirus scanning on Windows).
    """
    ensure_path_within(path, base_dir)
    robust_rmtree(path)
