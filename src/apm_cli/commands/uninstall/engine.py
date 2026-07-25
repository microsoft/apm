"""APM uninstall engine  -- validation, removal, and cleanup helpers."""

import builtins
from pathlib import Path

from ...constants import APM_MODULES_DIR
from ...core.command_logger import CommandLogger
from ...deps.lockfile import LockFile
from ...integration.mcp_integrator import MCPIntegrator
from ...models.apm_package import DependencyReference
from ...utils.path_security import PathTraversalError, safe_rmtree
from ...utils.paths import portable_relpath


def _is_marketplace_ref(package: str) -> bool:
    """Check if *package* is marketplace notation using the public API."""
    from ...marketplace.resolver import parse_marketplace_ref

    return parse_marketplace_ref(package) is not None


def _build_children_index(lockfile):
    """Build parent_url -> [child_deps] index in a single O(n) pass.

    Returns a dict mapping each ``resolved_by`` URL to the list of
    dependency objects that claim it as their parent.
    """
    children = {}
    for dep in lockfile.get_package_dependencies():
        parent = dep.resolved_by
        if parent:
            if parent not in children:
                children[parent] = []
            children[parent].append(dep)
    return children


def _parse_dependency_entry(dep_entry):
    """Parse a dependency entry from apm.yml into a DependencyReference."""
    if isinstance(dep_entry, DependencyReference):
        return dep_entry
    if isinstance(dep_entry, str):
        return DependencyReference.parse(dep_entry)
    if isinstance(dep_entry, builtins.dict):
        return DependencyReference.parse_from_dict(dep_entry)
    raise ValueError(f"Unsupported dependency entry type: {type(dep_entry).__name__}")


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


