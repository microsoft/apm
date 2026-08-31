"""Deterministic, fail-closed repository inventory for the architecture linter.

The inventory is the single canonical listing of "every file the linter is
allowed to know about". It is built exactly once per run, walked with a
sorted, excluded-roots-aware traversal, and handed to every rule as the same
immutable tuple of repository-relative POSIX paths -- so no rule ever
disagrees with another about what the repository contains.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Roots holding generated, vendored, or environment-local content. Never
# walked; a huge fraction of a repository's bytes typically live here and
# none of it is architecture-relevant source.
EXCLUDED_ROOTS: tuple[str, ...] = (
    ".git",
    ".venv",
    "venv",
    "env",
    "ENV",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "build",
    "dist",
    "htmlcov",
    "mutants",
    ".idea",
    ".vscode",
)

# These names are generated wherever they occur.  The remaining names in
# EXCLUDED_ROOTS are excluded only when they are repository-root children:
# legitimate tracked source can live below paths such as tests/red_team/env/.
_EXCLUDED_ANYWHERE: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)
_EXCLUDED_AT_ROOT: frozenset[str] = frozenset(EXCLUDED_ROOTS) - _EXCLUDED_ANYWHERE

# Suffixes that mark a directory as generated regardless of its base name
# (e.g. "apm_cli.egg-info").
_EXCLUDED_SUFFIXES: tuple[str, ...] = (".egg-info",)

# Roots and files that MUST exist for the inventory to be trustworthy.
# Missing any of these fails closed rather than silently linting a partial,
# possibly-wrong-checkout tree.
REQUIRED_ROOTS: tuple[str, ...] = (
    "src",
    "scripts",
    "tests",
    ".apm",
)
REQUIRED_FILES: tuple[str, ...] = (
    "pyproject.toml",
    ".apm/architecture/owners/index.json",
)


class InventoryError(RuntimeError):
    """Raised when the repository tree cannot be trusted for linting."""


@dataclass(frozen=True)
class Inventory:
    """The sorted, deterministic set of repository-relative file paths."""

    root: str
    files: tuple[str, ...]
    excluded_roots: tuple[str, ...]


def is_safe_repository_relative_path(value: object) -> bool:
    """Return whether `value` is a canonical repository-relative POSIX path."""
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if not value.isascii() or any(
        ord(character) < 32 or ord(character) > 126 for character in value
    ):
        return False
    if "\\" in value or PurePosixPath(value).is_absolute():
        return False
    parts = value.split("/")
    return all(part and part not in {".", ".."} for part in parts)


def _is_excluded_name(name: str) -> bool:
    return name in _EXCLUDED_ANYWHERE or any(name.endswith(suffix) for suffix in _EXCLUDED_SUFFIXES)


def _is_excluded(relative_parts: tuple[str, ...]) -> bool:
    return (bool(relative_parts) and relative_parts[0] in _EXCLUDED_AT_ROOT) or any(
        _is_excluded_name(part) for part in relative_parts
    )


def _check_required_anchors(root: Path) -> None:
    missing_roots = [name for name in REQUIRED_ROOTS if not (root / name).is_dir()]
    if missing_roots:
        raise InventoryError(f"required root(s) missing: {', '.join(sorted(missing_roots))}")
    missing_files = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing_files:
        raise InventoryError(f"required file(s) missing: {', '.join(sorted(missing_files))}")


def _relative_path(root: Path, dirpath: str, filename: str) -> str:
    relative_dir = Path(dirpath).relative_to(root)
    if relative_dir == Path("."):
        return filename
    return (relative_dir / filename).as_posix()


def _raise_walk_error(error: OSError) -> None:
    raise InventoryError(f"cannot inventory repository: {error}") from error


def build_inventory(root: Path) -> Inventory:
    """Walk `root` exactly once, deterministically, failing closed on anchors.

    Directory traversal order from :func:`os.walk` is not itself guaranteed
    sorted, so `dirnames` is sorted in place before descent and the final
    file list is sorted again before returning -- the caller always receives
    the same tuple for the same tree.
    """
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise InventoryError(f"root is not a directory: {resolved_root}")

    _check_required_anchors(resolved_root)

    collected: list[str] = []
    for dirpath, dirnames, filenames in os.walk(
        resolved_root,
        onerror=_raise_walk_error,
    ):
        relative_parts = Path(dirpath).relative_to(resolved_root).parts
        if _is_excluded(relative_parts):
            dirnames[:] = []
            continue
        dirnames[:] = sorted(name for name in dirnames if not _is_excluded((*relative_parts, name)))
        for filename in filenames:
            if _is_excluded((*relative_parts, filename)):
                continue
            collected.append(_relative_path(resolved_root, dirpath, filename))

    files = tuple(sorted(collected))
    if not files:
        raise InventoryError("inventory is empty; refusing to lint nothing")
    return Inventory(
        root=resolved_root.as_posix(),
        files=files,
        excluded_roots=EXCLUDED_ROOTS,
    )
