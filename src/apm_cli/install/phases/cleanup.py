"""Cleanup orchestrator phase -- orphan and stale-file removal.

Routes **all** file-system deletions through the canonical security chokepoint
``apm_cli.integration.cleanup.remove_stale_deployed_files`` (PR #762) which
enforces three safety gates: ``validate_deploy_path``, directory rejection,
and fail-closed content-hash provenance.

Two distinct cleanup passes run in sequence:

**Block A -- Orphan cleanup**
    For every dependency in the *previous* lockfile whose key is NOT in
    ``ctx.intended_dep_keys``, all deployed files are removed.  ``targets=None``
    is passed deliberately so the helper validates against *all*
    ``KNOWN_TARGETS``, not just the active install's target set.

**Block B -- Intra-package stale-file cleanup**
    For every dependency still in the manifest, files present in the old
    lockfile but absent from the fresh integration output are removed.
    Failed deletions are re-inserted into ``ctx.package_deployed_files`` so
    the downstream lockfile phase records the retained paths.

This module is a faithful extraction from ``commands/install.py`` --
no behavioural changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apm_cli.drift import detect_stale_files
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.cleanup import CleanupOpts, remove_stale_deployed_files

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _run_orphan_cleanup(ctx: InstallContext) -> None:
    # Orphan cleanup: remove deployed files for packages that were
    # present in the previous lockfile but are no longer in apm.yml.
    existing_lockfile = ctx.existing_lockfile
    if not existing_lockfile or ctx.only_packages:
        return

    from apm_cli.deps.lockfile import _SELF_KEY

    orphan_total_deleted = 0
    orphan_deleted_targets: list = []
    for orphan_key, orphan_dep in existing_lockfile.dependencies.items():
        if orphan_key == _SELF_KEY or orphan_key in ctx.intended_dep_keys:
            continue
        if not orphan_dep.deployed_files:
            continue
        orphan_result = remove_stale_deployed_files(
            orphan_dep.deployed_files,
            ctx.project_root,
            opts=CleanupOpts(
                dep_key=orphan_key,
                targets=None,
                diagnostics=ctx.diagnostics,
                recorded_hashes=dict(orphan_dep.deployed_file_hashes),
                failed_path_retained=False,
            ),
        )
        orphan_total_deleted += len(orphan_result.deleted)
        orphan_deleted_targets.extend(orphan_result.deleted_targets)
        if ctx.logger:
            for skipped in orphan_result.skipped_user_edit:
                ctx.logger.cleanup_skipped_user_edit(skipped, orphan_key)
    if orphan_deleted_targets:
        BaseIntegrator.cleanup_empty_parents(orphan_deleted_targets, ctx.project_root)
    if ctx.logger:
        ctx.logger.orphan_cleanup(orphan_total_deleted)


def _run_stale_cleanup(ctx: InstallContext) -> None:
    # Stale-file cleanup: remove files present in old lockfile but absent
    # from the new integration output for each still-declared dependency.
    existing_lockfile = ctx.existing_lockfile
    if not existing_lockfile or not ctx.package_deployed_files:
        return

    for dep_key, new_deployed in ctx.package_deployed_files.items():
        if ctx.diagnostics.count_for_package(dep_key, "error") > 0:
            continue

        prev_dep = existing_lockfile.get_dependency(dep_key)
        if not prev_dep:
            continue
        stale = detect_stale_files(prev_dep.deployed_files, new_deployed)
        if not stale:
            continue

        cleanup_result = remove_stale_deployed_files(
            stale,
            ctx.project_root,
            opts=CleanupOpts(
                dep_key=dep_key,
                targets=ctx.targets or None,
                diagnostics=ctx.diagnostics,
                recorded_hashes=dict(prev_dep.deployed_file_hashes),
            ),
        )
        new_deployed.extend(cleanup_result.failed)
        if cleanup_result.deleted_targets:
            BaseIntegrator.cleanup_empty_parents(cleanup_result.deleted_targets, ctx.project_root)
        if ctx.logger:
            for skipped in cleanup_result.skipped_user_edit:
                ctx.logger.cleanup_skipped_user_edit(skipped, dep_key)
            ctx.logger.stale_cleanup(dep_key, len(cleanup_result.deleted))


def run(ctx: InstallContext) -> None:
    """Execute orphan cleanup and intra-package stale-file cleanup.

    Reads ``ctx.existing_lockfile``, ``ctx.intended_dep_keys``,
    ``ctx.package_deployed_files`` (mutated), ``ctx.diagnostics``,
    ``ctx.targets``, ``ctx.logger``, ``ctx.project_root``,
    ``ctx.only_packages``.
    """
    _run_orphan_cleanup(ctx)
    _run_stale_cleanup(ctx)
