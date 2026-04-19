"""Shared cleanup helper for stale deployed files.

Used by the post-install cleanup blocks in :mod:`apm_cli.commands.install`
to remove files previously deployed for a still-present package that the
current install no longer produces (e.g. after a rename or removal inside
the package). Centralises the safety gates so both the local-package and
remote-package cleanup paths apply the same rules.

Safety gates, in order:

1. **Path validation** -- :meth:`BaseIntegrator.validate_deploy_path` rejects
   path traversal and any path not under a known integration prefix.
2. **Directory rejection** -- APM-managed primitives are file-keyed
   (``SKILL.md`` for skills, individual ``.prompt.md`` / ``.instructions.md``
   files elsewhere). A ``deployed_files`` entry that resolves to a directory
   on disk is treated as untrusted (likely a poisoned lockfile entry under a
   broad prefix like ``.github/instructions/``) and refused.
3. **Provenance check** -- when the previous lockfile recorded a content
   hash for the file, the on-disk content must still match. If the user
   edited the file after APM deployed it the hash will differ and the
   deletion is skipped with a warning. Files without a recorded hash
   (legacy lockfiles) fall through and are deleted, preserving prior
   behaviour.

The helper does not own diagnostics-vs-logger output formatting policy;
it pushes warnings to *diagnostics* (collect-then-render) and informational
or progress lines through *logger*.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .base_integrator import BaseIntegrator


@dataclass
class CleanupResult:
    """Outcome of a stale-file cleanup pass for a single package."""

    deleted: List[str] = field(default_factory=list)
    """Workspace-relative paths actually removed from disk."""

    failed: List[str] = field(default_factory=list)
    """Paths that raised during ``unlink``/``rmtree`` and should be
    retained in ``deployed_files`` for retry on the next install."""

    skipped_user_edit: List[str] = field(default_factory=list)
    """Paths skipped because the on-disk content no longer matches the
    hash APM recorded at deploy time -- treated as user-edited."""

    skipped_unmanaged: List[str] = field(default_factory=list)
    """Paths refused by the safety gates (validation failure, directory
    entry, etc.). Not retained in ``deployed_files``."""

    deleted_targets: List[Path] = field(default_factory=list)
    """Absolute paths of deleted entries -- input to
    :meth:`BaseIntegrator.cleanup_empty_parents`."""


def remove_stale_deployed_files(
    stale_paths: Iterable[str],
    project_root: Path,
    *,
    dep_key: str,
    targets,
    diagnostics,
    logger,
    recorded_hashes: Optional[Dict[str, str]] = None,
) -> CleanupResult:
    """Remove APM-deployed files that are no longer produced by *dep_key*.

    Args:
        stale_paths: Workspace-relative paths flagged as stale by
            :func:`apm_cli.drift.detect_stale_files`.
        project_root: Project root the deletion is scoped within.
        dep_key: Unique key of the package these paths belong to (used
            for diagnostic attribution and verbose output).
        targets: Resolved target profiles for this install (passed
            through to :meth:`BaseIntegrator.validate_deploy_path`).
        diagnostics: ``DiagnosticCollector`` -- recoverable warnings
            (user-edit skip, unlink failure, refused directory entry)
            are pushed here.
        logger: ``CommandLogger`` (or subclass) for inline verbose
            output. May be ``None`` only in tests; production callers
            always pass an instance.
        recorded_hashes: Mapping from rel-path to ``"sha256:<hex>"`` as
            stored on the previous ``LockedDependency``. ``None`` (or
            empty) disables the per-file provenance check entirely --
            preserved for backward compat with pre-hash lockfiles.

    Returns:
        :class:`CleanupResult` describing what happened. The caller is
        responsible for any post-deletion bookkeeping (extending the
        new ``deployed_files`` list with ``failed`` so they are retried,
        invoking :meth:`BaseIntegrator.cleanup_empty_parents` on
        ``deleted_targets``, and reporting ``deleted`` count to the user
        via the appropriate logger method).
    """
    result = CleanupResult()
    recorded_hashes = recorded_hashes or {}

    for stale_path in sorted(stale_paths):
        # Gate 1: path validation (traversal, allowed prefix, in-tree).
        if not BaseIntegrator.validate_deploy_path(
            stale_path, project_root, targets=targets
        ):
            result.skipped_unmanaged.append(stale_path)
            continue

        stale_target = project_root / stale_path
        if not stale_target.exists():
            # File already gone -- treat as cleaned (no-op success).
            continue

        # Gate 2: directory rejection. APM-managed primitives are
        # file-keyed; a directory entry under an integration prefix is
        # almost certainly a poisoned lockfile entry that would rmtree
        # an entire user-managed subtree.
        if stale_target.is_dir() and not stale_target.is_symlink():
            result.skipped_unmanaged.append(stale_path)
            diagnostics.warn(
                (
                    f"Refused to remove directory entry {stale_path}: APM "
                    "only deletes individual files. If this entry was added "
                    "by a malicious or corrupt lockfile, remove it manually "
                    "from apm.lock.yaml."
                ),
                package=dep_key,
            )
            continue

        # Gate 3: provenance check. If APM recorded a content hash for
        # this file at deploy time and it no longer matches, the user
        # has edited the file -- skip deletion and warn so they can
        # decide what to do.
        expected_hash = recorded_hashes.get(stale_path)
        if expected_hash:
            try:
                from ..utils.content_hash import compute_file_hash

                actual_hash = compute_file_hash(stale_target)
            except Exception:
                actual_hash = None
            if actual_hash and actual_hash != expected_hash:
                result.skipped_user_edit.append(stale_path)
                diagnostics.warn(
                    (
                        f"Skipped removing {stale_path}: file has been "
                        "edited since APM deployed it. Delete it manually "
                        "if you no longer need it, or ignore this warning "
                        "to keep your changes."
                    ),
                    package=dep_key,
                )
                continue

        # All gates passed -- safe to delete.
        try:
            stale_target.unlink()
            result.deleted.append(stale_path)
            result.deleted_targets.append(stale_target)
        except Exception as exc:
            result.failed.append(stale_path)
            diagnostics.warn(
                (
                    f"Could not remove stale file {stale_path}: {exc}. "
                    "Path retained in lockfile; will retry on next "
                    "'apm install'."
                ),
                package=dep_key,
            )

    return result
