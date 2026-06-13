"""Heavy-lifting for ``apm deps`` extracted to keep cli.py under 800 lines.

Patched globals on ``apm_cli.commands.deps.cli`` (APMPackage, Path,
_resolve_scope_deps) are accessed via a function-level late import so that
test monkey-patches on ``apm_cli.commands.deps.cli.*`` take effect normally.

No module-level import of ``cli`` here to avoid circular imports.
"""

import sys

import click

from ...constants import APM_YML_FILENAME

# ---------------------------------------------------------------------------
# _show_scope_deps
# ---------------------------------------------------------------------------


def _show_scope_deps(scope_label, apm_dir, logger, console, has_rich, insecure_only=False):
    """Display dependencies for a single scope (Project or Global)."""
    from apm_cli.commands.deps import cli as _cli  # route patched _resolve_scope_deps

    installed_packages, orphaned_packages = _cli._resolve_scope_deps(apm_dir, logger, insecure_only)

    if installed_packages is None:
        logger.progress(f"No APM dependencies installed ({scope_label} scope)")
        logger.verbose_detail("Run 'apm install' to install dependencies from apm.yml")
        return

    if not installed_packages:
        if insecure_only:
            logger.progress(f"No insecure APM dependencies installed ({scope_label} scope)")
        else:
            logger.progress(
                f"apm_modules/ directory exists but contains no valid packages ({scope_label} scope)"
            )
        return

    if has_rich:
        from rich.table import Table

        table = Table(
            title=(
                f" Insecure APM Dependencies ({scope_label})"
                if insecure_only
                else f" APM Dependencies ({scope_label})"
            ),
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Package", style="bold white")
        table.add_column("Version", style="yellow")
        table.add_column("Source", style="blue")
        if insecure_only:
            table.add_column("Origin", style="bold red")
        table.add_column("Prompts", style="magenta", justify="center")
        table.add_column("Instructions", style="green", justify="center")
        table.add_column("Agents", style="cyan", justify="center")
        table.add_column("Skills", style="yellow", justify="center")
        table.add_column("Hooks", style="red", justify="center")

        for pkg in installed_packages:
            p = pkg["primitives"]
            table.add_row(
                pkg["name"],
                pkg["version"],
                pkg["source"],
                *([pkg["insecure_via"]] if insecure_only else []),
                str(p.get("prompts", 0)) if p.get("prompts", 0) > 0 else "-",
                str(p.get("instructions", 0)) if p.get("instructions", 0) > 0 else "-",
                str(p.get("agents", 0)) if p.get("agents", 0) > 0 else "-",
                str(p.get("skills", 0)) if p.get("skills", 0) > 0 else "-",
                str(p.get("hooks", 0)) if p.get("hooks", 0) > 0 else "-",
            )

        console.print(table)

        if orphaned_packages:
            logger.warning(f"{len(orphaned_packages)} orphaned package(s) found (not in apm.yml):")
            for pkg in orphaned_packages:
                logger.warning(f"  - {pkg}")
            logger.info("Run 'apm prune' to remove orphaned packages")
    else:
        # Fallback text table
        if insecure_only:
            click.echo(f" Insecure APM Dependencies ({scope_label}):")
            click.echo(
                f"{'Package':<30} {'Version':<10} {'Source':<12} {'Origin':<18} "
                f"{'Prompts':>7} {'Instr':>7} {'Agents':>7} {'Skills':>7} {'Hooks':>7}"
            )
            click.echo("-" * 117)
        else:
            click.echo(f" APM Dependencies ({scope_label}):")
            click.echo(
                f"{'Package':<30} {'Version':<10} {'Source':<12} {'Prompts':>7} {'Instr':>7} {'Agents':>7} {'Skills':>7} {'Hooks':>7}"
            )
            click.echo("-" * 98)

        for pkg in installed_packages:
            p = pkg["primitives"]
            name = pkg["name"][:28]
            version = pkg["version"][:8]
            source = pkg["source"][:10]
            insecure_via = pkg["insecure_via"][:16]
            prompts = str(p.get("prompts", 0)) if p.get("prompts", 0) > 0 else "-"
            instructions = str(p.get("instructions", 0)) if p.get("instructions", 0) > 0 else "-"
            agents = str(p.get("agents", 0)) if p.get("agents", 0) > 0 else "-"
            skills = str(p.get("skills", 0)) if p.get("skills", 0) > 0 else "-"
            hooks = str(p.get("hooks", 0)) if p.get("hooks", 0) > 0 else "-"
            if insecure_only:
                click.echo(
                    f"{name:<30} {version:<10} {source:<12} {insecure_via:<18} "
                    f"{prompts:>7} {instructions:>7} {agents:>7} {skills:>7} {hooks:>7}"
                )
            else:
                click.echo(
                    f"{name:<30} {version:<10} {source:<12} {prompts:>7} {instructions:>7} {agents:>7} {skills:>7} {hooks:>7}"
                )

        if orphaned_packages:
            logger.warning(f"{len(orphaned_packages)} orphaned package(s) found (not in apm.yml):")
            for pkg in orphaned_packages:
                logger.warning(f"  - {pkg}")
            logger.info("Run 'apm prune' to remove orphaned packages")


# ---------------------------------------------------------------------------
# _update_impl  (body of the ``deps update`` Click command)
# ---------------------------------------------------------------------------


def _update_impl(packages, verbose, force, target, parallel_downloads, global_, legacy_skill_paths):
    """Implementation of ``apm deps update``.

    Kept in a separate function so the Click-decorated ``update`` wrapper in
    cli.py stays thin and patchable.  Patched names (APMPackage, Path) are
    accessed through the original cli module at call-time.
    """
    from apm_cli.commands.deps import cli as _cli  # route patched APMPackage / Path

    from ...core.auth import AuthResolver
    from ...core.command_logger import InstallLogger
    from ...utils.console import _rich_warning
    from ..install import (
        _APM_IMPORT_ERROR,
        APM_DEPS_AVAILABLE,
        _install_apm_dependencies,
    )

    _rich_warning(
        "'apm deps update' is deprecated; use 'apm update' instead. "
        "'apm update' now supports -g/--global, [PACKAGES]..., --force, and "
        "--parallel-downloads, plus an interactive plan, --dry-run, and --yes.",
        symbol="warning",
    )

    logger = InstallLogger(verbose=verbose, partial=bool(packages))

    if not APM_DEPS_AVAILABLE:
        logger.error("APM dependency system not available")
        if _APM_IMPORT_ERROR:
            logger.progress(f"Import error: {_APM_IMPORT_ERROR}")
        sys.exit(1)

    from ...core.scope import InstallScope, get_apm_dir

    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    project_root = get_apm_dir(scope)
    apm_yml_path = project_root / APM_YML_FILENAME

    if not apm_yml_path.exists():
        scope_hint = "~/.apm/" if global_ else "current directory"
        logger.error(f"No {APM_YML_FILENAME} found in {scope_hint}")
        sys.exit(1)

    try:
        apm_package = _cli.APMPackage.from_apm_yml(apm_yml_path)
    except Exception as e:
        logger.error(f"Failed to parse {APM_YML_FILENAME}: {e}")
        sys.exit(1)

    all_deps = apm_package.get_apm_dependencies() + apm_package.get_dev_apm_dependencies()
    if not all_deps:
        logger.progress("No APM dependencies defined in apm.yml")
        return

    from .._helpers import UnknownPackageError, resolve_requested_packages

    try:
        only_pkgs = resolve_requested_packages(packages, all_deps)
    except UnknownPackageError as e:
        logger.error(f"Package '{e.token}' not found in {APM_YML_FILENAME}")
        logger.progress(f"Available: {', '.join(e.available)}")
        sys.exit(1)

    from ...deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed

    lockfile_path = get_lockfile_path(project_root)
    migrate_lockfile_if_needed(project_root)

    old_lockfile = LockFile.read(lockfile_path)
    had_baseline = old_lockfile is not None
    old_shas: dict = {}
    if old_lockfile:
        for key, dep in old_lockfile.dependencies.items():
            old_shas[key] = dep.resolved_commit

    auth_resolver = AuthResolver()

    noun = f"{len(packages)} package(s)" if packages else f"all {len(all_deps)} dependencies"
    if not legacy_skill_paths:
        from ...integration.targets import should_use_legacy_skill_paths

        legacy_skill_paths = should_use_legacy_skill_paths()

    logger.start(f"Updating {noun}...")

    try:
        install_result = _install_apm_dependencies(
            apm_package,
            update_refs=True,
            verbose=verbose,
            only_packages=only_pkgs,
            force=force,
            parallel_downloads=parallel_downloads,
            logger=logger,
            auth_resolver=auth_resolver,
            target=target,
            scope=scope,
            legacy_skill_paths=legacy_skill_paths,
        )
    except Exception as e:
        logger.error(f"Update failed: {e}")
        if not verbose:
            logger.progress("Run with --verbose for detailed diagnostics")
        sys.exit(1)

    if install_result.diagnostics and install_result.diagnostics.has_diagnostics:
        install_result.diagnostics.render_summary()

    new_lockfile = LockFile.read(lockfile_path)
    changed: list = []
    if new_lockfile:
        for key, dep in new_lockfile.dependencies.items():
            old_sha = old_shas.get(key)
            new_sha = dep.resolved_commit
            if old_sha and new_sha and old_sha != new_sha:
                changed.append((key, old_sha[:8], new_sha[:8], dep.resolved_ref or ""))

    error_count = 0
    if install_result.diagnostics:
        try:
            error_count = int(install_result.diagnostics.error_count)
        except (TypeError, ValueError):
            error_count = 0

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
