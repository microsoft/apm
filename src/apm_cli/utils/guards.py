"""Read-only project-tree guard for drift detection.

When ``apm audit`` runs the install pipeline against a scratch directory
to compute drift, the working tree must remain untouched. ``_ReadOnlyProjectGuard``
takes a stat snapshot of every protected path on entry and asserts no
mutation occurred on exit. Any divergence raises ``ProtectedPathMutationError``.

This is a defense-in-depth check: the primary mechanism is redirecting all
writes via ``project_root=scratch_root``. The guard catches accidental
direct-path writes that bypass the redirection.
"""

from __future__ import annotations

import os
from pathlib import Path


class ProtectedPathMutationError(RuntimeError):
    """Raised when a path under guard was mutated during drift replay."""


def _snapshot(paths: list[Path]) -> dict[Path, tuple[int, int] | None]:
    """Capture (mtime_ns, size) for each path, or ``None`` if missing.

    Symlinks are followed; missing paths record ``None`` so they may
    legitimately remain absent without triggering the guard.
    """
    snap: dict[Path, tuple[int, int] | None] = {}
    for p in paths:
        try:
            st = p.stat()
            snap[p] = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            snap[p] = None
        except OSError:
            snap[p] = None
    return snap


def _walk_protected(roots: list[Path]) -> list[Path]:
    """Enumerate every regular file under each root (recursive).

    Missing roots are silently dropped. Symlinks are NOT recursed into
    to avoid infinite loops.
    """
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            files.append(root)
            continue
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for fn in filenames:
                files.append(Path(dirpath) / fn)
    return files


class _ReadOnlyProjectGuard:
    """Context manager that snapshots protected paths and asserts no mutation.

    Usage::

        with _ReadOnlyProjectGuard(project_root, [".apm", "apm.lock.yaml", ".github"]):
            run_replay(...)  # writes only to scratch_root, never to project_root

    On exit, every snapshotted file is re-stat'd. Any (mtime, size) divergence
    OR a previously-present file going missing OR a previously-missing file
    appearing raises ``ProtectedPathMutationError`` listing the offending paths.

    The guard tolerates files that were missing on entry AND missing on exit
    (no-op) -- this is important for fresh-repo scenarios where ``.apm/`` may
    not exist yet.
    """

    def __init__(self, project_root: Path, protected_subpaths: list[str]) -> None:
        self.project_root = project_root.resolve()
        self.protected_roots = [self.project_root / sp for sp in protected_subpaths]
        self._snapshot: dict[Path, tuple[int, int] | None] = {}

    def __enter__(self) -> _ReadOnlyProjectGuard:
        files = _walk_protected(self.protected_roots)
        self._snapshot = _snapshot(files)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Re-walk in case new files appeared (or files vanished).
        current_files = set(_walk_protected(self.protected_roots))
        snapshotted_files = set(self._snapshot.keys())

        violations: list[str] = []

        # Newly-appeared files under protected roots are violations.
        for new_path in current_files - snapshotted_files:
            violations.append(f"created: {new_path}")

        # Snapshotted files that vanished or changed.
        for path, prev in self._snapshot.items():
            try:
                st = path.stat()
                cur = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                cur = None
            except OSError:
                cur = None

            if prev is None and cur is None:
                continue  # missing -> still missing: fine
            if prev is None and cur is not None:
                violations.append(f"created: {path}")
            elif prev is not None and cur is None:
                violations.append(f"deleted: {path}")
            elif prev != cur:
                violations.append(f"modified: {path}")

        if violations and exc_type is None:
            # Only raise if no other exception is propagating -- don't mask
            # the original error.
            raise ProtectedPathMutationError(
                "Drift replay mutated protected project paths:\n  - "
                + "\n  - ".join(sorted(violations))
            )
