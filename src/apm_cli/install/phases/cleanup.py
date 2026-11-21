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
    target set. ``--frozen`` never rewrites the lockfile, so it retains every
    orphan instead (files, checkout and claims) until a normal install.

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
from apm_cli.core.remediation import ORPHANED_PACKAGES_PRUNE, kept_note
from apm_cli.core.scope import is_user_scope
from apm_cli.drift import detect_stale_files
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.cleanup import DeploymentEvidence, remove_stale_deployed_files

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.install.service import FrozenDisposition


def deployment_evidence(ctx: InstallContext) -> DeploymentEvidence:
    """Return this install's deletion evidence, built once from the prior lock (#3701).

    Receipts come from the installation-bound session; the render fallback
    replays ``ctx.existing_lockfile`` (the versions whose files may go), so
    every install deletion path shares one evidence owner.
    """
    evidence = getattr(ctx, "deployment_evidence", None)
    kept = getattr(ctx, "kept_deployed_values", frozenset())
    if isinstance(evidence, DeploymentEvidence):
        # Evidence may be built before integration finishes; the skip set is the run's latest.
        evidence.retain(kept)
    else:
        evidence = DeploymentEvidence.collect(
            ctx.project_root,
            user_scope=is_user_scope(getattr(ctx, "scope", None)),
            lockfile=ctx.existing_lockfile,
            lockfile_root=ctx.apm_dir,
            diagnostics=ctx.diagnostics,
            kept=kept,
        )
        ctx.deployment_evidence = evidence
    return evidence


def kept_prior_values(ctx: InstallContext, prior_files: list[str]) -> list[str]:
    """Return prior lock values this run skipped, kept or refused (#4230).

    The integrate phase's skip set reaches cleanup through one channel
    (``ctx.kept_deployed_values`` -> ``DeploymentEvidence.kept``). Such a
    value is never stale: it stays on disk and keeps its lock entry.
    """
    evidence = deployment_evidence(ctx)
    return [path for path in prior_files if evidence.kept(path)]


def run(ctx: InstallContext) -> None:
    """Execute orphan cleanup and intra-package stale-file cleanup.

    Reads ``ctx.existing_lockfile``, ``ctx.intended_dep_keys``,
    ``ctx.package_deployed_files`` (mutated), ``ctx.diagnostics``,
    ``ctx.targets``, ``ctx.logger``, ``ctx.project_root``,
    ``ctx.only_packages``.
    """
    from apm_cli.install.service import InstallService

    frozen_disposition = InstallService.frozen_disposition(ctx)
    receipt_retirements: list[Path] = []
    try:
        _run_cleanup(ctx, receipt_retirements, frozen_disposition=frozen_disposition)
    finally:
        DeploymentMaterializationSession.retire_removed(
            ctx.project_root,
            receipt_retirements,
            user_scope=is_user_scope(getattr(ctx, "scope", None)),
        )


