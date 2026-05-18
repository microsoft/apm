"""APM uninstall command CLI."""

from __future__ import annotations

import sys

import click

from ...constants import APM_YML_FILENAME
from ...core.command_logger import CommandLogger
from .engine import (
    _cleanup_transitive_orphans,
    _dry_run_uninstall,
    _remove_packages_from_disk,
    _validate_uninstall_packages,
)
from .flow import (
    _cleanup_mcp_state,
    _collect_deployed_files,
    _load_lockfile_state,
    _load_manifest_data,
    _log_cleanup_counts,
    _ManifestUpdateContext,
    _McpCleanupContext,
    _summarise_uninstall,
    _sync_integrations,
    _update_lockfile_after_uninstall,
    _update_manifest_dependencies,
)


@click.command(help="Remove APM packages, their integrated files, and apm.yml entries")
@click.argument("packages", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed removal information")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Remove from user scope (~/.apm/) instead of the current project",
)
@click.pass_context
def uninstall(ctx, packages, dry_run, verbose, global_):
    """Remove APM packages from apm.yml and apm_modules (like npm uninstall)."""
    from ...core.scope import (
        InstallScope,
        get_apm_dir,
        get_deploy_root,
        get_manifest_path,
        get_modules_dir,
    )

    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    manifest_path = get_manifest_path(scope)
    apm_dir = get_apm_dir(scope)
    deploy_root = get_deploy_root(scope)
    manifest_display = str(manifest_path) if scope is InstallScope.USER else APM_YML_FILENAME
    logger = CommandLogger("uninstall", verbose=verbose, dry_run=dry_run)
    try:
        if not manifest_path.exists():
            if scope is InstallScope.USER:
                logger.error(
                    f"No user manifest found at {manifest_display}. Install a package globally "
                    "first with 'apm install -g <package>' or create the file manually."
                )
            else:
                logger.error(f"No {manifest_display} found. Run 'apm init' in this project first.")
            sys.exit(1)
        if not packages:
            logger.error("No packages specified. Specify packages to uninstall.")
            sys.exit(1)
        if scope is InstallScope.USER:
            logger.progress("Uninstalling from user scope (~/.apm/)")
        logger.start(f"Uninstalling {len(packages)} package(s)...")

        apm_yml_path = manifest_path
        data, dump_yaml = _load_manifest_data(apm_yml_path, logger)
        current_deps = data["dependencies"]["apm"] or []
        packages_to_remove, packages_not_found = _validate_uninstall_packages(
            packages,
            current_deps,
            logger,
        )
        if not packages_to_remove:
            logger.warning("No packages found in apm.yml to remove")
            return

        modules_dir = get_modules_dir(scope)
        if dry_run:
            _dry_run_uninstall(packages_to_remove, modules_dir, logger)
            return

        _update_manifest_dependencies(
            data,
            current_deps,
            packages_to_remove,
            _ManifestUpdateContext(
                apm_yml_path=apm_yml_path,
                dump_yaml=dump_yaml,
                logger=logger,
            ),
        )
        lockfile_path, lockfile, pre_uninstall_mcp_servers = _load_lockfile_state(apm_dir)
        removed_from_modules = _remove_packages_from_disk(packages_to_remove, modules_dir, logger)
        orphan_removed, actual_orphans = _cleanup_transitive_orphans(
            lockfile,
            packages_to_remove,
            modules_dir,
            apm_yml_path,
            logger,
        )
        removed_from_modules += orphan_removed
        all_deployed_files = _collect_deployed_files(lockfile, packages_to_remove, actual_orphans)
        _update_lockfile_after_uninstall(
            lockfile,
            lockfile_path,
            packages_to_remove,
            actual_orphans,
            logger,
        )
        cleaned = _sync_integrations(
            manifest_path,
            deploy_root,
            all_deployed_files,
            logger,
            user_scope=scope is InstallScope.USER,
        )
        _log_cleanup_counts(cleaned, logger)
        _cleanup_mcp_state(
            manifest_path,
            _McpCleanupContext(
                lockfile=lockfile,
                lockfile_path=lockfile_path,
                pre_uninstall_mcp_servers=pre_uninstall_mcp_servers,
                modules_dir=modules_dir,
                deploy_root=deploy_root,
                scope=scope,
                logger=logger,
            ),
        )
        _summarise_uninstall(packages_to_remove, removed_from_modules, packages_not_found, logger)
    except Exception as e:
        logger.error(f"Error uninstalling packages: {e}")
        sys.exit(1)
