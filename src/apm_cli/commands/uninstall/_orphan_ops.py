"""Orphan/reachability helpers extracted from engine.py (800-line guardrail)."""

from __future__ import annotations

import builtins
from pathlib import Path


def _read_survivor_direct_refs(apm_yml_path, packages_to_remove):
    """Parse apm.yml's direct APM dependencies, excluding *packages_to_remove*.

    Seeds the forward reachability walk (see
    :func:`apm_cli.deps.reachability.compute_forward_reachable_keys`) with
    the project's SURVIVING direct dependencies. For the real uninstall
    path *apm_yml_path* is already post-edit (Step 3 in ``cli.py`` runs
    before ``_cleanup_transitive_orphans``), so the identity subtraction
    below is a no-op there; for the ``--dry-run`` preview (called BEFORE
    apm.yml is rewritten) it is what turns the pre-edit dependency list
    into the correct post-removal survivor set, mirroring the identity
    matching ``_validate_uninstall_packages`` already uses.

    Returns an empty list -- never raises -- if *apm_yml_path* is ``None``,
    missing, or malformed; the project's own manifest is a Step-1/3
    concern here, not part of this walk's fail-closed contract, which
    covers the manifests visited DURING the walk, not this seed step.
    """
    if apm_yml_path is None:
        return []

    from .engine import _parse_dependency_entry

    removed_identities = builtins.set()
    for pkg in packages_to_remove:
        try:
            removed_identities.add(_parse_dependency_entry(pkg).get_identity())
        except (ValueError, TypeError, AttributeError, KeyError):
            continue

    import yaml

    from ...utils.yaml_io import load_yaml

    try:
        data = load_yaml(apm_yml_path) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return []

    refs = []
    for dep_entry in data.get("dependencies", {}).get("apm", []) or []:
        try:
            ref = _parse_dependency_entry(dep_entry)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        if ref.get_identity() in removed_identities:
            continue
        refs.append(ref)
    return refs


def _warn_reachability_incomplete(reachability, logger):
    """Emit exactly one bounded warning when reachability could not be verified.

    Fires at most once per ``_compute_actual_orphans`` call regardless of
    how many manifests were unverifiable during the walk -- the per-node
    detail is confined to ``--verbose`` so the always-shown message stays
    a single, bounded line.
    """
    logger.warning(
        f"Skipped transitive dependency cleanup -- {len(reachability.unverifiable)} "
        "package manifest(s) could not be verified. Keeping all transitive "
        "dependencies as a precaution."
    )
    if not logger.verbose:
        logger.info("Run with --verbose to see which manifest(s) failed.")
    logger.info("Fix or restore the affected manifest(s), then re-run to complete cleanup.")
    for pkg_id, reason in reachability.unverifiable:
        logger.verbose_detail(f"    {pkg_id}: {reason}")


