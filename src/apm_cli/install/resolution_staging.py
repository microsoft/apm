"""Rollback-scoped staging for dependency resolution writes."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from apm_cli.utils.path_security import ensure_path_within, safe_rmtree


class ResolutionStagingSession:
    """Track paths mutated during resolution and restore them on failure."""

    def __init__(self, apm_modules_dir: Path) -> None:
        """Create an empty staging session rooted below ``apm_modules``."""
        self._modules_dir = apm_modules_dir
        self._staging_root = apm_modules_dir / ".apm-resolution-staging" / uuid.uuid4().hex
        self._backups: dict[Path, Path | None] = {}
        self._lock = threading.Lock()

    def prepare_path(self, path: Path) -> None:
        """Record *path* and preserve its pre-resolution contents if present."""
        resolved = ensure_path_within(path, self._modules_dir)
        with self._lock:
            if resolved in self._backups:
                return
            backup: Path | None = None
            if resolved.exists():
                relative = resolved.relative_to(self._modules_dir.resolve())
                backup = self._staging_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                resolved.replace(backup)
            self._backups[resolved] = backup

    def commit(self) -> None:
        """Discard preserved pre-resolution contents after successful validation."""
        self._remove_staging_root()
        self._backups.clear()

    def rollback(self) -> None:
        """Remove session-created paths and restore every replaced path."""
        with self._lock:
            for path, backup in reversed(self._backups.items()):
                if path.exists():
                    safe_rmtree(path, self._modules_dir)
                if backup is not None and backup.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    backup.replace(path)
            self._remove_staging_root()
            self._backups.clear()

    def _remove_staging_root(self) -> None:
        if self._staging_root.exists():
            safe_rmtree(self._staging_root, self._modules_dir)
        staging_parent = self._staging_root.parent
        if staging_parent.exists() and not any(staging_parent.iterdir()):
            staging_parent.rmdir()


ResolveFunction = Callable[[Any, ResolutionStagingSession], None]


def transactional_resolution(resolve: ResolveFunction) -> Callable[[Any], None]:
    """Wrap a resolve operation in a rollback-scoped staging session."""

    @wraps(resolve)
    def wrapped(ctx: Any) -> None:
        session = ResolutionStagingSession(ctx.apm_modules_dir)
        try:
            resolve(ctx, session)
        except BaseException:
            session.rollback()
            raise
        session.commit()

    return wrapped
