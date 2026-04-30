"""Utilities for reading Spec Kit style constitution file."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional  # noqa: F401, UP035

from .constants import CONSTITUTION_RELATIVE_PATH

# Module-level cache: resolved base_dir -> constitution content (#171)
_constitution_cache: dict[Path, str | None] = {}


def clear_constitution_cache() -> None:
    """Clear the constitution read cache. Call in tests for isolation."""
    _constitution_cache.clear()


def find_constitution(base_dir: Path) -> Path:
    """Return path to constitution.md if present, else Path that does not exist.

    We keep logic trivial for Phase 0: fixed location under memory/.
    Later phases may support multiple shards / namespacing.
    """
    return base_dir / CONSTITUTION_RELATIVE_PATH


def read_constitution(base_dir: Path) -> str | None:
    """Read full constitution content if file exists.

    Results are cached by resolved base_dir for the lifetime of the process.

    Args:
        base_dir: Repository root path.
    Returns:
        Full file text or None if absent.
    """
    resolved = base_dir.resolve()
    if resolved in _constitution_cache:
        return _constitution_cache[resolved]
    path = find_constitution(base_dir)
    if not path.exists() or not path.is_file():
        _constitution_cache[resolved] = None
        return None
    try:
        content = path.read_text(encoding="utf-8")
        _constitution_cache[resolved] = content
        return content
    except OSError:
        _constitution_cache[resolved] = None
        return None
