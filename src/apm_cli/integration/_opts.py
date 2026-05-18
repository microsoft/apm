from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class IntegrateOpts:
    """Bundled optional arguments for integrate_*_for_target methods."""

    force: bool = False
    managed_files: set[str] | None = None
    diagnostics: Any = None


@dataclass(frozen=True, slots=True)
class SyncRemoveOpts:
    """Bundled optional arguments for sync file removal helpers."""

    legacy_glob_dir: Any = None
    legacy_glob_pattern: str | None = None
    targets: Any = None
    logger: Any = None
    warn_fn: Any = None
