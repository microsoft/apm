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

The helper records cleanup diagnostics via *diagnostics* (collect-then-
render) and returns a :class:`CleanupResult` summarizing deleted, failed,
and skipped paths. Callers remain responsible for any informational,
progress, or warning logging based on that result -- the helper itself
takes no logger.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional  # noqa: F401, UP035

from .base_integrator import BaseIntegrator


@dataclass
class CleanupResult:
    """Outcome of a stale-file cleanup pass for a single package."""

    deleted: list[str] = field(default_factory=list)
    """Workspace-relative paths actually removed from disk."""

    failed: list[str] = field(default_factory=list)
    """Paths that raised during ``unlink``/``rmtree`` and should be
    retained in ``deployed_files`` for retry on the next install."""

    skipped_user_edit: list[str] = field(default_factory=list)
    """Paths skipped because the on-disk content no longer matches the
    hash APM recorded at deploy time -- treated as user-edited."""

    skipped_unmanaged: list[str] = field(default_factory=list)
    """Paths refused by the safety gates (validation failure, directory
    entry, etc.). Not retained in ``deployed_files``."""

    deleted_targets: list[Path] = field(default_factory=list)
    """Absolute paths of deleted entries -- input to
    :meth:`BaseIntegrator.cleanup_empty_parents`."""


