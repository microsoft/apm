"""Cleanup orchestrator phase -- orphan and stale-file removal.

Routes **all** file-system deletions through the canonical security chokepoint
``apm_cli.integration.cleanup.remove_stale_deployed_files`` (PR #762) which
enforces three safety gates: ``validate_deploy_path``, directory rejection,
and fail-closed content-hash provenance.

Two distinct cleanup passes run in sequence:

**Block A -- Orphan cleanup**
    For every dependency in the *previous* lockfile whose key is NOT in
    ``ctx.intended_dep_keys``, deployed files not freshly claimed by an active
    package are removed. ``targets=None`` is passed deliberately so the helper
    validates against *all* ``KNOWN_TARGETS``, not just the active install's
    target set.

**Block B -- Intra-package stale-file cleanup**
    For every dependency still in the manifest, files present in the old
    lockfile but absent from the fresh integration output are removed.
    Failed deletions are re-inserted into ``ctx.package_deployed_files`` so
    the downstream lockfile phase records the retained paths.

Originally extracted from ``commands/install.py``; later extended with
cross-package file protection for stale cleanup (#1831).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.deployment_ledger import DeploymentMaterializationSession
from apm_cli.core.scope import is_user_scope
from apm_cli.drift import detect_stale_files
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.cleanup import remove_stale_deployed_files

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def run(ctx: InstallContext) -> None:
    """Execute orphan cleanup and intra-package stale-file cleanup.

    Reads ``ctx.existing_lockfile``, ``ctx.intended_dep_keys``,
    ``ctx.package_deployed_files`` (mutated), ``ctx.diagnostics``,
    ``ctx.targets``, ``ctx.logger``, ``ctx.project_root``,
    ``ctx.only_packages``.
    """
    receipt_retirements: list[Path] = []
    try:
        _run_cleanup(ctx, receipt_retirements)
    finally:
        DeploymentMaterializationSession.retire_removed(
            ctx.project_root,
            receipt_retirements,
            user_scope=is_user_scope(getattr(ctx, "scope", None)),
        )


def _run_cleanup(ctx: InstallContext, receipt_retirements: list[Path]) -> None:
    """Collect canonical cleanup outcomes without per-package receipt publication."""
    existing_lockfile = ctx.existing_lockfile
    only_packages = ctx.only_packages
    intended_dep_keys = ctx.intended_dep_keys
    project_root = ctx.project_root
    _targets = ctx.targets
    diagnostics = ctx.diagnostics
    logger = ctx.logger
    user_scope = is_user_scope(getattr(ctx, "scope", None))
    package_deployed_files = ctx.package_deployed_files
    orphan_cleanup_retained = getattr(ctx, "orphan_cleanup_retained", None)
    if orphan_cleanup_retained is None:
        orphan_cleanup_retained = {}
        ctx.orphan_cleanup_retained = orphan_cleanup_retained
    package_cleanup_retained = getattr(ctx, "package_cleanup_retained", None)
    if package_cleanup_retained is None:
        package_cleanup_retained = {}
        ctx.package_cleanup_retained = package_cleanup_retained
    surviving_files: frozenset[str] = frozenset()
    # Partial installs retire only the selected packages' dropped descendants
    # (#3502); every other previous entry stays owned and untouched.
    partial_dropped = (
        frozenset(getattr(ctx, "partial_dropped_dep_keys", ())) if only_packages else frozenset()
    )
    if existing_lockfile:
        from apm_cli.install.manifest_reconcile import surviving_deployed_paths
        from apm_cli.install.phases.targets import declared_target_profiles

        surviving_files = surviving_deployed_paths(
            existing_lockfile,
            package_deployed_files,
            _targets or [],
            declared_target_profiles(ctx),
            desired_owners=frozenset(
                set(existing_lockfile.dependencies) - partial_dropped
                if only_packages
                else intended_dep_keys
            ),
            failed_owners=frozenset(
                key
                for key in existing_lockfile.dependencies
                if diagnostics.count_for_package(key, "error") > 0
            ),
            user_scope=user_scope,
        )

    # ------------------------------------------------------------------
    # Orphan cleanup: remove deployed files for packages that were
    # removed from the manifest. This happens on every full install
    # (no only_packages), making apm install idempotent with the manifest.
    # A partial install treats only the selected packages' dropped
    # descendants as orphans.
    # Routed through remove_stale_deployed_files() so the same safety
    # gates -- including per-file content-hash provenance -- apply
    # uniformly with the intra-package stale path below.
    # ------------------------------------------------------------------
    if existing_lockfile and (not only_packages or partial_dropped):
        # Use intended_dep_keys (manifest intent, computed at ~line 1707) --
        # NOT package_deployed_files.keys() (integration outcome). A transient
        # integration failure for a still-declared package would leave its key
        # absent from package_deployed_files; deriving orphans from the outcome
        # set would then misclassify it as removed and delete its previously
        # deployed files even though it is still in apm.yml.
        from apm_cli.deps.lockfile import _SELF_KEY

        _orphan_total_deleted = 0
        _orphan_deleted_targets: list = []
        for _orphan_key, _orphan_dep in existing_lockfile.dependencies.items():
            # Issue #887: skip the synthesized self-entry (local .apm/
            # content). Local content stale cleanup happens in the
            # post_deps_local phase, not here. Treating self-entry as
            # an orphan would delete the project's own .github/ files.
            if _orphan_key == _SELF_KEY:
                continue
            if _orphan_key in intended_dep_keys:
                continue  # still in manifest -- handled by stale-cleanup below
            if only_packages and _orphan_key not in partial_dropped:
                continue  # unrelated to the selected packages' subtree
            orphan_only_files = [
                deployed_file
                for deployed_file in _orphan_dep.deployed_files
                if deployed_file not in surviving_files
            ]
            if not orphan_only_files:
                continue
            _orphan_result = remove_stale_deployed_files(
                orphan_only_files,
                project_root,
                dep_key=_orphan_key,
                # targets=None -> validate against all KNOWN_TARGETS, not
                # just the active install's targets. An orphan can have
                # files under a target the user is not currently running
                # (e.g. switched runtime since the dep was installed,
                # or scope mismatch). Restricting to _targets here would
                # leave those files behind. Pre-PR code handled this by
                # explicitly merging KNOWN_TARGETS; targets=None is the
                # cleaner equivalent.
                targets=None,
                diagnostics=diagnostics,
                recorded_hashes=dict(_orphan_dep.deployed_file_hashes),
                failed_path_retained=False,
                user_scope=user_scope,
                receipt_retirements=receipt_retirements,
            )
            _orphan_total_deleted += len(_orphan_result.deleted)
            _orphan_deleted_targets.extend(_orphan_result.deleted_targets)
            if _orphan_result.retained:
                orphan_cleanup_retained[_orphan_key] = {
                    path: _orphan_dep.deployed_file_hashes.get(path)
                    for path in _orphan_result.retained
                }
            for _skipped in _orphan_result.skipped_user_edit:
                if logger:
                    logger.cleanup_skipped_user_edit(_skipped, _orphan_key)
        if _orphan_deleted_targets:
            BaseIntegrator.cleanup_empty_parents(_orphan_deleted_targets, project_root)
        if logger:
            logger.orphan_cleanup(_orphan_total_deleted)
        if partial_dropped:
            pinned = _pin_retained_ancestors(
                existing_lockfile, partial_dropped, frozenset(orphan_cleanup_retained)
            )
            ctx.partial_pinned_dep_keys = pinned
            _remove_dropped_package_dirs(ctx, partial_dropped, pinned)

    # ------------------------------------------------------------------
    # Stale-file cleanup: within each package still present in the
    # manifest, remove files that were in the previous lockfile's
    # deployed_files but are not in the fresh integration output.
    # Handles renames and intra-package file removals (issue #666).
    # Complements the package-level orphan cleanup above, which handles
    # packages that left the manifest entirely.
    # ------------------------------------------------------------------
    if existing_lockfile and package_deployed_files:
        for dep_key, new_deployed in package_deployed_files.items():
            # Skip packages whose integration reported errors this run --
            # a file that failed to re-deploy would look stale and get
            # wrongly deleted.
            if diagnostics.count_for_package(dep_key, "error") > 0:
                continue

            prev_dep = existing_lockfile.get_dependency(dep_key)
            if not prev_dep:
                continue  # new package this install -- nothing stale yet
            stale = set(detect_stale_files(prev_dep.deployed_files, new_deployed))
            if not stale:
                continue

            # Cross-package file protection (#1831): do not remove a file
            # that another currently-installed package still deploys.
            # package_deployed_files is integration outcome, not a security
            # boundary; content-hash provenance remains the deletion gate.
            other_deployed = surviving_files - set(new_deployed)
            protected = stale & other_deployed
            stale = stale - other_deployed
            if protected and logger:
                for protected_path in sorted(protected):
                    logger.verbose_detail(
                        f"Kept stale file {protected_path} for {dep_key}; "
                        "still in use by another package or target"
                    )

            if not stale:
                continue

            cleanup_result = remove_stale_deployed_files(
                stale,
                project_root,
                dep_key=dep_key,
                # `_targets or None` mirrors the pre-refactor behavior: when
                # no targets were resolved (e.g. unknown runtime), pass None
                # so the cleanup helper falls back to scanning all
                # KNOWN_TARGETS rather than skipping cleanup entirely.
                # The chokepoint at integration/cleanup.py applies its own
                # path-safety gates regardless of which target set is used.
                targets=_targets or None,
                diagnostics=diagnostics,
                recorded_hashes=dict(prev_dep.deployed_file_hashes),
                require_recorded_hash=dep_key in ctx.inline_hook_withheld_packages,
                user_scope=user_scope,
                receipt_retirements=receipt_retirements,
            )
            # Re-insert every non-deletion so the lockfile retains the
            # prior ownership claim for retry or user review.
            new_deployed.extend(
                path for path in cleanup_result.retained if path not in new_deployed
            )
            if cleanup_result.retained:
                package_cleanup_retained[dep_key] = {
                    path: prev_dep.deployed_file_hashes.get(path)
                    for path in cleanup_result.retained
                }
                if dep_key in ctx.inline_hook_withheld_packages:
                    diagnostics.error(
                        "Inline hook revocation is incomplete: conflicting deployed content "
                        "was preserved. Resolve the reported files, reinstall, and restart "
                        "Claude; running-session hooks cannot be revoked by deleting files.",
                        package=dep_key,
                    )
            if cleanup_result.deleted_targets:
                BaseIntegrator.cleanup_empty_parents(cleanup_result.deleted_targets, project_root)
            for _skipped in cleanup_result.skipped_user_edit:
                if logger:
                    logger.cleanup_skipped_user_edit(_skipped, dep_key)
            if logger:
                logger.stale_cleanup(dep_key, len(cleanup_result.deleted))


def _pin_retained_ancestors(
    lockfile, dropped: frozenset[str], retained: frozenset[str]
) -> frozenset[str]:
    """Keep each retained dropped entry plus its dropped ancestors for a later retry.

    A descendant whose deployed files could not be removed stays locked under
    its recorded parent; keeping that parent chain lets the next targeted
    install of the selected package rediscover and retry it.
    """
    from apm_cli.deps.lockfile import lockfile_parent_identity

    by_identity = {
        lockfile_parent_identity(lockfile.dependencies[key], lockfile.replacements): key
        for key in dropped
        if key in lockfile.dependencies
    }
    pinned: set[str] = set()
    queue = [key for key in dropped if key in retained]
    while queue:
        key = queue.pop()
        if key in pinned or key not in lockfile.dependencies:
            continue
        pinned.add(key)
        queue.extend(
            by_identity[parent]
            for parent in lockfile.dependencies[key].parent_keys
            if parent in by_identity
        )
    return frozenset(pinned)


def _remove_dropped_package_dirs(
    ctx: InstallContext,
    dropped: frozenset[str],
    pinned: frozenset[str],
) -> None:
    """Remove dropped descendants' ``apm_modules`` copies that nothing retained shares.

    Selection routes through the declaration-aware orphan owner so a path
    still used by a resolved, retained, pinned or newly selected package
    survives. Pinned entries keep their copy alongside their lock entry for
    the next install.
    """
    from apm_cli.commands._helpers import _build_expected_install_paths, _find_orphaned_packages
    from apm_cli.deps.lockfile import _SELF_KEY
    from apm_cli.utils.path_security import PathTraversalError, safe_rmtree

    apm_modules_dir = ctx.apm_modules_dir
    existing_lockfile = ctx.existing_lockfile
    if apm_modules_dir is None or existing_lockfile is None or not apm_modules_dir.is_dir():
        return
    removable = [
        dependency
        for key, dependency in sorted(existing_lockfile.dependencies.items())
        if key in dropped and key not in pinned
    ]
    graph = getattr(ctx, "dependency_graph", None)
    try:
        # Resolved graph references, not raw declarations: unresolved
        # marketplace entries have no install path of their own.
        retained_refs = [*ctx.deps_to_install]
        if graph is not None:
            retained_refs.extend(
                node.dependency_ref for node in graph.dependency_tree.nodes.values()
            )
        retained_refs.extend(
            dependency.to_dependency_ref()
            for key, dependency in existing_lockfile.dependencies.items()
            if key != _SELF_KEY and (key not in dropped or key in pinned)
        )
        expected = _build_expected_install_paths(retained_refs, None, apm_modules_dir)
        candidates = _build_expected_install_paths(
            [dependency.to_dependency_ref() for dependency in removable], None, apm_modules_dir
        )
    except (ValueError, PathTraversalError):
        # Without every retained install path, no removal can be proven safe.
        return
    deleted: list[Path] = []
    for relative in _find_orphaned_packages(
        (path for path in candidates if not Path(path).is_absolute()), expected
    ):
        package_dir = apm_modules_dir / relative
        if not package_dir.is_dir():
            continue
        try:
            safe_rmtree(package_dir, apm_modules_dir)
        except (OSError, PathTraversalError) as exc:
            ctx.diagnostics.warn(
                f"Could not remove dropped dependency copy apm_modules/{relative}: {exc}",
                detail="Run 'apm prune' to retry.",
            )
            continue
        deleted.append(package_dir)
    if deleted:
        BaseIntegrator.cleanup_empty_parents(deleted, stop_at=apm_modules_dir)