def _resolve_marketplace_packages(
    packages: list[str],
    lockfile: "LockFile | None",
    logger: "CommandLogger",
    auth_resolver=None,
    dry_run: bool = False,
) -> dict[str, str | None]:
    """Resolve marketplace refs (NAME@MARKETPLACE[#REF]) to canonical owner/repo strings.

    Resolution proceeds in three stages for each marketplace-formatted package:

    1. **Lockfile lookup (offline)**: scan ``lockfile.dependencies`` for entries
       where ``discovered_via == marketplace_name`` and
       ``marketplace_plugin_name == plugin_name``.  When found, use the
       dependency's unique key as the canonical identity.  If an entry for the
       same plugin name exists under a *different* marketplace, a provenance-
       mismatch warning is emitted and that entry is used.
    2. **Registry fallback (silent)**: call :func:`parse_marketplace_ref` then
       :func:`resolve_marketplace_plugin` to obtain the canonical ``owner/repo``
       from the marketplace registry.  Skipped when *dry_run* is ``True``.
       A supply-chain guard refuses any canonical that is not already present
       in the lockfile (prevents a poisoned registry from removing an unrelated
       installed package).  Network errors fail only the affected package;
       remaining packages in the batch continue.
    3. **Unresolvable**: an error is logged with marketplace-specific wording
       and the package maps to ``None`` in the returned dict.

    Args:
        packages: List of marketplace-formatted package strings to resolve.
        lockfile: Current :class:`~apm_cli.deps.lockfile.LockFile` object, or
            ``None`` when no lockfile exists.
        logger: :class:`~apm_cli.core.command_logger.CommandLogger` for output.
        auth_resolver: Optional auth resolver forwarded to the registry call.
        dry_run: When ``True``, skip the network registry call (Stage 2).

    Returns:
        A dict mapping each original marketplace ref to its resolved canonical
        string, or ``None`` when resolution failed.
    """
    from ...marketplace.resolver import parse_marketplace_ref, resolve_marketplace_plugin

    resolved: dict[str, str | None] = {}

    for package in packages:
        parsed = parse_marketplace_ref(package)
        if parsed is None:
            continue  # Not a marketplace ref; skipped silently

        plugin_name, marketplace_name, _ref = parsed
        canonical: str | None = None

        # Stage 1: Lockfile-first lookup (offline, zero network calls)
        if lockfile is not None:
            # First pass: exact match (both discovered_via AND marketplace_plugin_name)
            for dep in lockfile.dependencies.values():
                if (
                    dep.discovered_via == marketplace_name
                    and dep.marketplace_plugin_name == plugin_name
                ):
                    canonical = dep.get_unique_key()
                    break

            # Second pass: plugin_name match with different marketplace (provenance mismatch)
            if canonical is None:
                for dep in lockfile.dependencies.values():
                    if (
                        dep.marketplace_plugin_name == plugin_name
                        and dep.discovered_via != marketplace_name
                    ):
                        canonical = dep.get_unique_key()
                        logger.warning(
                            f"{plugin_name}@{marketplace_name} not found; "
                            f"package was installed via {dep.discovered_via}. "
                            f"Proceeding with uninstall of {canonical}."
                        )
                        break

        # Stage 2: Registry fallback (silent, mirrors install behaviour)
        if canonical is None:
            if dry_run:
                logger.verbose_detail(
                    f"Skipping registry fallback for {plugin_name}@{marketplace_name} "
                    "(dry-run mode)"
                )
            else:
                logger.progress(
                    f"Resolving {plugin_name}@{marketplace_name} via registry...",
                    symbol="search",
                )
                try:
                    resolution = resolve_marketplace_plugin(
                        plugin_name, marketplace_name, auth_resolver=auth_resolver
                    )
                    canonical = resolution.canonical
                    # Supply-chain guard: refuse registry canonicals not present in lockfile
                    if lockfile is not None and canonical not in lockfile.dependencies:
                        logger.warning(
                            f"Registry resolved {plugin_name}@{marketplace_name} to "
                            f"{canonical}, but it is not recorded in apm.lock.yaml. "
                            "Refusing as a supply-chain precaution; use "
                            f"`apm uninstall {canonical}` directly if this is correct."
                        )
                        canonical = None
                    elif lockfile is None:
                        # No lockfile means no offline integrity anchor; behaviour is
                        # accepted today but tracked as a supply-chain follow-up.
                        logger.verbose_detail(
                            f"No lockfile present; trusting registry canonical "
                            f"{canonical} for {plugin_name}@{marketplace_name}."
                        )
                except Exception as exc:
                    logger.warning(
                        f"Registry lookup for {plugin_name}@{marketplace_name} failed: "
                        f"{exc}. Falling back to apm.yml match."
                    )

        # Stage 3: Not found in either source -- surface a clear error
        if canonical is None:
            if dry_run:
                logger.warning(
                    f"{plugin_name}@{marketplace_name} could not be resolved in dry-run "
                    "(registry fallback skipped). Re-run without --dry-run, or use "
                    "owner/repo notation to preview directly."
                )
            else:
                logger.error(
                    f"{plugin_name}@{marketplace_name} could not be resolved -- "
                    "use owner/repo format to uninstall directly, or run "
                    "`apm list` to find the owner/repo canonical name "
                    "then use `apm uninstall owner/repo` directly."
                )

        resolved[package] = canonical

    return resolved