def remove_stale_deployed_files(
    stale_paths: Iterable[str],
    project_root: Path,
    *,
    dep_key: str,
    targets,
    diagnostics,
    recorded_hashes: dict[str, str] | None = None,
    failed_path_retained: bool = True,
) -> CleanupResult:
    """Remove APM-deployed files that are no longer produced by *dep_key*.

    Args:
        stale_paths: Workspace-relative paths flagged as stale by
            :func:`apm_cli.drift.detect_stale_files` (intra-package
            renames/removals) or :func:`apm_cli.drift.detect_orphans`
            (whole package removed from the manifest).
        project_root: Project root the deletion is scoped within.
        dep_key: Unique key of the package these paths belong to (used
            for diagnostic attribution).
        targets: Resolved target profiles for this install (passed
            through to :meth:`BaseIntegrator.validate_deploy_path`).
        diagnostics: ``DiagnosticCollector`` -- recoverable warnings
            (user-edit skip, unlink failure, refused directory entry)
            are pushed here.
        recorded_hashes: Mapping from rel-path to ``"sha256:<hex>"`` as
            stored on the previous ``LockedDependency``. ``None`` (or
            empty) disables the per-file provenance check entirely --
            preserved for backward compat with pre-hash lockfiles.
        failed_path_retained: When ``True`` (default, intra-package
            stale cleanup) the failure diagnostic tells the user APM
            will retry on the next install -- the caller is expected
            to re-insert ``result.failed`` into the new
            ``deployed_files``. When ``False`` (orphan cleanup) the
            owning package is being removed from the lockfile so a
            failed path cannot be retained; the diagnostic instructs
            the user to remove the file manually instead.

    Returns:
        :class:`CleanupResult` describing what happened. The caller is
        responsible for any post-deletion bookkeeping (extending the
        new ``deployed_files`` list with ``failed`` so they are retried,
        invoking :meth:`BaseIntegrator.cleanup_empty_parents` on
        ``deleted_targets``, calling
        :meth:`InstallLogger.cleanup_skipped_user_edit` for each entry
        in ``skipped_user_edit`` so the inline yellow warning renders,
        and reporting ``deleted`` count to the user via
        :meth:`InstallLogger.stale_cleanup` /
        :meth:`InstallLogger.orphan_cleanup`).
    """
    result = CleanupResult()
    recorded_hashes = recorded_hashes or {}

    # Lazy-resolve cowork root at most once per invocation (same
    # pattern as sync_remove_files in base_integrator.py -- PR #926 P4).
    _cowork_root_resolved: bool = False
    _cowork_root_cached: Path | None = None
    _cowork_orphans_skipped: int = 0
    _cowork_resolve_errors: int = 0

    for stale_path in sorted(stale_paths):
        # ── Cowork:// paths ──────────────────────────────────────────
        # Handled BEFORE validate_deploy_path because that method
        # hard-rejects cowork:// when the OneDrive root is unavailable
        # (returning False ⇒ skipped_unmanaged).  For cleanup we want
        # the gentler "retain in *failed* for retry" behaviour, so we
        # do equivalent security checks (no traversal, known prefix,
        # containment via from_lockfile_path) ourselves.
        from .copilot_cowork_paths import COWORK_URI_SCHEME

        if stale_path.startswith(COWORK_URI_SCHEME):
            # Basic security: reject path-traversal components.
            if ".." in stale_path:
                result.skipped_unmanaged.append(stale_path)
                continue
            # Verify the path starts with a known integration prefix.
            from .targets import get_integration_prefixes

            if not stale_path.startswith(get_integration_prefixes(targets=targets)):
                result.skipped_unmanaged.append(stale_path)
                continue
            # Resolve the cowork:// URI to a real filesystem path.
            try:
                if not _cowork_root_resolved:
                    from .copilot_cowork_paths import (
                        resolve_copilot_cowork_skills_dir,
                    )

                    _cowork_root_cached = resolve_copilot_cowork_skills_dir()
                    _cowork_root_resolved = True
                if _cowork_root_cached is None:
                    # OneDrive unavailable -- retain lockfile entry so a
                    # later install with a configured root can clean up.
                    _cowork_orphans_skipped += 1
                    result.failed.append(stale_path)
                    continue
                from .copilot_cowork_paths import from_lockfile_path

                stale_target = from_lockfile_path(stale_path, _cowork_root_cached)
            except Exception:
                # Containment violation or malformed path -- retain in
                # lockfile for manual inspection.
                _cowork_resolve_errors += 1
                result.failed.append(stale_path)
                continue
        else:
            # ── Non-cowork paths ─────────────────────────────────────
            # Gate 1: path validation (traversal, allowed prefix, in-tree).
            if not BaseIntegrator.validate_deploy_path(stale_path, project_root, targets=targets):
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
        # decide what to do. Fails CLOSED on hash-read errors: if APM
        # cannot prove the file is unmodified (PermissionError, race,
        # etc.) we keep it rather than risk destroying user work.
        expected_hash = recorded_hashes.get(stale_path)
        if expected_hash:
            try:
                from ..utils.content_hash import compute_file_hash

                actual_hash = compute_file_hash(stale_target)
            except Exception as _hash_exc:
                result.skipped_user_edit.append(stale_path)
                diagnostics.warn(
                    (
                        f"Skipped removing {stale_path}: could not verify "
                        f"file content ({_hash_exc.__class__.__name__}). "
                        "Inspect the file and delete it manually if no "
                        "longer needed."
                    ),
                    package=dep_key,
                )
                continue
            if actual_hash != expected_hash:
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
            if failed_path_retained:
                diagnostics.warn(
                    (
                        f"Could not remove stale file {stale_path}: {exc}. "
                        "Path retained in lockfile; will retry on next "
                        "'apm install'."
                    ),
                    package=dep_key,
                )
            else:
                diagnostics.warn(
                    (
                        f"Could not remove orphaned file {stale_path}: {exc}. "
                        "The owning package is no longer in apm.yml -- "
                        "delete the file manually."
                    ),
                    package=dep_key,
                )

    # One-time warnings for cowork edge cases (mirrors sync_remove_files).
    if _cowork_orphans_skipped > 0:
        diagnostics.warn(
            (
                f"Cowork: skipping {_cowork_orphans_skipped} stale lockfile "
                f"{'entry' if _cowork_orphans_skipped == 1 else 'entries'}"
                " -- OneDrive path not detected.\n"
                "Run: apm config set copilot-cowork-skills-dir <path>  "
                "(or set APM_COPILOT_COWORK_SKILLS_DIR)\n"
                "to clean up these entries on the next install/uninstall."
            ),
            package=dep_key,
        )
    if _cowork_resolve_errors > 0:
        diagnostics.warn(
            (
                f"Cowork: {_cowork_resolve_errors} lockfile "
                f"{'entry' if _cowork_resolve_errors == 1 else 'entries'}"
                " failed path resolution (containment violation or "
                "malformed path). Paths retained for manual inspection."
            ),
            package=dep_key,
        )

    return result
