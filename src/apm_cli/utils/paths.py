"""Cross-platform path utilities for APM CLI.

Centralises the resolve-then-relativise-then-posixify pattern so every
call site gets Windows-safe, forward-slash relative paths by default.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable, Container, Iterable, Iterator
from pathlib import Path, PosixPath, PurePath, PurePosixPath, PureWindowsPath, WindowsPath

from .realpath import resolve_path

# Marks an input the string comparison does not decide; pathlib decides it.
_UNDECIDED = object()
# Only pathlib's own classes: a subclass may override relative_to/is_relative_to.
_STANDARD_CLASSES = frozenset({PurePosixPath, PureWindowsPath, PosixPath, WindowsPath})


def _comparable(text: str, base_text: str, windows: bool) -> tuple[str, str, str] | None:
    """Both texts in comparison form with their separator, or ``None`` when undecided."""
    if not windows:
        if text[:1] != "/" or base_text[:1] != "/" or text[:2] == "//" or base_text[:2] == "//":
            return None
        return text, base_text, "/"
    for value in (text, base_text):
        if len(value) < 3 or value[1:3] != ":\\" or not value[0].isascii():
            return None
        if not value[0].isalpha():
            return None
    folded, base_folded = text.lower(), base_text.lower()
    if len(folded) != len(text) or len(base_folded) != len(base_text):
        return None
    return folded, base_folded, "\\"


def _remainder(path: PurePath, base: PurePath) -> object:
    """Text of *path* below *base*, ``None`` when it is not below, or ``_UNDECIDED``.

    Decides only the common shape: both paths the same standard pathlib class
    and absolute, rooted at ``/`` (POSIX) or at ``X:\\`` (Windows). Their
    ``str`` forms are already normalized, so containment is a prefix test on
    whole components, case-insensitive on Windows as pathlib compares. Every
    other shape (UNC, ``\\\\?\\``, ``//``, drive-relative, relative, mixed or
    subclassed classes) and any input whose lowercase changes length is
    ``_UNDECIDED``.
    """
    if type(path) is not type(base) or type(path) not in _STANDARD_CLASSES:
        return _UNDECIDED
    windows = isinstance(path, PureWindowsPath)
    text = str(path)
    compared = _comparable(text, str(base), windows)
    if compared is None:
        return _UNDECIDED
    folded, base_folded, sep = compared
    if folded == base_folded:
        return ""
    prefix = base_folded if base_folded.endswith(sep) else base_folded + sep
    if not folded.startswith(prefix):
        return None
    rest = text[len(prefix) :]
    return _UNDECIDED if windows and ":" in rest else rest


def relative_to(path: PurePath, base: PurePath) -> PurePath:
    """``path.relative_to(base)`` without pathlib's per-parent object walk.

    Same result type and value; when *path* is not below *base*, pathlib
    itself raises, so the exception is pathlib's on every Python version.
    """
    rest = _remainder(path, base)
    if rest is _UNDECIDED or rest is None:
        return path.relative_to(base)
    return type(path)(rest)


def relative_posix(path: PurePath, base: PurePath) -> str | None:
    """``path.relative_to(base).as_posix()``, or ``None`` when it is not below *base*."""
    rest = _remainder(path, base)
    if rest is None:
        return None
    if rest is _UNDECIDED:
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            return None
    if not rest:
        return "."
    return rest.replace("\\", "/") if isinstance(path, PureWindowsPath) else rest


def is_within(path: PurePath, base: PurePath) -> bool:
    """``path.is_relative_to(base)``: a pure comparison, no filesystem access."""
    rest = _remainder(path, base)
    if rest is _UNDECIDED:
        return path.is_relative_to(base)
    return rest is not None


def portable_relpath(path: Path, base: Path) -> str:
    """Return a forward-slash relative path, resolving both sides first.

    Handles Windows 8.3 short names (e.g. ``RUNNER~1`` vs ``runneradmin``)
    and ensures consistent POSIX output on every platform.

    When *path* is not under *base* (or resolution fails), falls back to
    a resolved absolute POSIX path.
    """
    try:
        relative = relative_posix(resolve_path(path), resolve_path(base))
    except (ValueError, OSError, RuntimeError):
        relative = None
    if relative is not None:
        return relative
    try:
        return resolve_path(path).as_posix()
    except (OSError, RuntimeError):
        return path.as_posix()


def portable_relpaths(paths: Iterable[Path], base: Path) -> list[str]:
    """:func:`portable_relpath` of each path against one *base*, resolving it once.

    Each path is still resolved at use, through :func:`resolve_path` as
    :func:`portable_relpath` does; only the shared base is not resolved again
    per path within this call.
    """
    try:
        resolved_base: Path | None = resolve_path(base)
    except (OSError, RuntimeError):
        resolved_base = None
    results: list[str] = []
    for path in paths:
        relative = None
        if resolved_base is not None:
            try:
                relative = relative_posix(resolve_path(path), resolved_base)
            except (ValueError, OSError, RuntimeError):
                relative = None
        if relative is None:
            try:
                relative = resolve_path(path).as_posix()
            except (OSError, RuntimeError):
                relative = path.as_posix()
        results.append(relative)
    return results


def portable_link_relpath(
    path: str | Path,
    base: str | Path,
    *,
    relpath: Callable[[str, str], str] = os.path.relpath,
) -> str | None:
    """Return a relative Markdown path, or ``None`` across incompatible drives."""
    try:
        relative = relpath(str(path), str(base))
    except (OSError, ValueError):
        return None
    return relative.replace("\\", "/")


def resolve_base_and_source_dirs(
    base_dir: str | Path, source_dir: str | Path | None
) -> tuple[Path, Path]:
    """Resolve a compiler's write-root and source-root into absolute Paths.

    ``base_dir`` is where outputs are written; ``source_dir`` is where
    primitives are read. ``source_dir`` defaults to ``base_dir`` for
    back-compat and only diverges under ``apm compile --root`` (writes
    redirected, sources stay in ``$PWD``). Resolution falls back to
    ``absolute()`` when the path does not yet exist on disk.
    """

    def _resolve(value: str | Path) -> Path:
        try:
            return Path(value).resolve()
        except (OSError, FileNotFoundError):
            return Path(value).absolute()

    resolved_base = _resolve(base_dir)
    resolved_source = resolved_base if source_dir is None else _resolve(source_dir)
    return resolved_base, resolved_source


def caseless(text: str) -> str:
    """Unicode canonical caseless key of *text*: NFD, case folding, then NFC.

    Decomposing first and recomposing after matches canonical caseless
    matching: folding can undo a composition the input had (U+0390 and the
    NFD capital iota with U+0308 U+0301 share one key). The one key for path
    spellings and containment (#5158, #5167).
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", text).casefold())


def spelling_key(path: str) -> str:
    """Caseless, ``/``-separated key of *path*.

    Spellings that differ only in letter case or Unicode normalization share
    one key; whether they are one entry is a property of the volume.
    """
    return caseless(path.replace("\\", "/"))


_spelling_key = spelling_key


def _same_entry(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


class SpellingAliasIndex:
    """Spelling-key index over one inventory of relative paths, built once.

    On case- or Unicode-normalization-insensitive filesystems (macOS,
    Windows) ``Guide.md`` and ``guide.md`` are one directory entry. Lookups
    hash the queried spelling and run ``os.path.samefile`` only against
    inventory paths whose spelling differs solely in case or normalization,
    so case-sensitive filesystems never alias and a lookup costs
    O(matching bucket), not O(inventory).
    """

    def __init__(self, candidates: Iterable[str]) -> None:
        self._buckets: dict[str, list[str]] = {}
        for candidate in candidates:
            if "://" not in candidate:
                self._buckets.setdefault(_spelling_key(candidate), []).append(candidate)

    def alias(self, rel_path: str, root: Path) -> str | None:
        """Return the inventory path naming the same entry as *rel_path* under *root*."""
        return next(self._aliases(rel_path, root), None)

    def aliases(self, rel_path: str, root: Path) -> tuple[str, ...]:
        """Return every other inventory path naming the same entry as *rel_path* under *root*."""
        return tuple(self._aliases(rel_path, root))

    def _aliases(self, rel_path: str, root: Path) -> Iterator[str]:
        for candidate in self._buckets.get(_spelling_key(rel_path), ()):
            if candidate != rel_path and _same_entry(root / rel_path, root / candidate):
                yield candidate


def physical_alias(rel_path: str, root: Path, candidates: Iterable[str]) -> str | None:
    """One-shot :class:`SpellingAliasIndex` lookup; reuse an index for repeated queries."""
    return SpellingAliasIndex(candidates).alias(rel_path, root)


def stored_spelling(root: Path, rel_path: str) -> str | None:
    """Return the spelling the filesystem stores for the existing entry *root*/*rel_path*.

    Walks *rel_path* one component at a time, keeping an exact directory
    entry and otherwise the single entry whose spelling differs only in case
    or normalization and that ``os.path.samefile`` confirms. Returns ``None``
    when the entry is missing or ambiguous. Costs one directory listing per
    component that is not spelled exactly, independent of any inventory.
    """
    current = root
    parts: list[str] = []
    for part in PurePosixPath(rel_path.replace("\\", "/")).parts:
        try:
            names = os.listdir(current)
        except OSError:
            return None
        if part not in names:
            key = _spelling_key(part)
            matches = [
                name
                for name in names
                if _spelling_key(name) == key and _same_entry(current / part, current / name)
            ]
            if len(matches) != 1:
                return None
            part = matches[0]
        parts.append(part)
        current = current / part
    return "/".join(parts) if parts else None


def stored_alias(root: Path, rel_path: str, inventory: Container[str]) -> str | None:
    """Return the *inventory* key naming the existing entry *root*/*rel_path* differently.

    Looks up the filesystem's stored spelling of the entry, and its NFC and
    NFD forms (HFS+ stores names decomposed while keys are usually
    precomposed), in *inventory*: a few set lookups, never an inventory
    scan. A key whose letter case differs from the stored entry (renamed by
    hand after deployment) is not matched, so such a file stays protected.
    """
    stored = stored_spelling(root, rel_path)
    if stored is None:
        return None
    queried = rel_path.replace("\\", "/")
    for spelling in dict.fromkeys(
        (stored, unicodedata.normalize("NFC", stored), unicodedata.normalize("NFD", stored))
    ):
        if spelling != queried and spelling in inventory:
            return spelling
    return None