def _run_cleanup(
    ctx: InstallContext,
    receipt_retirements: list[Path],
    *,
    frozen_disposition: FrozenDisposition,
) -> None:
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
    # Owners this install keeps: the manifest intent, or for a partial install
    # every previous entry except the dropped descendants. The survivor
    # projection and the orphan owner both read this one intent.
    desired_owners: frozenset[str] = frozenset(intended_dep_keys or ())
    if existing_lockfile and only_packages:
        desired_owners = frozenset(
            set(existing_lockfile.dependencies)
            - partial_dropped  # architecture-authority-exempt: survivor intent input, not a removal predicate
        )
    if existing_lockfile:
        from apm_cli.install.manifest_reconcile import surviving_deployed_paths
        from apm_cli.install.phases.targets import declared_target_profiles

        surviving_files = surviving_deployed_paths(
            existing_lockfile,
            package_deployed_files,
            _targets or [],
            declared_target_profiles(ctx),
            desired_owners=desired_owners,
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
        # The orphan owner decides from manifest intent (intended_dep_keys) --
        # NOT package_deployed_files.keys() (integration outcome). A transient
        # integration failure for a still-declared package would leave its key
        # absent from package_deployed_files; deriving orphans from the outcome
        # set would then misclassify it as removed and delete its previously
        # deployed files even though it is still in apm.yml. A partial install
        # passes only the selected packages' dropped descendants as candidates.
        decision = _orphan_decision(ctx, desired_owners, surviving_files, orphan_cleanup_retained)
        _orphan_results: list = []
        _orphan_deleted_targets: list = []
        for _orphan_key in sorted(decision.candidate_keys):
            _orphan_dep = existing_lockfile.dependencies[_orphan_key]
            if frozen_disposition.retain_package_orphans:
                # Frozen never rewrites the lock, so an undeclared package keeps
                # its files, checkout and claims until a normal install (#3662).
                orphan_cleanup_retained[_orphan_key] = {
                    path: _orphan_dep.deployed_file_hashes.get(path)
                    for path in _orphan_dep.deployed_files
                }
                continue
            orphan_only_files = list(decision.removed_files[_orphan_key])
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
                evidence=deployment_evidence(ctx),
                recorded_hashes=dict(_orphan_dep.deployed_file_hashes),
                failed_path_retained=False,
                user_scope=user_scope,
                receipt_retirements=receipt_retirements,
            )
            _orphan_results.append(_orphan_result)
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
            logger.orphan_cleanup(_orphan_results)
            if frozen_disposition.retain_package_orphans and orphan_cleanup_retained:
                logger.frozen_orphans_kept(sorted(orphan_cleanup_retained))
        # Re-decide with the files cleanup could not delete: their packages
        # (and, for a partial install, their dropped ancestors) stay locked.
        settled = _orphan_decision(ctx, desired_owners, surviving_files, orphan_cleanup_retained)
        if partial_dropped:
            ctx.partial_pinned_dep_keys = settled.retained_keys
            _remove_dropped_package_dirs(ctx, settled)
        _trim_superseded_package_roots(ctx, settled)

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
            kept = kept_prior_values(ctx, prev_dep.deployed_files)
            if kept:
                stale -= set(kept)
                new_deployed.extend(path for path in kept if path not in new_deployed)
                package_cleanup_retained[dep_key] = {
                    **package_cleanup_retained.get(dep_key, {}),
                    **{path: prev_dep.deployed_file_hashes.get(path) for path in kept},
                }
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
                        kept_note(
                            f"stale file {protected_path} for {dep_key}",
                            "still in use by another package or target",
                        )
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
                evidence=deployment_evidence(ctx),
                recorded_hashes=dict(prev_dep.deployed_file_hashes),
                user_scope=user_scope,
                receipt_retirements=receipt_retirements,
                live_paths=surviving_files | set(new_deployed),
            )
            # Re-insert every non-deletion so the lockfile retains the
            # prior ownership claim for retry or user review.
            new_deployed.extend(
                path for path in cleanup_result.retained if path not in new_deployed
            )
            if cleanup_result.retained:
                # Merge: integrate may already keep this package's unproven files (#4360).
                package_cleanup_retained[dep_key] = package_cleanup_retained.get(dep_key, {}) | {
                    path: prev_dep.deployed_file_hashes.get(path)
                    for path in cleanup_result.retained
                }
            if cleanup_result.deleted_targets:
                BaseIntegrator.cleanup_empty_parents(cleanup_result.deleted_targets, project_root)
            for _skipped in cleanup_result.skipped_user_edit:
                if logger:
                    logger.cleanup_skipped_user_edit(_skipped, dep_key)
            if logger:
                for _aliased in cleanup_result.aliased:
                    logger.verbose_detail(
                        f"Renamed {_aliased} to the new letter case from {dep_key}"
                    )
                logger.stale_cleanup(dep_key, cleanup_result)


