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
    existing_lockfile = ctx.existing_lockfile
    only_packages = ctx.only_packages
    intended_dep_keys = ctx.intended_dep_keys
    project_root = ctx.project_root
    _targets = ctx.targets
    diagnostics = ctx.diagnostics
    logger = ctx.logger
    package_deployed_files = ctx.package_deployed_files

    # ------------------------------------------------------------------
    # Orphan cleanup: remove deployed files for packages that were
    # removed from the manifest. This happens on every full install
    # (no only_packages), making apm install idempotent with the manifest.
    # Routed through remove_stale_deployed_files() so the same safety
    # gates -- including per-file content-hash provenance -- apply
    # uniformly with the intra-package stale path below.
    # ------------------------------------------------------------------
    if existing_lockfile and not only_packages:
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
            if not _orphan_dep.deployed_files:
                continue
            _orphan_result = remove_stale_deployed_files(
                _orphan_dep.deployed_files,
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
            )
            _orphan_total_deleted += len(_orphan_result.deleted)
            _orphan_deleted_targets.extend(_orphan_result.deleted_targets)
            for _skipped in _orphan_result.skipped_user_edit:
                if logger:
                    logger.cleanup_skipped_user_edit(_skipped, _orphan_key)
        if _orphan_deleted_targets:
            BaseIntegrator.cleanup_empty_parents(_orphan_deleted_targets, project_root)
        if logger:
            logger.orphan_cleanup(_orphan_total_deleted)

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
            stale = detect_stale_files(prev_dep.deployed_files, new_deployed)
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
            )
            # Re-insert failed paths so the lockfile retains them for
            # retry on the next install.
            new_deployed.extend(cleanup_result.failed)
            if cleanup_result.deleted_targets:
                BaseIntegrator.cleanup_empty_parents(cleanup_result.deleted_targets, project_root)
            for _skipped in cleanup_result.skipped_user_edit:
                if logger:
                    logger.cleanup_skipped_user_edit(_skipped, dep_key)
            if logger:
                logger.stale_cleanup(dep_key, len(cleanup_result.deleted))