def _compute_actual_orphans(
    lockfile,
    orphans,
    removed_repo_urls,
    apm_yml_path,
    packages_to_remove,
    apm_modules_dir,
    logger,
):
    """Decide which candidate orphans are truly unreachable and safe to delete.

    The single canonical decision helper shared by
    ``_cleanup_transitive_orphans`` (real removal) and
    ``_dry_run_uninstall`` (its ``--dry-run`` preview), so a preview and
    the real run can never disagree about what would/will be kept.

    Subtracts the project's surviving direct dependencies and every
    still-declared lockfile entry from *orphans* (as before this fix),
    then asks :mod:`apm_cli.deps.reachability` -- the canonical owner --
    whether a remaining candidate is nonetheless still forward-reachable
    from a SURVIVING package's own real, on-disk manifest (the
    shared/diamond-dependency fix). If that walk could not be fully
    verified, this fails closed: it preserves EVERY candidate for this
    run rather than partially trusting an incomplete result.

    Returns:
        A ``(actual_orphans, repairs)`` tuple. *actual_orphans* is the
        frozenset of keys safe to delete this run, unchanged in meaning
        from before this docstring update. *repairs* maps each RESCUED
        candidate's key to the ``(parent_repo_url, local_path)`` of the
        surviving node that rescued it (see
        ``ReachabilityResult.reachable_via``) -- a rescued candidate's
        ``resolved_by``/``local_path`` are otherwise left pointing at a
        parent that may no longer exist in the lockfile, which would
        silently defeat the NEXT uninstall's backward orphan-candidate
        scan (``_build_children_index`` is keyed on ``resolved_by``) and
        permanently fail-close any later reachability walk that needs to
        re-resolve this same entry's on-disk directory. Only
        ``_cleanup_transitive_orphans`` (the real removal path) may act
        on *repairs* by writing them into the lockfile; ``--dry-run``
        must discard them -- its preview must stay read-only.
    """
    if not orphans:
        return builtins.frozenset(), {}

    direct_refs = _read_survivor_direct_refs(apm_yml_path, packages_to_remove)

    remaining_deps = builtins.set()
    for ref in direct_refs:
        remaining_deps.add(ref.get_unique_key())
    for dep in lockfile.get_package_dependencies():
        key = dep.get_unique_key()
        if key not in orphans and dep.repo_url not in removed_repo_urls:
            remaining_deps.add(key)

    candidate_orphans = builtins.frozenset(orphans) - remaining_deps
    if not candidate_orphans:
        return candidate_orphans, {}

    from ...deps.reachability import compute_forward_reachable_keys

    project_root = apm_yml_path.parent if apm_yml_path else Path(".")
    reachability = compute_forward_reachable_keys(
        lockfile, project_root, apm_modules_dir, direct_refs, candidate_orphans
    )
    if not reachability.complete:
        _warn_reachability_incomplete(reachability, logger)
        return builtins.frozenset(), {}

    rescued = candidate_orphans & reachability.reachable
    repairs = {
        key: reachability.reachable_via[key] for key in rescued if key in reachability.reachable_via
    }
    return candidate_orphans - reachability.reachable, repairs


def _dry_run_uninstall(packages_to_remove, apm_modules_dir, logger, apm_yml_path=None):
    """Show what would be removed without making changes.

    *apm_yml_path* is the project's manifest, read here BEFORE it is
    rewritten (unlike the real uninstall path, this preview runs before
    ``cli.py``'s Step 3 edits apm.yml). It defaults to ``None`` for
    direct-unit-test callers that don't set up a diamond-dependency
    scenario; production call sites always pass the real path so the
    reachability rescue (see ``_compute_actual_orphans``) is exercised
    for every real ``--dry-run`` invocation.
    """
    from .engine import _build_children_index, _parse_dependency_entry

    logger.progress(f"Dry run: Would remove {len(packages_to_remove)} package(s):")
    for pkg in packages_to_remove:
        logger.progress(f"  - {pkg} from apm.yml")
        try:
            dep_ref = _parse_dependency_entry(pkg)
            package_path = dep_ref.get_install_path(apm_modules_dir)
        except (ValueError, TypeError, AttributeError, KeyError):
            pkg_str = pkg if isinstance(pkg, str) else str(pkg)
            package_path = apm_modules_dir / pkg_str.split("/")[-1]
        if apm_modules_dir.exists() and package_path.exists():
            logger.progress(f"  - {pkg} from apm_modules/")

    from ...deps.lockfile import LockFile, get_lockfile_path

    lockfile_path = get_lockfile_path(Path("."))
    lockfile = LockFile.read(lockfile_path)
    if lockfile:
        removed_repo_urls = builtins.set()
        for pkg in packages_to_remove:
            try:
                ref = _parse_dependency_entry(pkg)
                removed_repo_urls.add(ref.repo_url)
            except (ValueError, TypeError, AttributeError, KeyError):
                removed_repo_urls.add(pkg)
        children_index = _build_children_index(lockfile)
        queue = builtins.list(removed_repo_urls)
        potential_orphans = builtins.set()
        while queue:
            parent_url = queue.pop()
            for dep in children_index.get(parent_url, []):
                key = dep.get_unique_key()
                if key in potential_orphans:
                    continue
                potential_orphans.add(key)
                queue.append(dep.repo_url)

        actual_orphans, _resolved_by_repairs = _compute_actual_orphans(
            lockfile,
            potential_orphans,
            removed_repo_urls,
            apm_yml_path,
            packages_to_remove,
            apm_modules_dir,
            logger,
        )
        # _resolved_by_repairs is intentionally discarded: this LockFile
        # instance is a throwaway local read (never .write()-ed back), and
        # --dry-run's preview must stay strictly read-only regardless.
        if actual_orphans:
            logger.progress(f"  Transitive dependencies that would be removed:")  # noqa: F541
            for orphan_key in sorted(actual_orphans):
                logger.progress(f"    - {orphan_key}")

    logger.success("Dry run complete - no changes made")


