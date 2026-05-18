"""APM dependency update engine."""

from __future__ import annotations

import sys
from dataclasses import dataclass

import click

from ....constants import APM_YML_FILENAME
from ....core.scope import InstallScope, get_apm_dir


def _resolve_legacy_skill_paths(legacy_skill_paths: bool) -> bool:
    """Resolve the compatibility flag via CLI first, then env defaults."""
    if legacy_skill_paths:
        return legacy_skill_paths

    from ....integration.targets import should_use_legacy_skill_paths

    return should_use_legacy_skill_paths()


def _load_apm_package_for_update(apm_yml_path, logger):
    """Load and validate the project manifest for dependency updates."""
    try:
        return sys.modules["apm_cli.commands.deps.cli"].APMPackage.from_apm_yml(apm_yml_path)
    except Exception as exc:
        logger.error(f"Failed to parse {APM_YML_FILENAME}: {exc}")
        sys.exit(1)


@dataclass(frozen=True, slots=True)
class _UpdateRunContext:
    """Arguments for executing dependency updates through the install engine."""

    apm_package: object
    logger: object
    auth_resolver: object
    verbose: bool
    only_pkgs: object
    force: bool
    parallel_downloads: int
    target: object
    scope: object
    legacy_skill_paths: bool


def _run_dependency_update(context: _UpdateRunContext):
    """Execute the install engine in update-refs mode."""
    from ...install import _install_apm_dependencies

    try:
        return _install_apm_dependencies(
            context.apm_package,
            update_refs=True,
            verbose=context.verbose,
            only_packages=context.only_pkgs,
            force=context.force,
            parallel_downloads=context.parallel_downloads,
            logger=context.logger,
            auth_resolver=context.auth_resolver,
            target=context.target,
            scope=context.scope,
            legacy_skill_paths=context.legacy_skill_paths,
        )
    except Exception as exc:
        context.logger.error(f"Update failed: {exc}")
        if not context.verbose:
            context.logger.progress("Run with --verbose for detailed diagnostics")
        sys.exit(1)


def _validate_packages(packages, all_deps, logger):
    """Validate and normalize requested packages to canonical dependency keys.

    Returns list of canonical package keys, or exits with error if any invalid.
    """
    if not packages:
        return None

    token_to_canonical: dict[str, str] = {}
    for dep in all_deps:
        canonical_key = dep.get_unique_key() or dep.repo_url or dep.get_display_name()
        tokens = {canonical_key, dep.get_display_name(), dep.repo_url}
        if hasattr(dep, "alias") and dep.alias:
            tokens.add(dep.alias)
        parts = dep.repo_url.split("/")
        if len(parts) >= 2:
            tokens.add(parts[-1])
        for token in tokens:
            if token and token not in token_to_canonical:
                token_to_canonical[token] = canonical_key

    only_pkgs = []
    seen: dict[str, bool] = {}
    for pkg in packages:
        canonical = token_to_canonical.get(pkg)
        if not canonical:
            available = ", ".join(dep.get_display_name() for dep in all_deps)
            logger.error(f"Package '{pkg}' not found in {APM_YML_FILENAME}")
            logger.progress(f"Available: {available}")
            sys.exit(1)
        if canonical not in seen:
            seen[canonical] = True
            only_pkgs.append(canonical)

    return only_pkgs


def _prepare_lockfile_baseline(project_root):
    """Migrate lockfile if needed and snapshot current SHAs.

    Returns (lockfile_path, old_lockfile, old_shas, had_baseline).
    """
    from ....deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed

    lockfile_path = get_lockfile_path(project_root)
    migrate_lockfile_if_needed(project_root)

    old_lockfile = LockFile.read(lockfile_path)
    had_baseline = old_lockfile is not None
    old_shas: dict = {}
    if old_lockfile:
        for key, dep in old_lockfile.dependencies.items():
            old_shas[key] = dep.resolved_commit

    return lockfile_path, old_lockfile, old_shas, had_baseline


def _compute_changed_packages(lockfile_path, old_shas):
    """Compare old vs new lockfile SHAs to identify changed packages.

    Returns list of tuples: (key, old_sha, new_sha, ref).
    """
    from ....deps.lockfile import LockFile

    new_lockfile = LockFile.read(lockfile_path)
    changed: list = []
    if new_lockfile:
        for key, dep in new_lockfile.dependencies.items():
            old_sha = old_shas.get(key)
            new_sha = dep.resolved_commit
            if old_sha and new_sha and old_sha != new_sha:
                changed.append((key, old_sha[:8], new_sha[:8], dep.resolved_ref or ""))

    return changed