def _orphan_decision(
    ctx: InstallContext,
    desired_owners: frozenset[str],
    surviving_files: frozenset[str],
    retained: dict[str, dict[str, str | None]],
):
    """Ask the orphan owner for this install's package-removal decision."""
    from apm_cli.install.orphans import (
        DeploymentClaims,
        InstalledPaths,
        OrphanOperation,
        decide_orphan_removal,
    )

    graph = ctx.dependency_graph
    materialized = [*ctx.deps_to_install]
    if graph is not None:
        # Resolved graph references, not raw declarations: unresolved
        # marketplace entries have no install path of their own.
        materialized.extend(node.dependency_ref for node in graph.dependency_tree.nodes.values())
    apm_modules_dir = ctx.apm_modules_dir
    lockfile = ctx.existing_lockfile
    partial = bool(ctx.only_packages)
    return decide_orphan_removal(
        operation=OrphanOperation.TARGETED_INSTALL if partial else OrphanOperation.INSTALL,
        roots=(),
        candidate_universe=lockfile,
        installed_paths=(
            InstalledPaths(root=apm_modules_dir, packages=None)
            if isinstance(apm_modules_dir, Path)
            else None
        ),
        resolved_keys=None,
        # A partial install settles the resolve phase's decision: every
        # previous entry except the selected packages' dropped descendants.
        intended=desired_owners,
        materialized=materialized,
        reachability=None,
        deployment_claims=DeploymentClaims(live_files=surviving_files, retained=retained),
    )


def _remove_dropped_package_dirs(ctx: InstallContext, decision) -> None:
    """Remove dropped descendants' ``apm_modules`` copies the orphan owner released.

    The owner keeps a path still used by a resolved, retained, pinned or newly
    selected package; pinned entries keep their copy alongside their lock
    entry for the next install.
    """
    from apm_cli.utils.path_security import PathTraversalError, safe_rmtree

    apm_modules_dir = ctx.apm_modules_dir
    deleted: list[Path] = []
    for relative in decision.removed_paths:
        package_dir = apm_modules_dir / relative
        if not package_dir.is_dir():
            continue
        try:
            safe_rmtree(package_dir, apm_modules_dir)
        except (OSError, PathTraversalError) as exc:
            ctx.diagnostics.warn(
                f"Could not remove dropped dependency copy apm_modules/{relative}: {exc}",
                detail=ORPHANED_PACKAGES_PRUNE.sentence(),
            )
            continue
        deleted.append(package_dir)
    if deleted:
        BaseIntegrator.cleanup_empty_parents(deleted, stop_at=apm_modules_dir)


def _trim_superseded_package_roots(ctx: InstallContext, decision) -> None:
    """Reduce a released package copy to the retained packages nested inside it.

    Switching ``owner/repo`` to ``path:`` dependencies on the same repository
    leaves the old whole-repository checkout as the parent of the new
    subdirectory installs (#2926). The orphan owner names each released root
    that still hosts retained subtrees and selects its unneeded entries --
    the state a fresh subdirectory install produces.
    """
    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within, safe_rmtree

    apm_modules_dir = getattr(ctx, "apm_modules_dir", None)
    for root, hosted in decision.superseded_roots:
        root_dir = apm_modules_dir / root
        if not root_dir.is_dir() or root_dir.is_symlink():
            continue
        entries = _entries_leading_to(root_dir, apm_modules_dir, set(hosted))
        for orphan in decision.superseded_entries(entries):
            for entry in entries[orphan]:
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        safe_rmtree(entry, apm_modules_dir)
                    else:
                        ensure_path_within(entry.parent, apm_modules_dir)
                        entry.unlink()
                except (OSError, PathTraversalError) as exc:
                    display = entry.relative_to(apm_modules_dir).as_posix()
                    ctx.diagnostics.warn(
                        f"Could not remove superseded content apm_modules/{display}: {exc}",
                        detail=ORPHANED_PACKAGES_PRUNE.sentence(),
                    )


def _entries_leading_to(
    directory: Path, apm_modules_dir: Path, hosted: set[str]
) -> dict[str, list[Path]]:
    """Group entries by case-folded relative path, descending only toward *hosted*.

    A case-sensitive filesystem can hold several entries per folded key
    (``README.md`` and ``readme.md``); each group keeps all of them.
    """
    entries: dict[str, list[Path]] = {}
    for child in sorted(directory.iterdir()):
        relative = child.relative_to(apm_modules_dir).as_posix().casefold()
        entries.setdefault(relative, []).append(child)
        if (
            child.is_dir()
            and not child.is_symlink()
            and relative not in hosted
            and any(path.startswith(f"{relative}/") for path in hosted)
        ):
            for key, group in _entries_leading_to(child, apm_modules_dir, hosted).items():
                entries.setdefault(key, []).extend(group)
    return entries
