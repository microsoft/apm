"""APM uninstall engine  -- validation, removal, and cleanup helpers."""

from __future__ import annotations

import builtins
from pathlib import Path

from ...deps.lockfile import LockFile
from ...integration.mcp_integrator import MCPIntegrator
from ...models.apm_package import DependencyReference
from ...utils.path_security import PathTraversalError, safe_rmtree
from ...utils.paths import portable_relpath
from ._sync import (
    _build_managed_buckets,
    _compute_skill_dirs_exist,
    _McpCleanupContext,
    _phase2_reintegrate_packages,
    _ReintegrationContext,
    _sync_integrations_after_uninstall,
)

_APM_MODULES_DIR_NAME = "apm_modules"


def _cleanup_stale_mcp(
    context_or_apm_package,
    lockfile=None,
    lockfile_path=None,
    old_mcp_servers=None,
    **kwargs,
):
    """Remove stale MCP servers, preserving the legacy engine patch target."""
    if isinstance(context_or_apm_package, _McpCleanupContext):
        context = context_or_apm_package
    else:
        valid_fields = _McpCleanupContext.__dataclass_fields__
        extra = {key: value for key, value in kwargs.items() if key in valid_fields}
        context = _McpCleanupContext(
            apm_package=context_or_apm_package,
            lockfile=lockfile,
            lockfile_path=lockfile_path,
            old_mcp_servers=old_mcp_servers or set(),
            **extra,
        )
    if not context.old_mcp_servers:
        return
    apm_modules_path = (
        context.modules_dir
        if context.modules_dir is not None
        else Path.cwd() / _APM_MODULES_DIR_NAME
    )
    remaining_mcp = MCPIntegrator.collect_transitive(
        apm_modules_path,
        context.lockfile_path,
        trust_private=True,
    )
    try:
        remaining_root_mcp = context.apm_package.get_mcp_dependencies()
    except Exception:
        remaining_root_mcp = []
    all_remaining_mcp = MCPIntegrator.deduplicate(remaining_root_mcp + remaining_mcp)
    new_mcp_servers = MCPIntegrator.get_server_names(all_remaining_mcp)
    stale_servers = context.old_mcp_servers - new_mcp_servers
    if stale_servers:
        MCPIntegrator.remove_stale(
            stale_servers,
            project_root=context.project_root,
            user_scope=context.user_scope,
            scope=context.scope,
        )
    MCPIntegrator.update_lockfile(new_mcp_servers, context.lockfile_path)


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


def _lockfile_lookup(
    lockfile: LockFile,
    plugin_name: str,
    marketplace_name: str,
    logger: CommandLogger,
) -> str | None:
    """Stage 1: offline lockfile scan returning a canonical key or ``None``.

    First tries an exact match (``discovered_via`` + ``marketplace_plugin_name``).
    Falls back to a plugin-name-only match and emits a provenance warning when
    the marketplace differs.
    """
    # First pass: exact match
    for dep in lockfile.dependencies.values():
        if dep.discovered_via == marketplace_name and dep.marketplace_plugin_name == plugin_name:
            return dep.get_unique_key()

    # Second pass: plugin_name match with different marketplace (provenance mismatch)
    for dep in lockfile.dependencies.values():
        if dep.marketplace_plugin_name == plugin_name and dep.discovered_via != marketplace_name:
            canonical = dep.get_unique_key()
            logger.warning(
                f"{plugin_name}@{marketplace_name} not found; "
                f"package was installed via {dep.discovered_via}. "
                f"Proceeding with uninstall of {canonical}."
            )
            return canonical

    return None


def _registry_lookup(
    plugin_name: str,
    marketplace_name: str,
    lockfile: LockFile | None,
    logger: CommandLogger,
    auth_resolver=None,
) -> str | None:
    """Stage 2: registry network call returning a canonical key or ``None``.

    Applies a supply-chain guard: rejects any canonical returned by the
    registry that is not already present in *lockfile*.  When *lockfile*
    is ``None`` the canonical is accepted with a verbose warning.
    """
    from ...marketplace.resolver import resolve_marketplace_plugin

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
            return None
        if lockfile is None:
            # No lockfile means no offline integrity anchor; behaviour is
            # accepted today but tracked as a supply-chain follow-up.
            logger.verbose_detail(
                f"No lockfile present; trusting registry canonical "
                f"{canonical} for {plugin_name}@{marketplace_name}."
            )
        return canonical
    except Exception as exc:
        logger.warning(
            f"Registry lookup for {plugin_name}@{marketplace_name} failed: "
            f"{exc}. Falling back to apm.yml match."
        )
        return None


