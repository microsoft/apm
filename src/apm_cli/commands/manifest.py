"""``apm manifest`` command group: maintain the apm.yml manifest."""

from __future__ import annotations

import os
import sys
import traceback

import click

from ..core.command_logger import CommandLogger
from ._helpers import _is_interactive


def _running_in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no")


@click.group(help="Maintain the apm.yml manifest")
def manifest() -> None:
    """Maintain the apm.yml manifest."""


@manifest.command(help="Rewrite the deprecated 'target:' key in apm.yml to 'targets:'")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    help="Migrate the user manifest (~/.apm/apm.yml) instead of ./apm.yml",
)
@click.option("--dry-run", is_flag=True, help="Show the proposed changes without writing them")
@click.option("--yes", "-y", is_flag=True, help="Apply the changes without a confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def migrate(global_: bool, dry_run: bool, yes: bool, verbose: bool) -> None:
    """Rewrite the deprecated top-level 'target:' key to 'targets:'.

    The rewrite keeps comments and formatting, touches only the top-level
    key, and deploys exactly the same files and MCP configuration. Running
    it on an already migrated manifest changes nothing. Under CI=true it
    prints the proposed diff and exits 1 without writing.
    """
    import yaml

    from ..core.scope import InstallScope, get_manifest_path
    from ..models.manifest_target_migration import (
        ManifestMigrationError,
        plan_manifest_migration,
    )

    logger = CommandLogger("manifest-migrate", verbose=verbose, dry_run=dry_run)
    manifest_path = get_manifest_path(InstallScope.USER if global_ else InstallScope.PROJECT)
    label = manifest_path.as_posix() if global_ else "apm.yml"
    migrate_cmd = "apm manifest migrate --global" if global_ else "apm manifest migrate"

    if not manifest_path.is_file():
        logger.error(f"No manifest found at {manifest_path.as_posix()}", symbol="error")
        if not global_:
            logger.info(
                "Run 'apm init' to create one, or 'apm manifest migrate --global' "
                "for ~/.apm/apm.yml"
            )
        sys.exit(1)

    try:
        migration = plan_manifest_migration(manifest_path)
    except (ManifestMigrationError, ValueError, yaml.YAMLError, OSError) as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(1)
    except Exception as exc:
        logger.exception(f"Migration failed: {exc}")
        logger.verbose_detail(traceback.format_exc())
        sys.exit(1)

    compilation_hint = (
        f"{label} still sets 'compilation.target', which APM ignores; delete it by hand."
        if migration.ignored_compilation_target
        else None
    )
    if not migration.changed:
        logger.success(f"{label} has no 'target:' key -- nothing to migrate", symbol="check")
        if compilation_hint:
            logger.info(compilation_hint)
        return

    logger.preformatted(migration.diff(label))
    if migration.plan is not None and migration.plan.expanded_all:
        count = len(migration.plan.targets or ())
        logger.info(
            f"'target: all' expands to the {count} targets 'apm install' deploys today; "
            "remove any you do not use."
        )

    if _running_in_ci():
        logger.error(
            f"{label} still uses 'target:' (CI=true fails until it is migrated). "
            f"Run '{migrate_cmd}' locally and commit {label}.",
            symbol="error",
        )
        sys.exit(1)

    if dry_run:
        logger.dry_run_notice(f"No changes written. Re-run without --dry-run to update {label}.")
        return

    if not yes:
        if not _is_interactive():
            logger.error(
                "Cannot prompt for confirmation in a non-interactive shell. "
                f"Re-run '{migrate_cmd} --yes' to apply.",
                symbol="error",
            )
            sys.exit(1)
        if not click.confirm(f"Apply these changes to {label}?", default=False):
            logger.progress(f"Cancelled. {label} was not changed.", symbol="info")
            return

    try:
        migration.write()
    except OSError as exc:
        logger.error(f"Could not write {manifest_path.as_posix()}: {exc}", symbol="error")
        sys.exit(1)
    if migration.plan is not None and migration.plan.targets is None:
        logger.success(f"Removed the empty 'target:' key from {label}", symbol="check")
    else:
        logger.success(f"Migrated {label}: 'target:' is now 'targets:'", symbol="check")
    if compilation_hint:
        logger.info(compilation_hint)
    logger.info(f"Commit {label} to record the change.")