def _persist_uninstall_state(
    *,
    manifest_path,
    deploy_root,
    all_deployed_files,
    lockfile,
    lockfile_path,
    lockfile_updated: bool,
    scope,
    logger,
    pre_mcp_servers,
    modules_dir,
) -> None:
    """Steps 9-10 of the uninstall flow: sync integrations and flush lockfile.

    Encapsulates integration cleanup, deployment-state reconciliation, MCP
    server cleanup, and the deferred lockfile write.  Called after packages
    have been removed from disk and the in-memory lockfile has been mutated
    (Steps 4-8).  All exceptions are surfaced as logged warnings so the
    final summary in the caller is always reached.
    """
    import traceback as _traceback

    from ...core.scope import InstallScope
    from ...models.apm_package import APMPackage
    from .engine import _cleanup_stale_mcp, _sync_integrations_after_uninstall

    # Step 9: Sync integrations and reconcile deployment state.
    cleaned = {
        "prompts": 0,
        "agents": 0,
        "skills": 0,
        "commands": 0,
        "hooks": 0,
        "instructions": 0,
    }
    surviving_deployed_files = {}
    lockfile_ready = True
    try:
        apm_package = APMPackage.from_apm_yml(manifest_path)
        cleaned, surviving_deployed_files = _sync_integrations_after_uninstall(
            apm_package,
            deploy_root,
            all_deployed_files,
            logger,
            user_scope=scope is InstallScope.USER,
            lockfile=lockfile,
        )
    except Exception as _sync_err:
        logger.warning(f"Integration cleanup failed: {type(_sync_err).__name__}: {_sync_err}")
        logger.verbose_detail(_traceback.format_exc().rstrip())
        logger.verbose_detail(
            "Some integrated files may remain. Run `apm install --force` to resync."
        )

    if lockfile:
        try:
            from .lockfile_state import reconcile_uninstall_deployment_state

            lockfile_updated = (
                reconcile_uninstall_deployment_state(
                    lockfile,
                    deploy_root=deploy_root,
                    all_deployed_files=all_deployed_files,
                    surviving_deployed_files=surviving_deployed_files,
                )
                or lockfile_updated
            )
        except Exception as state_err:
            lockfile_ready = False
            logger.warning(
                "Lockfile state could not be reconciled. "
                "Run 'apm install --force' to resync before retrying."
            )
            logger.verbose_detail(f"Lockfile reconciliation error: {state_err}")

    for label, count in cleaned.items():
        if count > 0:
            logger.progress(f"Cleaned up {count} integrated {label}", symbol="check")
            logger.verbose_detail(f"    Removed {count} deployed {label} file(s)")

    # Step 10: MCP cleanup and deferred lockfile write.
    try:
        apm_package = APMPackage.from_apm_yml(manifest_path)
        _cleanup_stale_mcp(
            apm_package,
            lockfile,
            lockfile_path,
            pre_mcp_servers,
            modules_dir=modules_dir,
            project_root=deploy_root,
            user_scope=scope is InstallScope.USER,
            scope=scope,
            persist=False,
        )
    except Exception:
        logger.warning("MCP cleanup during uninstall failed")

    if lockfile and lockfile_updated and lockfile_ready:
        try:
            from .lockfile_state import lockfile_has_persisted_state

            if lockfile_has_persisted_state(lockfile):
                lockfile.write(lockfile_path)
            else:
                lockfile_path.unlink(missing_ok=True)
        except Exception:
            logger.warning(
                "Failed to update lockfile -- it may be out of sync with uninstalled packages."
            )