def _validate_uninstall_packages(
    packages: list[str],
    current_deps: list,
    logger: "CommandLogger",
    lockfile: "LockFile | None" = None,
    auth_resolver=None,
    dry_run: bool = False,
) -> tuple[list, list]:
    """Validate which packages can be removed and return matched/unmatched lists.

    Accepts both canonical ``owner/repo`` strings and marketplace refs of the
    form ``NAME@MARKETPLACE[#REF]``.  Marketplace refs are resolved to their
    canonical form before being matched against the ``current_deps`` list from
    ``apm.yml``.

    Args:
        packages: Package identifiers supplied by the user.
        current_deps: Current dependency list read from ``apm.yml``.
        logger: :class:`~apm_cli.core.command_logger.CommandLogger` for output.
        lockfile: Optional :class:`~apm_cli.deps.lockfile.LockFile` used for
            offline marketplace resolution.  When ``None`` the registry fallback
            is attempted instead.
        auth_resolver: Optional auth resolver forwarded to the registry call.
        dry_run: When ``True``, skip the network registry call in Stage 2.

    Returns:
        A two-tuple ``(packages_to_remove, packages_not_found)`` where
        *packages_to_remove* contains matched dep entries and
        *packages_not_found* contains unresolved or unmatched package strings.
    """
    # Pre-resolve any marketplace refs before the main validation loop
    mkt_refs_set = {p for p in packages if _is_marketplace_ref(p)}
    mkt_resolved: dict[str, str | None] = {}
    if mkt_refs_set:
        mkt_resolved = _resolve_marketplace_packages(
            list(mkt_refs_set),
            lockfile,
            logger,
            auth_resolver=auth_resolver,
            dry_run=dry_run,
        )

    packages_to_remove = []
    packages_not_found = []

    for package in packages:
        # A package arg is either: (a) a marketplace ref in
        # `name@marketplace[#ref]` form (no slash), (b) an `owner/repo`
        # slug, or (c) a local filesystem path. The legacy guard below
        # only handled (a) when there is no `/`, but Windows absolute
        # paths use backslashes (e.g. `C:\Users\...\my-pkg`) and have
        # no `/` either -- they were wrongly rejected as "Invalid
        # package format" and the DB row for any deployed copilot-app
        # workflow would leak. Use the canonical local-path detector
        # so paths fall through to DependencyReference parsing on
        # every platform.
        is_local = DependencyReference.is_local_path(package)
        if "/" not in package and not is_local:
            if package in mkt_refs_set:
                canonical = mkt_resolved.get(package)
                if canonical is None:
                    # Error already logged by _resolve_marketplace_packages;
                    # surface in packages_not_found so caller counts are accurate.
                    packages_not_found.append(package)
                    continue
                canonical_for_match = canonical
                display_label = package
            else:
                logger.error(
                    f"Invalid package format: {package}. "
                    "Use 'owner/repo' or 'plugin-name@marketplace' format."
                )
                packages_not_found.append(package)
                continue
        else:
            canonical_for_match = package
            display_label = package

        matched_dep = None
        try:
            pkg_ref = DependencyReference.parse(canonical_for_match)
            pkg_identity = pkg_ref.get_identity()
        except Exception:
            pkg_identity = canonical_for_match

        for dep_entry in current_deps:
            try:
                dep_ref = _parse_dependency_entry(dep_entry)
                if dep_ref.get_identity() == pkg_identity:
                    matched_dep = dep_entry
                    break
            except (ValueError, TypeError, AttributeError, KeyError):
                dep_str = dep_entry if isinstance(dep_entry, str) else str(dep_entry)
                if dep_str == canonical_for_match:
                    matched_dep = dep_entry
                    break

        if matched_dep is not None:
            packages_to_remove.append(matched_dep)
            if canonical_for_match != display_label:
                logger.progress(
                    f"{display_label} - found in apm.yml (as {canonical_for_match})",
                    symbol="check",
                )
            else:
                logger.progress(f"{display_label} - found in apm.yml", symbol="check")
        else:
            packages_not_found.append(package)
            if canonical_for_match != display_label:
                logger.warning(f"{display_label} ({canonical_for_match}) - not found in apm.yml")
            else:
                logger.warning(f"{display_label} - not found in apm.yml")

    return packages_to_remove, packages_not_found


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


def _remove_packages_from_disk(packages_to_remove, apm_modules_dir, logger):
    """Remove direct packages from apm_modules/ and return removal count."""
    removed = 0
    if not apm_modules_dir.exists():
        return removed

    deleted_pkg_paths = []
    for package in packages_to_remove:
        try:
            dep_ref = _parse_dependency_entry(package)
            package_path = dep_ref.get_install_path(apm_modules_dir)
        except PathTraversalError as e:
            logger.error(f"Refusing to remove {package}: {e}")
            continue
        except (ValueError, TypeError, AttributeError, KeyError):
            package_str = package if isinstance(package, str) else str(package)
            repo_parts = package_str.split("/")
            if len(repo_parts) >= 2:
                package_path = apm_modules_dir.joinpath(*repo_parts)
            else:
                package_path = apm_modules_dir / package_str

        if package_path.exists():
            try:
                safe_rmtree(package_path, apm_modules_dir)
                logger.progress(f"Removed {package} from apm_modules/")
                logger.verbose_detail(
                    f"    Path: {portable_relpath(package_path, apm_modules_dir)}"
                )
                removed += 1
                deleted_pkg_paths.append(package_path)
            except Exception as e:
                logger.error(f"Failed to remove {package} from apm_modules/: {e}")
        else:
            logger.warning(f"Package {package} not found in apm_modules/")

    from ...integration.base_integrator import BaseIntegrator as _BI2

    _BI2.cleanup_empty_parents(deleted_pkg_paths, stop_at=apm_modules_dir)
    return removed


