"""Rollback-scoped staging for dependency resolution writes."""

from __future__ import annotations

import contextlib
import os
import re
import threading
import uuid
from hashlib import sha256
from pathlib import Path

from filelock import FileLock, Timeout

from apm_cli.utils.path_security import ensure_path_within, safe_rmtree
from apm_cli.utils.staging_guard import STAGING_DIR_NAME

# Matches both the current 12-hex-char staging root name and the 32-hex-char
# (full uuid4().hex) name used before the MAX_PATH fix, so upgrading APM does
# not strand orphaned staging roots created by an older version (issue #2896).
_STAGING_NAME = re.compile(r"[0-9a-f]{12}|[0-9a-f]{32}")


class ResolutionStagingSession:
    """Track paths mutated during resolution and restore them on failure."""

    def __init__(self, apm_modules_dir: Path) -> None:
        """Create an empty staging session rooted below ``apm_modules``."""
        self._modules_dir = apm_modules_dir
        self._modules_existed = apm_modules_dir.exists()
        # 12 hex chars (48 bits) is ample entropy for one install session and
        # keeps staged paths well clear of Windows MAX_PATH (issue #2896).
        self._staging_root = apm_modules_dir / STAGING_DIR_NAME / uuid.uuid4().hex[:12]
        self._staging_lock_path = self._staging_root.with_suffix(".lock")
        self._staging_lock: FileLock | None = None
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
                self._acquire_staging_lock()
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
            self._acquire_staging_lock()
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
            except BaseException as activation_error:
                if backup is not None and backup.exists() and not resolved.exists():
                    try:
                        backup.replace(resolved)
                    except (OSError, ValueError) as recovery_error:
                        raise OSError(
                            f"Could not activate {staged} at {resolved}: {activation_error}. "
                            f"Could not restore prior package from {backup}: {recovery_error}. "
                            "Prior data is retained for recovery; close programs using these "
                            "paths and inspect the backup before retrying."
                        ) from activation_error
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

    def commit(self) -> list[tuple[Path, str]]:
        """Discard preserved pre-resolution contents after successful validation."""
        cleanup_issues: list[tuple[Path, str]] = []
        with self._lock:
            # Deleting backups is irreversible. Never roll back to a partially
            # removed backup if post-validation disposal itself fails.
            self._backups.clear()
            self._replacement_by_destination.clear()
            self._destination_by_replacement.clear()
            self._relocations.clear()
            try:
                self._remove_staging_root()
            except (OSError, ValueError) as exc:
                cleanup_issues.append(
                    (self._staging_root, f"could not remove committed staging: {exc}")
                )
            finally:
                cleanup_issues.extend(self._release_staging_lock())
        return cleanup_issues

    def remove_abandoned_roots(self) -> list[tuple[Path, str]]:
        """Remove inactive backups created by lock-aware APM versions."""
        try:
            return self._remove_abandoned_staging_roots()
        except Exception as exc:
            return [
                (
                    self._staging_root.parent,
                    f"unexpected cleanup error: {exc}",
                )
            ]

    def rollback(self) -> list[tuple[Path, str]]:
        """Remove session-created paths and restore every replaced path."""
        cleanup_issues: list[tuple[Path, str]] = []
        blocked_paths: set[Path] = set()
        blocked_ancestors: set[Path] = set()

        def block_recovery(*paths: Path) -> None:
            for path in paths:
                blocked_paths.add(path)
                blocked_ancestors.update(path.parents)

        def defer_overlapping_recovery(recovery_path: Path, *paths: Path) -> bool:
            if not any(
                path in blocked_ancestors or not blocked_paths.isdisjoint((path, *path.parents))
                for path in paths
            ):
                return False
            # A deferred ancestor also blocks its other children: restoring them
            # now would consume backups that the ancestor's retry can overwrite.
            block_recovery(*paths)
            cleanup_issues.append(
                (
                    recovery_path,
                    f"deferred recovery of {paths[0]} because overlapping recovery failed; "
                    "preserve the retained data and resolve the reported obstruction first",
                )
            )
            return True

        with self._lock:
            for path, backup in reversed(self._backups.items()):
                if defer_overlapping_recovery(backup or path, path):
                    continue
                try:
                    if backup is not None and backup.exists():
                        if path.exists():
                            safe_rmtree(path, self._modules_dir)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        backup.replace(path)
                    elif backup is None:
                        if path.exists():
                            safe_rmtree(path, self._modules_dir)
                        self._remove_empty_parents(path.parent)
                except (OSError, ValueError) as exc:
                    block_recovery(path)
                    cleanup_issues.append((backup or path, f"could not restore {path}: {exc}"))
            for source, destination in reversed(self._relocations):
                try:
                    recovery_paths = (
                        ensure_path_within(source, self._modules_dir),
                        ensure_path_within(destination, self._modules_dir),
                    )
                    if defer_overlapping_recovery(destination, *recovery_paths):
                        continue
                    if destination.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        if source.exists() and os.path.samefile(source, destination):
                            self._replace_case_only(destination, source)
                        else:
                            destination.replace(source)
                    self._remove_empty_parents(destination.parent)
                except (OSError, ValueError) as exc:
                    block_recovery(source, destination)
                    cleanup_issues.append((destination, f"could not restore {source}: {exc}"))
            try:
                # Never dispose of prior data after a failed restoration.
                if not cleanup_issues:
                    self._remove_staging_root()
            except (OSError, ValueError) as exc:
                cleanup_issues.append((self._staging_root, f"could not remove staging: {exc}"))
            finally:
                cleanup_issues.extend(self._release_staging_lock())
            try:
                if (
                    not self._modules_existed
                    and self._modules_dir.exists()
                    and not any(self._modules_dir.iterdir())
                ):
                    self._modules_dir.rmdir()
            except OSError as exc:
                cleanup_issues.append(
                    (self._modules_dir, f"could not remove empty directory: {exc}")
                )
            if not cleanup_issues:
                self._backups.clear()
                self._replacement_by_destination.clear()
                self._destination_by_replacement.clear()
                self._relocations.clear()
        return cleanup_issues

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
        # 16 hex chars (64 bits) of the digest is enough to make collisions
        # between the destinations staged in one session negligible, while
        # keeping staged paths well clear of Windows MAX_PATH (issue #2896).
        slot = sha256(relative).hexdigest()[:16]
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
            with contextlib.suppress(OSError):
                staging_parent.rmdir()

    def _acquire_staging_lock(self) -> None:
        """Mark this session active before preserving any package contents."""
        if self._staging_lock is not None:
            return
        staging_parent = self._staging_root.parent
        ensure_path_within(staging_parent, self._modules_dir)
        staging_parent.mkdir(parents=True, exist_ok=True)
        ensure_path_within(self._staging_lock_path, staging_parent)
        if self._staging_lock_path.is_symlink():
            raise ValueError(f"Refusing symlinked staging lock: {self._staging_lock_path}")
        lock = FileLock(str(self._staging_lock_path), thread_local=False)
        lock.acquire(timeout=0)
        self._staging_lock = lock

    def _release_staging_lock(self) -> list[tuple[Path, str]]:
        """Release and remove this session's staging activity marker."""
        if self._staging_lock is None:
            return []
        try:
            self._staging_lock.release()
        except Exception as exc:
            self._staging_lock = None
            return [(self._staging_lock_path, f"could not release activity lock: {exc}")]
        self._staging_lock = None
        try:
            ensure_path_within(self._staging_lock_path, self._staging_root.parent)
            self._staging_lock_path.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            return [(self._staging_lock_path, f"could not remove activity lock: {exc}")]
        staging_parent = self._staging_root.parent
        try:
            if staging_parent.exists() and not any(staging_parent.iterdir()):
                staging_parent.rmdir()
        except OSError as exc:
            return [(staging_parent, f"could not remove empty staging directory: {exc}")]
        return []

    def _remove_abandoned_staging_roots(self) -> list[tuple[Path, str]]:
        """Remove inactive, APM-named staging roots left by earlier runs."""
        issues: list[tuple[Path, str]] = []
        staging_parent = self._staging_root.parent
        if not staging_parent.is_dir() or staging_parent.is_symlink():
            return issues
        try:
            ensure_path_within(staging_parent, self._modules_dir)
            candidates = list(staging_parent.iterdir())
        except (OSError, ValueError) as exc:
            return [(staging_parent, f"could not inspect staging directory: {exc}")]
        for candidate in candidates:
            if (
                candidate != self._staging_lock_path
                and candidate.suffix == ".lock"
                and _STAGING_NAME.fullmatch(candidate.stem) is not None
                and candidate.is_file()
                and not candidate.is_symlink()
                and not candidate.with_suffix("").exists()
            ):
                orphan_lock = FileLock(str(candidate))
                try:
                    orphan_lock.acquire(timeout=0)
                except (OSError, Timeout):
                    continue
                try:
                    issues.append(
                        (
                            candidate,
                            "orphaned activity lock remains without a staging directory",
                        )
                    )
                finally:
                    with contextlib.suppress(Exception):
                        orphan_lock.release()
                continue
            if (
                candidate == self._staging_root
                or _STAGING_NAME.fullmatch(candidate.name) is None
                or candidate.is_symlink()
                or not candidate.is_dir()
            ):
                continue
            lock_path = candidate.with_suffix(".lock")
            if not lock_path.exists():
                issues.append(
                    (
                        candidate,
                        "legacy backup has no activity lock and may belong to a running install",
                    )
                )
                continue
            if lock_path.is_symlink() or not lock_path.is_file():
                issues.append((candidate, "activity lock is not a regular file"))
                continue
            try:
                ensure_path_within(lock_path, staging_parent)
                candidate_lock = FileLock(str(lock_path))
                candidate_lock.acquire(timeout=0)
            except (OSError, Timeout, ValueError):
                continue
            try:
                if (candidate / "backups").exists():
                    issues.append(
                        (
                            candidate,
                            "prior package backups remain; inspect and recover before deleting",
                        )
                    )
                    continue
                safe_rmtree(candidate, staging_parent)
            except (OSError, ValueError) as exc:
                issues.append((candidate, f"could not remove backup: {exc}"))
                continue
            finally:
                try:
                    candidate_lock.release()
                except Exception as exc:
                    issues.append((lock_path, f"could not release cleanup lock: {exc}"))
                else:
                    try:
                        lock_path.unlink(missing_ok=True)
                    except OSError as exc:
                        issues.append((lock_path, f"could not remove cleanup lock: {exc}"))
        with contextlib.suppress(OSError):
            if not any(staging_parent.iterdir()):
                staging_parent.rmdir()
        return issues