def _resolve_single_marketplace_package(
    package: str,
    lockfile: LockFile | None,
    logger: CommandLogger,
    auth_resolver=None,
    dry_run: bool = False,
) -> str | None:
    """Resolve a single marketplace ref to its canonical ``owner/repo`` string.

    Returns the canonical string on success, or ``None`` when resolution
    fails (caller is responsible for error reporting via *logger*).
    """
    from ...marketplace.resolver import parse_marketplace_ref

    parsed = parse_marketplace_ref(package)
    if parsed is None:
        return None  # Not a marketplace ref; skipped silently

    plugin_name, marketplace_name, _ref = parsed
    canonical: str | None = None

    # Stage 1: Lockfile-first lookup (offline, zero network calls)
    if lockfile is not None:
        canonical = _lockfile_lookup(lockfile, plugin_name, marketplace_name, logger)

    # Stage 2: Registry fallback (silent, mirrors install behaviour)
    if canonical is None:
        if dry_run:
            logger.verbose_detail(
                f"Skipping registry fallback for {plugin_name}@{marketplace_name} (dry-run mode)"
            )
        else:
            canonical = _registry_lookup(
                plugin_name, marketplace_name, lockfile, logger, auth_resolver=auth_resolver
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

    return canonical


def _resolve_marketplace_packages(
    packages: list[str],
    lockfile: LockFile | None,
    logger: CommandLogger,
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
    resolved: dict[str, str | None] = {}

    for package in packages:
        if not _is_marketplace_ref(package):
            continue
        canonical = _resolve_single_marketplace_package(
            package, lockfile, logger, auth_resolver=auth_resolver, dry_run=dry_run
        )
        resolved[package] = canonical

    return resolved


def _find_dep_match(canonical_for_match: str, current_deps: list) -> object | None:
    """Search *current_deps* for the entry that matches *canonical_for_match*.

    Returns the raw dep entry (string, dict, or
    :class:`~apm_cli.models.apm_package.DependencyReference`) if found, or
    ``None`` when no match exists.
    """
    try:
        pkg_ref = DependencyReference.parse(canonical_for_match)
        pkg_identity = pkg_ref.get_identity()
    except Exception:
        pkg_identity = canonical_for_match

    for dep_entry in current_deps:
        try:
            dep_ref = _parse_dependency_entry(dep_entry)
            if dep_ref.get_identity() == pkg_identity:
                return dep_entry
        except (ValueError, TypeError, AttributeError, KeyError):
            dep_str = dep_entry if isinstance(dep_entry, str) else str(dep_entry)
            if dep_str == canonical_for_match:
                return dep_entry
    return None


def _validate_single_package(
    package: str,
    mkt_refs_set: set,
    mkt_resolved: dict,
    current_deps: list,
    logger: CommandLogger,
) -> tuple[object | None, str | None]:
    """Validate a single package and return a ``(matched_dep, not_found)`` pair.

    Exactly one of the returned values is non-``None``:

    * ``(matched_dep, None)`` -- *package* was found in *current_deps*; caller
      should append *matched_dep* to the ``packages_to_remove`` list.
    * ``(None, not_found_str)`` -- *package* could not be matched; caller
      should append *not_found_str* to the ``packages_not_found`` list.

    Args:
        package: The package identifier supplied by the user.
        mkt_refs_set: Pre-computed set of marketplace-ref strings in the
            current batch (used to distinguish them from bare invalid strings).
        mkt_resolved: Pre-resolved ``{marketplace_ref: canonical_or_None}``
            mapping produced by :func:`_resolve_marketplace_packages`.
        current_deps: Current dependency list from ``apm.yml``.
        logger: :class:`~apm_cli.core.command_logger.CommandLogger` for output.

    Returns:
        ``(matched_dep, None)`` or ``(None, not_found_str)``.
    """
    if "/" not in package:
        if package in mkt_refs_set:
            canonical = mkt_resolved.get(package)
            if canonical is None:
                # Error already logged by _resolve_marketplace_packages
                return None, package
            canonical_for_match = canonical
            display_label = package
        else:
            logger.error(
                f"Invalid package format: {package}. "
                "Use 'owner/repo' or 'plugin-name@marketplace' format."
            )
            return None, package
    else:
        canonical_for_match = package
        display_label = package

    matched_dep = _find_dep_match(canonical_for_match, current_deps)

    if matched_dep is not None:
        if canonical_for_match != display_label:
            logger.progress(
                f"{display_label} - found in apm.yml (as {canonical_for_match})",
                symbol="check",
            )
        else:
            logger.progress(f"{display_label} - found in apm.yml", symbol="check")
        return matched_dep, None

    if canonical_for_match != display_label:
        logger.warning(f"{display_label} ({canonical_for_match}) - not found in apm.yml")
    else:
        logger.warning(f"{display_label} - not found in apm.yml")
    return None, package


def _validate_uninstall_packages(
    packages: list[str],
    current_deps: list,
    logger: CommandLogger,
    lockfile: LockFile | None = None,
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
        matched_dep, not_found = _validate_single_package(
            package, mkt_refs_set, mkt_resolved, current_deps, logger
        )
        if matched_dep is not None:
            packages_to_remove.append(matched_dep)
        else:
            packages_not_found.append(not_found)

    return packages_to_remove, packages_not_found


def _dry_run_uninstall(packages_to_remove, apm_modules_dir, logger):
    """Show what would be removed without making changes."""
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

    from ...deps.lockfile import get_lockfile_path

    lockfile_path = get_lockfile_path(Path("."))
    lockfile = LockFile.read(lockfile_path)
    if lockfile:
        _show_lockfile_orphans_preview(packages_to_remove, lockfile, logger)

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
    orphans = _collect_transitive_orphans(children_index, removed_repo_urls)

    if not orphans:
        return 0, builtins.set()

    # Determine remaining deps to avoid removing still-needed packages
    remaining_deps = _compute_remaining_deps(lockfile, apm_yml_path, removed_repo_urls, orphans)

    actual_orphans = orphans - remaining_deps
    removed = 0
    deleted_orphan_paths = []
    for orphan_key in actual_orphans:
        orphan_dep = lockfile.get_dependency(orphan_key)
        if not orphan_dep:
            continue
        deleted_path = _remove_orphan_if_exists(orphan_key, apm_modules_dir, logger)
        if deleted_path:
            removed += 1
            deleted_orphan_paths.append(deleted_path)

    from ...integration.base_integrator import BaseIntegrator as _BI

    _BI.cleanup_empty_parents(deleted_orphan_paths, stop_at=apm_modules_dir)
    return removed, actual_orphans


def _collect_transitive_orphans(
    children_index: dict, removed_repo_urls: builtins.set
) -> builtins.set:
    """BFS from removed root URLs to collect all transitively orphaned dep keys."""
    orphans: builtins.set = builtins.set()
    queue = builtins.list(removed_repo_urls)
    while queue:
        parent_url = queue.pop()
        for dep in children_index.get(parent_url, []):
            key = dep.get_unique_key()
            if key in orphans:
                continue
            orphans.add(key)
            queue.append(dep.repo_url)
    return orphans


def _remove_orphan_if_exists(orphan_key: str, apm_modules_dir: Path, logger) -> Path | None:
    """Resolve the orphan's install path and delete it if it exists. Returns deleted path or None."""
    try:
        orphan_ref = DependencyReference.parse(orphan_key)
        orphan_path = orphan_ref.get_install_path(apm_modules_dir)
    except ValueError:
        parts = orphan_key.split("/")
        orphan_path = (
            apm_modules_dir.joinpath(*parts) if len(parts) >= 2 else apm_modules_dir / orphan_key
        )

    if not orphan_path.exists():
        return None
    try:
        safe_rmtree(orphan_path, apm_modules_dir)
        logger.progress(f"Removed transitive dependency {orphan_key} from apm_modules/")
        logger.verbose_detail(f"    Path: {portable_relpath(orphan_path, apm_modules_dir)}")
        return orphan_path
    except Exception as e:
        logger.error(f"Failed to remove transitive dep {orphan_key}: {e}")
        return None


def _show_lockfile_orphans_preview(packages_to_remove: list, lockfile, logger) -> None:
    """Show the transitive dependencies that would also be removed (dry-run mode)."""
    removed_repo_urls: builtins.set = builtins.set()
    for pkg in packages_to_remove:
        try:
            ref = _parse_dependency_entry(pkg)
            removed_repo_urls.add(ref.repo_url)
        except (ValueError, TypeError, AttributeError, KeyError):
            removed_repo_urls.add(pkg)
    children_index = _build_children_index(lockfile)
    queue = builtins.list(removed_repo_urls)
    potential_orphans: builtins.set = builtins.set()
    while queue:
        parent_url = queue.pop()
        for dep in children_index.get(parent_url, []):
            key = dep.get_unique_key()
            if key in potential_orphans:
                continue
            potential_orphans.add(key)
            queue.append(dep.repo_url)
    if potential_orphans:
        logger.progress("  Transitive dependencies that would be removed:")
        for orphan_key in sorted(potential_orphans):
            logger.progress(f"    - {orphan_key}")


def _compute_remaining_deps(
    lockfile, apm_yml_path: Path, removed_repo_urls: builtins.set, orphans: builtins.set
) -> builtins.set:
    """Build the set of dep keys that must be kept (still referenced after removal)."""
    remaining_deps: builtins.set = builtins.set()
    try:
        from ...utils.yaml_io import load_yaml

        updated_data = load_yaml(apm_yml_path) or {}
        for dep_str in updated_data.get("dependencies", {}).get("apm", []) or []:
            try:
                ref = _parse_dependency_entry(dep_str)
                remaining_deps.add(ref.get_unique_key())
            except (ValueError, TypeError, AttributeError, KeyError):
                remaining_deps.add(dep_str)
    except Exception:
        pass
    for dep in lockfile.get_package_dependencies():
        key = dep.get_unique_key()
        if key not in orphans and dep.repo_url not in removed_repo_urls:
            remaining_deps.add(key)
    return remaining_deps