def _emit_update_summary(changed, error_count, had_baseline, logger):
    """Emit summary of update operation."""
    if changed:
        pkg_noun = "package" if len(changed) == 1 else "packages"
        if error_count > 0:
            logger.warning(f"Updated {len(changed)} {pkg_noun} with {error_count} error(s).")
        else:
            logger.success(f"Updated {len(changed)} {pkg_noun}:")
        for key, old_sha, new_sha, ref in changed:
            ref_str = f" ({ref})" if ref else ""
            click.echo(f"  {key}{ref_str}: {old_sha} -> {new_sha}")
    elif error_count > 0:
        logger.error(f"Update failed with {error_count} error(s).")
    elif not had_baseline:
        logger.success("Update complete.")
    else:
        logger.success("All packages already at latest refs.")


def update(packages, **params):
    """Update APM dependencies to latest git refs.

    Re-resolves git references (branches/tags) to their current SHAs,
    downloads updated content, re-integrates primitives, and regenerates
    the lockfile.

    \b
    Examples:
        apm deps update                    # Update all packages
        apm deps update org/repo           # Update one package
        apm deps update org/a org/b        # Update specific packages
        apm deps update --verbose          # Show detailed progress
    """
    from ....core.auth import AuthResolver
    from ....core.command_logger import InstallLogger
    from ...install import _APM_IMPORT_ERROR, APM_DEPS_AVAILABLE

    verbose = params["verbose"]
    force = params["force"]
    target = params["target"]
    parallel_downloads = params["parallel_downloads"]
    global_ = params["global_"]
    legacy_skill_paths = params["legacy_skill_paths"]
    logger = InstallLogger(verbose=verbose, partial=bool(packages))

    if not APM_DEPS_AVAILABLE:
        logger.error("APM dependency system not available")
        if _APM_IMPORT_ERROR:
            logger.progress(f"Import error: {_APM_IMPORT_ERROR}")
        sys.exit(1)

    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    project_root = get_apm_dir(scope)
    apm_yml_path = project_root / APM_YML_FILENAME

    if not apm_yml_path.exists():
        scope_hint = "~/.apm/" if global_ else "current directory"
        logger.error(f"No {APM_YML_FILENAME} found in {scope_hint}")
        sys.exit(1)

    apm_package = _load_apm_package_for_update(apm_yml_path, logger)

    all_deps = apm_package.get_apm_dependencies() + apm_package.get_dev_apm_dependencies()
    if not all_deps:
        logger.progress("No APM dependencies defined in apm.yml")
        return

    # Validate and normalize requested packages to canonical dependency keys.
    # The install engine matches only_packages by DependencyReference identity
    # (e.g. "owner/repo"), so short names like "compliance-rules" must be
    # mapped to their canonical form before calling the engine.
    only_pkgs = _validate_packages(packages, all_deps, logger)

    # Migrate legacy lockfile first, then snapshot SHAs for before/after diff
    lockfile_path, old_lockfile, old_shas, had_baseline = _prepare_lockfile_baseline(project_root)

    auth_resolver = AuthResolver()

    noun = f"{len(packages)} package(s)" if packages else f"all {len(all_deps)} dependencies"
    legacy_skill_paths = _resolve_legacy_skill_paths(legacy_skill_paths)

    logger.start(f"Updating {noun}...")

    install_result = _run_dependency_update(
        _UpdateRunContext(
            apm_package=apm_package,
            logger=logger,
            auth_resolver=auth_resolver,
            verbose=verbose,
            only_pkgs=only_pkgs,
            force=force,
            parallel_downloads=parallel_downloads,
            target=target,
            scope=scope,
            legacy_skill_paths=legacy_skill_paths,
        )
    )

    # Show diagnostics if any
    if install_result.diagnostics and install_result.diagnostics.has_diagnostics:
        install_result.diagnostics.render_summary()

    # Compare old vs new lockfile SHAs to show what changed
    changed = _compute_changed_packages(lockfile_path, old_shas)

    error_count = 0
    if install_result.diagnostics:
        try:
            error_count = int(install_result.diagnostics.error_count)
        except (TypeError, ValueError):
            error_count = 0

    _emit_update_summary(changed, error_count, had_baseline, logger)
