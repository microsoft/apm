"""Post-deps local content: stale cleanup + lockfile persistence.

Handles the second half of the local ``.apm/`` content integration
lifecycle that was previously an inline block in the Click ``install()``
handler (lines 653-795 pre-F3).  The first half -- actually deploying
the primitives -- is handled by ``_integrate_root_project`` in the
``integrate`` phase.

Two responsibilities:

1. **Stale cleanup** -- remove files deployed by a *previous* local
   integration that are no longer produced.  Only runs when the
   current integration completed without errors (avoids deleting files
   that failed to re-deploy).  All deletions route through the
   canonical security chokepoint
   ``apm_cli.integration.cleanup.remove_stale_deployed_files`` (PR #762).

2. **Lockfile persistence** -- read-modify-write the lockfile to persist
   ``local_deployed_files`` and per-file content hashes.  Runs after the
   dep lockfile phase has already written dependency data; this phase
   simply augments the on-disk lockfile with the local fields.

Scope guard: this phase only runs for ``InstallScope.PROJECT``.  User-
scope installs do not track local deployed files (matching pre-refactor
behavior).
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _persist_local_lockfile(ctx: InstallContext) -> None:
    from apm_cli.deps.lockfile import LockFile as lock_file
    from apm_cli.deps.lockfile import get_lockfile_path as get_lockfile_path
    from apm_cli.install.phases.lockfile import compute_deployed_hashes as hash_deployed

    lock_path = get_lockfile_path(ctx.apm_dir)
    persist_lock = lock_file.read(lock_path) or lock_file()
    persist_lock.local_deployed_files = sorted(ctx.local_deployed_files)
    persist_lock.local_deployed_file_hashes = hash_deployed(
        ctx.local_deployed_files, ctx.project_root
    )
    existing_for_cmp = lock_file.read(lock_path)
    if not existing_for_cmp or not persist_lock.is_semantically_equivalent(existing_for_cmp):
        persist_lock.save(lock_path)


def run(ctx: InstallContext) -> None:
    """Execute local content stale cleanup and lockfile persistence.

    Reads ``ctx.local_deployed_files``, ``ctx.old_local_deployed``,
    ``ctx.local_content_errors_before``, ``ctx.diagnostics``,
    ``ctx.targets``, ``ctx.logger``, ``ctx.project_root``, ``ctx.apm_dir``.

    Mutates ``ctx.local_deployed_files`` (appends failed cleanup paths).
    """
    from apm_cli.core.scope import InstallScope

    # Scope guard: only PROJECT scope tracks local deployed files.
    if ctx.scope is not InstallScope.PROJECT:
        return

    # Skip if there is no local content (current or previous).
    if not ctx.local_deployed_files and not ctx.old_local_deployed:
        return

    diagnostics = ctx.diagnostics
    logger = ctx.logger

    # ------------------------------------------------------------------
    # Stale cleanup: remove files deployed by previous local integration
    # that are no longer produced.  Only run when integration completed
    # without errors to avoid deleting files that failed to re-deploy.
    # ------------------------------------------------------------------
    _local_had_errors = (
        diagnostics is not None and diagnostics.error_count > ctx.local_content_errors_before
    )

    if ctx.old_local_deployed and not _local_had_errors:
        from apm_cli.integration.base_integrator import BaseIntegrator
        from apm_cli.integration.cleanup import CleanupOpts, remove_stale_deployed_files

        _stale = builtins.set(ctx.old_local_deployed) - builtins.set(ctx.local_deployed_files)
        if _stale:
            # Get recorded hashes from the pre-install lockfile for
            # content-hash provenance verification.
            _prev_hashes: dict = {}
            if ctx.existing_lockfile:
                _prev_hashes = dict(ctx.existing_lockfile.local_deployed_file_hashes)

            _cleanup_result = remove_stale_deployed_files(
                _stale,
                ctx.project_root,
                opts=CleanupOpts(
                    dep_key="<local .apm/>",
                    targets=ctx.targets,
                    diagnostics=diagnostics,
                    recorded_hashes=_prev_hashes,
                ),
            )
            # Failed paths stay in lockfile so we retry next time.
            ctx.local_deployed_files.extend(_cleanup_result.failed)
            if _cleanup_result.deleted_targets:
                BaseIntegrator.cleanup_empty_parents(
                    _cleanup_result.deleted_targets, ctx.project_root
                )
            for _skipped in _cleanup_result.skipped_user_edit:
                if logger:
                    logger.cleanup_skipped_user_edit(_skipped, "<local .apm/>")
            if logger:
                logger.stale_cleanup("<local .apm/>", len(_cleanup_result.deleted))

    _persist_local_lockfile(ctx)
