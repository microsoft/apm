"""Cross-platform path utilities for APM CLI.

Centralises the resolve-then-relativise-then-posixify pattern so every
call site gets Windows-safe, forward-slash relative paths by default.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable, Container, Iterable
from pathlib import Path, PurePosixPath


def portable_relpath(path: Path, base: Path) -> str:
    """Return a forward-slash relative path, resolving both sides first.

    Handles Windows 8.3 short names (e.g. ``RUNNER~1`` vs ``runneradmin``)
    and ensures consistent POSIX output on every platform.

    When *path* is not under *base* (or resolution fails), falls back to
    a resolved absolute POSIX path.
    """
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, OSError, RuntimeError):
        try:
            return path.resolve().as_posix()
        except (OSError, RuntimeError):
            return path.as_posix()


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


def _spelling_key(rel_path: str) -> str:
    return unicodedata.normalize("NFC", rel_path.replace("\\", "/")).casefold()


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
        for candidate in self._buckets.get(_spelling_key(rel_path), ()):
            if candidate != rel_path and _same_entry(root / rel_path, root / candidate):
                return candidate
        return None


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
