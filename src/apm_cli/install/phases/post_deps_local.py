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

Both scopes persist: at user scope the root is ``~/.apm`` and the claims go
to the user lockfile, so a later ``install -g`` can update APM's own files
when a user-root primitive changes (#3772).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.core.scope import is_user_scope

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def run(ctx: InstallContext) -> None:
    """Execute local content stale cleanup and lockfile persistence.

    Reads ``ctx.local_deployed_files``, ``ctx.old_local_deployed``,
    ``ctx.local_content_errors_before``, ``ctx.diagnostics``,
    ``ctx.targets``, ``ctx.logger``, ``ctx.project_root``, ``ctx.apm_dir``.

    Mutates ``ctx.local_deployed_files`` (appends failed cleanup paths).
    """
    # Skip if there is no local content (current or previous). Root hooks that
    # only merge into shared configs still need a lockfile so that
    # `apm audit --ci` replays them (#4033).
    if not ctx.local_deployed_files and not ctx.old_local_deployed and not ctx.local_hooks_merged:
        return

    diagnostics = ctx.diagnostics
    logger = ctx.logger

    _local_had_errors = (
        diagnostics is not None and diagnostics.error_count > ctx.local_content_errors_before
    )

    # ------------------------------------------------------------------
    # Reconciliation and persistence: route stale local content through the
    # same target-contraction owner that already repaired the dependency and
    # local compatibility views in the lockfile phase. A separate cleanup pass
    # using only the active targets can reject an inactive target's valid path
    # as unmanaged and reintroduce the ghost that the canonical owner removed.
    # ------------------------------------------------------------------
    from apm_cli.deps.lockfile import LockFile as _LF
    from apm_cli.deps.lockfile import get_lockfile_path as _get_lfp
    from apm_cli.install.phases.lockfile import compute_deployed_hashes as _hash_deployed

    _lock_path = _get_lfp(ctx.apm_dir)
    _persist_lock = _LF.read(_lock_path) or _LF()
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    _prior_ledger = DeploymentLedgerCodec.from_lockfile(_persist_lock)
    # Target-scoped union: preserve project-root files deployed by OTHER
    # targets (a prior install) rather than clobbering them. Symmetric with
    # the per-dependency reconciliation in phases/lockfile.py and with
    # on-disk stale cleanup, so a multi-target deploy keeps content-integrity
    # coverage for every committed deploy target (issue #1716).
    from apm_cli.install.manifest_reconcile import reconcile_deployed_block
    from apm_cli.install.phases.cleanup import kept_prior_values
    from apm_cli.install.phases.targets import declared_target_profiles

    # A path a dependency of this install now claims leaves the self entry
    # without being treated as stale, so its surviving file is not deleted.
    _dependency_claimed = _dependency_claimed_paths(ctx)
    _prior_files = [
        path for path in _persist_lock.local_deployed_files if path not in _dependency_claimed
    ]
    _prior_hashes = {
        path: content_hash
        for path, content_hash in _persist_lock.local_deployed_file_hashes.items()
        if path not in _dependency_claimed
    }
    # Members this run skipped keep their prior rows and hashes (#4230).
    _kept = kept_prior_values(ctx, _prior_files)
    # A kept edited local output never hashes the edit (#4873), as for dependencies,
    # and a kept path the project did not already claim gains no local claim.
    _retained = getattr(ctx, "package_cleanup_retained", {}).get(".", {})
    _claimed = set(_prior_files)
    _current_files = sorted(
        path
        for path in ctx.local_deployed_files
        if path not in _dependency_claimed and (path not in _retained or path in _claimed)
    )
    _current_hashes = _hash_deployed(
        (path for path in _current_files if path not in _retained), ctx.project_root
    )
    _ghost_count = 0

    def _log_local_ghost_drop(path: str) -> None:
        nonlocal _ghost_count
        _ghost_count += 1
        if logger:
            logger.verbose_detail(
                f"Removed stale local lockfile path {path} (target not declared in apm.yml)"
            )

    from apm_cli.install.phases.cleanup import deployment_evidence
    from apm_cli.integration.cleanup import CleanupResult

    def _surface_local_cleanup(cleanup: CleanupResult) -> None:
        if logger is None:
            return
        for skipped in cleanup.skipped_user_edit:
            logger.cleanup_skipped_user_edit(skipped, "<local .apm/>")
        logger.stale_cleanup("<local .apm/>", cleanup)

    _files, _hashes, _ledger = reconcile_deployed_block(
        project_root=ctx.project_root,
        dep_key="<local .apm/>",
        current_files=_current_files,
        current_hashes=_current_hashes,
        prior_files=_prior_files,
        prior_hashes=_prior_hashes,
        active_targets=ctx.targets,
        declared_targets=declared_target_profiles(ctx),
        diagnostics=ctx.diagnostics,
        evidence=deployment_evidence(ctx),
        on_ghost_drop=_log_local_ghost_drop,
        on_cleanup=_surface_local_cleanup,
        prior_ledger=_prior_ledger,
        cleanup_retained_hashes=_retained,
        current_run_trusted=not _local_had_errors,
        retained_values=frozenset(_kept),
        include_ledger=True,
        user_scope=is_user_scope(getattr(ctx, "scope", None)),
    )

    DeploymentLedgerCodec.replace_context_local_files(ctx, sorted(_files))
    DeploymentLedgerCodec.replace_legacy_owner(
        _persist_lock,
        ".",
        sorted(_files),
        _hashes,
        provenance=_ledger,
        current_targets=ctx.targets or (),
    )
    if logger and _ghost_count:
        noun = "entry" if _ghost_count == 1 else "entries"
        logger.info(f"Repaired {_ghost_count} inactive-target local lockfile {noun}")
    # Only write if changed.
    _existing_for_cmp = _LF.read(_lock_path)
    if not _existing_for_cmp or not _persist_lock.is_semantically_equivalent(_existing_for_cmp):
        _persist_lock.save(_lock_path)


def _dependency_claimed_paths(ctx: InstallContext) -> frozenset[str]:
    """Return the paths a dependency of this user-scope install claims (#3772).

    At user scope the root package is ``$HOME``, which also holds dependency
    sources; a file a dependency claims stays that dependency's, so
    uninstalling it removes the file. Project scope keeps its root claims.
    """
    if not is_user_scope(getattr(ctx, "scope", None)):
        return frozenset()
    from apm_cli.core.deployment_state import DeploymentReconciler

    return DeploymentReconciler.current_claimed_paths(ctx.package_deployed_files)
