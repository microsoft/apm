"""Rollback-scoped staging for dependency resolution writes."""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from apm_cli.utils.path_security import ensure_path_within, safe_rmtree


class ResolutionStagingSession:
    """Track paths mutated during resolution and restore them on failure."""

    def __init__(self, apm_modules_dir: Path) -> None:
        """Create an empty staging session rooted below ``apm_modules``."""
        self._modules_dir = apm_modules_dir
        self._staging_root = apm_modules_dir / ".apm-resolution-staging" / uuid.uuid4().hex
        self._backups: dict[Path, Path | None] = {}
        self._relocations: list[tuple[Path, Path]] = []
        self._lock = threading.Lock()

    def prepare_path(self, path: Path) -> None:
        """Record *path* and preserve its pre-resolution contents if present."""
        resolved = ensure_path_within(path, self._modules_dir)
        with self._lock:
            if resolved in self._backups:
                return
            backup: Path | None = None
            if resolved.exists():
                resolved_base = ensure_path_within(self._modules_dir, self._modules_dir)
                relative = resolved.relative_to(resolved_base)
                backup = self._staging_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                resolved.replace(backup)
            self._backups[resolved] = backup

    def relocate_path(self, source: Path, destination: Path) -> None:
        """Move one existing package path and journal the rename for rollback."""
        ensure_path_within(source, self._modules_dir)
        ensure_path_within(destination, self._modules_dir)
        with self._lock:
            if source.parts == destination.parts:
                return
            if source.is_symlink():
                raise ValueError(
                    f"Refusing to migrate symlinked package directory: {source}. "
                    "Replace the symlink with the installed package and retry."
                )
            if not source.exists():
                raise FileNotFoundError(
                    f"Package directory disappeared before casing migration: {source}. "
                    "Run 'apm install' again."
                )
            if destination.exists():
                if not os.path.samefile(source, destination):
                    raise FileExistsError(
                        f"Package directory already exists at {destination}. "
                        "Inspect both case variants and remove the duplicate before retrying."
                    )
                self._replace_case_only(source, destination)
                self._relocations.append((source, destination))
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            self._relocations.append((source, destination))

    def commit(self) -> None:
        """Discard preserved pre-resolution contents after successful validation."""
        self._remove_staging_root()
        self._backups.clear()
        self._relocations.clear()

    def rollback(self) -> None:
        """Remove session-created paths and restore every replaced path."""
        with self._lock:
            for path, backup in reversed(self._backups.items()):
                if path.exists():
                    safe_rmtree(path, self._modules_dir)
                if backup is not None and backup.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    backup.replace(path)
            for source, destination in reversed(self._relocations):
                if destination.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if source.exists() and os.path.samefile(source, destination):
                        self._replace_case_only(destination, source)
                    else:
                        destination.replace(source)
                self._remove_empty_parents(destination.parent)
            self._remove_staging_root()
            self._backups.clear()
            self._relocations.clear()

    def _remove_empty_parents(self, path: Path) -> None:
        """Remove empty migration-created parents below ``apm_modules``."""
        ensure_path_within(path, self._modules_dir)
        while path != self._modules_dir and path.exists() and not any(path.iterdir()):
            path.rmdir()
            path = path.parent

    @staticmethod
    def _replace_case_only(source: Path, destination: Path) -> None:
        """Rename an entry through a sibling so case-insensitive filesystems update spelling."""
        try:
            source.replace(destination)
            if any(child.name == destination.name for child in destination.parent.iterdir()):
                return
        except OSError:
            if not source.exists() and destination.exists():
                return
        temporary = source.with_name(f".apm-case-migration-{uuid.uuid4().hex}")
        source.replace(temporary)
        try:
            temporary.replace(destination)
        except BaseException:
            temporary.replace(source)
            raise

    def _remove_staging_root(self) -> None:
        if self._staging_root.exists():
            safe_rmtree(self._staging_root, self._modules_dir)
        staging_parent = self._staging_root.parent
        if staging_parent.exists() and not any(staging_parent.iterdir()):
            staging_parent.rmdir()
