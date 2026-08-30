"""Rollback-scoped staging for dependency resolution writes."""

from __future__ import annotations

import os
import threading
import uuid
from hashlib import sha256
from pathlib import Path

from apm_cli.utils.path_security import ensure_path_within, safe_rmtree


class ResolutionStagingSession:
    """Track paths mutated during resolution and restore them on failure."""

    def __init__(self, apm_modules_dir: Path) -> None:
        """Create an empty staging session rooted below ``apm_modules``."""
        self._modules_dir = apm_modules_dir
        self._modules_existed = apm_modules_dir.exists()
        self._staging_root = apm_modules_dir / ".apm-resolution-staging" / uuid.uuid4().hex
        self._backups: dict[Path, Path | None] = {}
        self._replacement_by_destination: dict[Path, Path] = {}
        self._destination_by_replacement: dict[Path, Path] = {}
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
                backup = self._staging_root / "backups" / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                resolved.replace(backup)
            self._backups[resolved] = backup

    def prepare_replacement(self, path: Path) -> Path:
        """Return an isolated download path without disturbing *path*."""
        resolved = ensure_path_within(path, self._modules_dir)
        replacement = self._isolated_staging_path("replacements", resolved)
        replacement = ensure_path_within(replacement, self._staging_root)
        with self._lock:
            if resolved in self._backups:
                raise RuntimeError(f"Path is already staged for replacement: {resolved}")
            if resolved in self._replacement_by_destination:
                raise RuntimeError(f"Path already has a replacement in progress: {resolved}")
            if replacement.exists():
                safe_rmtree(replacement, self._staging_root)
            replacement.parent.mkdir(parents=True, exist_ok=True)
            self._replacement_by_destination[resolved] = replacement
            self._destination_by_replacement[replacement] = resolved
        return replacement

    def publish_replacement(self, replacement: Path) -> Path:
        """Activate a prepared replacement and return its live path."""
        staged = ensure_path_within(replacement, self._staging_root)
        with self._lock:
            resolved = self._destination_by_replacement.get(staged)
            if resolved is None:
                raise RuntimeError(f"Replacement path is not reserved by this session: {staged}")
            if resolved in self._backups:
                raise RuntimeError(f"Path is already staged for replacement: {resolved}")
            if not staged.exists():
                raise FileNotFoundError(f"Replacement path was not materialized: {staged}")

            backup: Path | None = None
            if resolved.exists():
                backup = self._isolated_staging_path("backups", resolved)
                backup.parent.mkdir(parents=True, exist_ok=True)
                self._backups[resolved] = backup
                resolved.replace(backup)
            else:
                self._backups[resolved] = None
            try:
                resolved.parent.mkdir(parents=True, exist_ok=True)
                staged.replace(resolved)
            except BaseException:
                if backup is not None and backup.exists() and not resolved.exists():
                    backup.replace(resolved)
                raise
            self._replacement_by_destination.pop(resolved)
            self._destination_by_replacement.pop(staged)
            return resolved

    def discard_replacement(self, replacement: Path) -> None:
        """Discard a failed prepared replacement without touching the live path."""
        staged = ensure_path_within(replacement, self._staging_root)
        with self._lock:
            resolved = self._destination_by_replacement.get(staged)
            if resolved is None:
                return
            if staged.exists():
                safe_rmtree(staged, self._staging_root)
            self._replacement_by_destination.pop(resolved)
            self._destination_by_replacement.pop(staged)

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
        self._replacement_by_destination.clear()
        self._destination_by_replacement.clear()
        self._relocations.clear()

    def rollback(self) -> None:
        """Remove session-created paths and restore every replaced path."""
        with self._lock:
            for path, backup in reversed(self._backups.items()):
                if backup is not None and backup.exists():
                    if path.exists():
                        safe_rmtree(path, self._modules_dir)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    backup.replace(path)
                elif backup is None:
                    if path.exists():
                        safe_rmtree(path, self._modules_dir)
                    self._remove_empty_parents(path.parent)
            for source, destination in reversed(self._relocations):
                if destination.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if source.exists() and os.path.samefile(source, destination):
                        self._replace_case_only(destination, source)
                    else:
                        destination.replace(source)
                self._remove_empty_parents(destination.parent)
            self._remove_staging_root()
            if (
                not self._modules_existed
                and self._modules_dir.exists()
                and not any(self._modules_dir.iterdir())
            ):
                self._modules_dir.rmdir()
            self._backups.clear()
            self._replacement_by_destination.clear()
            self._destination_by_replacement.clear()
            self._relocations.clear()

    def _remove_empty_parents(self, path: Path) -> None:
        """Remove empty migration-created parents below ``apm_modules``."""
        ensure_path_within(path, self._modules_dir)
        while path != self._modules_dir and path.exists() and not any(path.iterdir()):
            path.rmdir()
            path = path.parent

    def _isolated_staging_path(self, bucket: str, destination: Path) -> Path:
        """Return an opaque slot so nested destinations never overlap."""
        modules = ensure_path_within(self._modules_dir, self._modules_dir)
        relative = destination.relative_to(modules).as_posix().encode("utf-8")
        slot = sha256(relative).hexdigest()
        return self._staging_root / bucket / slot

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