def _cleanup_transitive_orphans(
    lockfile, packages_to_remove, apm_modules_dir, apm_yml_path, logger
):
    """Remove orphaned transitive deps and return (removed_count, actual_orphan_keys)."""

    if not lockfile or not apm_modules_dir.exists():
        return 0, builtins.set()

    removed_repo_urls = builtins.set()
    for pkg in packages_to_remove:
        try:
            ref = _parse_dependency_entry(pkg)
            removed_repo_urls.add(ref.repo_url)
        except (ValueError, TypeError, AttributeError, KeyError):
            removed_repo_urls.add(pkg)

    # Find transitive orphans recursively
    children_index = _build_children_index(lockfile)
    orphans = builtins.set()
    queue = builtins.list(removed_repo_urls)
    while queue:
        parent_url = queue.pop()
        for dep in children_index.get(parent_url, []):
            key = dep.get_unique_key()
            if key in orphans:
                continue
            orphans.add(key)
            queue.append(dep.repo_url)

    if not orphans:
        return 0, builtins.set()

    # Determine which candidates are truly unreachable (still-needed direct
    # deps and lockfile survivors, PLUS anything a surviving package's own
    # real manifest still forward-reaches -- see _compute_actual_orphans).
    actual_orphans, resolved_by_repairs = _compute_actual_orphans(
        lockfile,
        orphans,
        removed_repo_urls,
        apm_yml_path,
        packages_to_remove,
        apm_modules_dir,
        logger,
    )

    # Repair rescued candidates' resolved_by/local_path BEFORE deleting
    # the true orphans below. Without this, a rescued entry keeps pointing
    # at a parent that may no longer be in the lockfile, which silently
    # defeats the NEXT uninstall's backward orphan-candidate scan
    # (_build_children_index is keyed on resolved_by) and permanently
    # fail-closes any later reachability walk needing to re-resolve this
    # same entry's on-disk directory (see _compute_actual_orphans'
    # docstring). Real removal path only -- --dry-run never reaches here.
    for rescued_key, (new_parent_repo_url, new_local_path) in resolved_by_repairs.items():
        rescued_dep = lockfile.get_dependency(rescued_key)
        if rescued_dep is None:
            continue
        rescued_dep.resolved_by = new_parent_repo_url
        if new_local_path is not None:
            rescued_dep.local_path = new_local_path

    removed = 0
    deleted_orphan_paths = []
    for orphan_key in actual_orphans:
        orphan_dep = lockfile.get_dependency(orphan_key)
        if not orphan_dep:
            continue
        try:
            orphan_ref = orphan_dep.to_dependency_ref()
            orphan_path = orphan_ref.get_install_path(apm_modules_dir)
        except ValueError:
            parts = orphan_key.split("/")
            orphan_path = (
                apm_modules_dir.joinpath(*parts)
                if len(parts) >= 2
                else apm_modules_dir / orphan_key
            )

        if orphan_path.exists():
            try:
                safe_rmtree(orphan_path, apm_modules_dir)
                logger.progress(f"Removed transitive dependency {orphan_key} from apm_modules/")
                logger.verbose_detail(f"    Path: {portable_relpath(orphan_path, apm_modules_dir)}")
                removed += 1
                deleted_orphan_paths.append(orphan_path)
            except Exception as e:
                logger.error(f"Failed to remove transitive dep {orphan_key}: {e}")

    from ...integration.base_integrator import BaseIntegrator as _BI

    _BI.cleanup_empty_parents(deleted_orphan_paths, stop_at=apm_modules_dir)
    return removed, actual_orphans


def _sync_integrations_after_uninstall(
    apm_package: object,
    project_root: Path,
    all_deployed_files: set[str],
    logger: CommandLogger,
    user_scope: bool = False,
    lockfile: LockFile | None = None,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Remove deployed files and re-integrate from remaining packages.

    When *user_scope* is ``True``, targets are resolved for user-level
    deployment so cleanup and re-integration use the correct paths.

    *lockfile*, when provided, must be the post-removal in-memory
    lockfile (orphans already deleted). The on-disk lockfile is still
    stale at this point in the uninstall pipeline, so Phase 2 must not
    re-read it from disk.
    """
    from ...install.services import _deployed_path_entry, _skill_bundle_file_entries
    from ...integration.base_integrator import BaseIntegrator
    from ...integration.dispatch import get_dispatch_table
    from ...integration.targets import resolve_targets
    from ...models.apm_package import (
        build_installed_package_info,
        surviving_dependency_refs_for_reintegration,
    )
    from ...primitives.discovery import clear_discovery_cache

    # Phase 2 re-integration walks the on-disk primitive set after Phase 1
    # has removed the uninstalled package's files. The process-scoped
    # discovery memo populated earlier in this CLI run would otherwise
    # serve the pre-removal snapshot, causing deleted primitives to be
    # re-integrated. See #1533 follow-up.
    clear_discovery_cache()

    _dispatch = get_dispatch_table()
    _integrators = {name: entry.integrator_class() for name, entry in _dispatch.items()}

    # Resolve targets once -- used for both Phase 1 removal and Phase 2 re-integration.
    config_target = list(apm_package.canonical_targets)
    _explicit = config_target or None
    _resolved_targets = resolve_targets(
        project_root, user_scope=user_scope, explicit_target=_explicit
    )

    sync_managed = all_deployed_files if all_deployed_files else None
    if sync_managed is not None:
        # Partition against default KNOWN_TARGETS for legacy/project-scope
        # paths, then merge with resolved targets for user-scope paths.
        # This ensures both .github/ (legacy) and .copilot/ (resolved)
        # prefixes are recognized during uninstall cleanup.
        _buckets = BaseIntegrator.partition_managed_files(sync_managed)
        if user_scope and _resolved_targets:
            _scope_buckets = BaseIntegrator.partition_managed_files(
                sync_managed, targets=_resolved_targets
            )
            for _bname, _bpaths in _scope_buckets.items():
                _existing = _buckets.get(_bname)
                if _existing is not None:
                    _existing.update(_bpaths)
                else:
                    _buckets[_bname] = _bpaths
    else:
        _buckets = None

    counts = {entry.counter_key: 0 for entry in _dispatch.values()}
    package_deployed_files: dict[str, list[str]] = {}

    # Phase 1: Remove all APM-deployed files
    # Per-target sync for primitives with sync_for_target
    for _target in _resolved_targets:
        for _prim_name, _mapping in _target.primitives.items():
            _entry = _dispatch.get(_prim_name)
            if not _entry or _entry.sync_method != "sync_for_target":
                continue
            _effective_root = _mapping.deploy_root or _target.root_dir
            _deploy_dir = project_root / _effective_root / _mapping.subdir
            # Dynamic-root targets (e.g. copilot-app) have no filesystem
            # deploy dir; their managed files are URIs that the integrator
            # resolves internally.  Skip the dir-exists guard for them.
            _is_dynamic = _target.resolved_deploy_root is not None
            if not _is_dynamic and not _deploy_dir.exists():
                continue
            _managed_subset = None
            if _buckets is not None:
                _bucket_key = BaseIntegrator.partition_bucket_key(_prim_name, _target.name)
                _managed_subset = _buckets.get(_bucket_key, set())
            result = _integrators[_prim_name].sync_for_target(
                _target,
                apm_package,
                project_root,
                managed_files=_managed_subset,
            )
            counts[_entry.counter_key] += result.get("files_removed", 0)

    # Skills (multi-target, handled by SkillIntegrator)
    # Check both target root_dir and deploy_root for skill directories
    _skill_dirs_exist = False
    for t in _resolved_targets:
        if t.supports("skills"):
            sm = t.primitives["skills"]
            er = sm.deploy_root or t.root_dir
            if (project_root / er / "skills").exists():
                _skill_dirs_exist = True
                break

    # Scan sync_managed DIRECTLY for cowork:// entries.
    # partition_managed_files() uses resolved_deploy_root to detect
    # dynamic-root targets, but the static KNOWN_TARGETS["copilot-cowork"]
    # always has resolved_deploy_root=None (it is only set after for_scope()
    # resolves the OneDrive path at install time).  As a result, cowork://
    # paths are never routed into _buckets["skills"] by the partition, so
    # the bucket-based _has_cowork_skills check in the previous fix always
    # returned False.  Bypassing the bucket and scanning sync_managed
    # directly is the correct approach: no partition logic is involved.
    _cowork_skill_files: set = set()
    if sync_managed:
        from ...integration.copilot_cowork_paths import COWORK_URI_SCHEME

        _cowork_skill_files = {p for p in sync_managed if p.startswith(COWORK_URI_SCHEME)}
    _has_cowork_skills = bool(_cowork_skill_files)

    if _skill_dirs_exist or _has_cowork_skills:
        # Merge cowork entries into the skills bucket so sync_integration
        # receives them via managed_files.
        if _has_cowork_skills and _buckets is not None:
            _buckets.setdefault("skills", set()).update(_cowork_skill_files)
        elif _has_cowork_skills:
            _buckets = {"skills": _cowork_skill_files, "hooks": set()}

        # When cowork entries are present, pass targets=None so
        # sync_integration builds skill_prefix_tuple from KNOWN_TARGETS
        # (which includes the copilot-cowork target with user_root_resolver
        # set).  Using _resolved_targets alone would yield only the local
        # prefix (.copilot/skills/) and cowork:// paths would be silently
        # skipped by the startswith() guard inside sync_integration.
        _sync_targets = None if _has_cowork_skills else _resolved_targets
        result = _integrators["skills"].sync_integration(
            apm_package,
            project_root,
            managed_files=_buckets["skills"] if _buckets else None,
            targets=_sync_targets,
        )
        counts["skills"] = result.get("files_removed", 0)

    # Scan sync_managed DIRECTLY for copilot-app-db:// entries.
    # The copilot-app target is opt-in: resolve_targets() excludes it from the
    # default user-scope set unless --target copilot-app was passed at install
    # time and recorded on the package's canonical target list. Without this scan, prompts
    # deployed to ~/.copilot/data.db would never be deleted on uninstall
    # because the per-target loop above does not iterate copilot-app.
    if sync_managed:
        from ...integration.copilot_app_db import COPILOT_APP_LOCKFILE_PREFIX

        _copilot_app_files = {p for p in sync_managed if p.startswith(COPILOT_APP_LOCKFILE_PREFIX)}
        if _copilot_app_files:
            # Find or synthesise a user-scope copilot-app TargetProfile.
            from ...integration.targets import KNOWN_TARGETS

            _ca_target = next(
                (t for t in _resolved_targets if t.name == "copilot-app"),
                None,
            )
            if _ca_target is None:
                _ca_static = KNOWN_TARGETS.get("copilot-app")
                if _ca_static is not None:
                    _ca_target = _ca_static.for_scope(user_scope=True)
            if _ca_target is not None:
                result = _integrators["prompts"].sync_for_target(
                    _ca_target,
                    apm_package,
                    project_root,
                    managed_files=_copilot_app_files,
                )
                counts["prompts"] += result.get("files_removed", 0)

    # Hooks: managed-file removal always scans every KNOWN_TARGETS prefix
    # (safe -- it only ever deletes files already tracked as this
    # package's own deployed_files). The merged-hook JSON wipe, in
    # contrast, is scoped to `_resolved_targets` -- the SAME set the
    # Phase 2 rebuild loop below iterates -- so a harness dropped from
    # this project's `targets:` list is never wiped for packages that
    # remain declared and installed (#2250).
    result = _integrators["hooks"].sync_integration(
        apm_package,
        project_root,
        managed_files=_buckets["hooks"] if _buckets else None,
        targets=_resolved_targets,
    )
    counts["hooks"] = result.get("files_removed", 0)

    # Phase 2: Re-integrate from remaining installed packages
    # Re-clear the discovery memo: Phase 1 mutated the on-disk primitive
    # set (removed files), so any cache snapshot taken between entry and
    # here is stale. Integrator dispatch below walks discovery internally.
    clear_discovery_cache()
    _targets = _resolved_targets

    # Lockfile survivors include transitive packages still required by a
    # remaining direct dep (#2254). Pass the in-memory post-removal
    # lockfile when the caller has one -- disk is still stale here.
    for dep_ref in surviving_dependency_refs_for_reintegration(
        apm_package, project_root, lockfile=lockfile
    ):
        pkg_info = build_installed_package_info(dep_ref, Path(APM_MODULES_DIR))
        if pkg_info is None:
            continue
        dep_key = dep_ref.get_unique_key()
        deployed_files = package_deployed_files.setdefault(dep_key, [])

        try:
            for _target in _targets:
                for _prim_name in _target.primitives:
                    _entry = _dispatch.get(_prim_name)
                    if not _entry or _entry.multi_target:
                        continue
                    integration_result = getattr(_integrators[_prim_name], _entry.integrate_method)(
                        _target,
                        pkg_info,
                        project_root,
                    )
                    deployed_files.extend(
                        _deployed_path_entry(path, project_root, _targets)
                        for path in integration_result.target_paths
                    )
            skill_result = _integrators["skills"].integrate_package_skill(
                pkg_info,
                project_root,
                targets=_targets,
            )
            for path in skill_result.target_paths:
                deployed_files.append(_deployed_path_entry(path, project_root, _targets))
                deployed_files.extend(_skill_bundle_file_entries(path, project_root, _targets))
        except Exception as exc:
            pkg_id = dep_ref.get_identity() if hasattr(dep_ref, "get_identity") else str(dep_ref)
            logger.warning(
                f"Best-effort re-integration skipped for {pkg_id}: {exc}. "
                "Run 'apm install' to rebuild integrated files."
            )

    return counts, package_deployed_files


def _cleanup_stale_mcp(
    apm_package,
    lockfile,
    lockfile_path,
    old_mcp_servers,
    modules_dir=None,
    project_root=None,
    user_scope: bool = False,
    scope=None,
    persist: bool = True,
):
    """Remove MCP servers that are no longer needed after uninstall."""
    if not old_mcp_servers:
        return
    from apm_cli.integration.mcp_config_view import CurrentMcpConfigView

    apm_modules_path = modules_dir if modules_dir is not None else Path.cwd() / APM_MODULES_DIR
    view = CurrentMcpConfigView.derive(
        apm_package,
        lockfile,
        apm_modules_path,
        trust_transitive_self_defined=True,
    )
    new_mcp_servers = MCPIntegrator.get_server_names(view.dependencies)
    stale_servers = old_mcp_servers - new_mcp_servers
    if stale_servers:
        MCPIntegrator.remove_stale(
            stale_servers,
            project_root=project_root,
            user_scope=user_scope,
            scope=scope,
        )
    if persist:
        MCPIntegrator.update_lockfile(
            new_mcp_servers,
            lockfile_path,
            mcp_configs=dict(view.configs),
            mcp_config_provenance=dict(view.provenance),
        )
        return

    lockfile.mcp_servers = sorted(new_mcp_servers)
    lockfile.mcp_configs = dict(view.configs)
    lockfile.mcp_config_provenance = dict(view.provenance)
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    DeploymentLedgerCodec.replace_mcp_target_servers(
        lockfile,
        {
            runtime: sorted(set(servers).intersection(new_mcp_servers))
            for runtime, servers in lockfile.mcp_target_servers.items()
            if set(servers).intersection(new_mcp_servers)
        },
    )
