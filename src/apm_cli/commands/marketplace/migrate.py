"""``apm marketplace migrate`` command."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import MarketplaceYmlError
from ...marketplace.migration import migrate_marketplace_yml
from . import marketplace


@marketplace.command(help="Fold marketplace.yml into apm.yml's 'marketplace:' block")
@click.option(
    "--force",
    "--yes",
    "-y",
    "force",
    is_flag=True,
    help="Overwrite an existing 'marketplace:' block in apm.yml (alias: --yes/-y)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show the proposed apm.yml changes without writing them",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def migrate(force, dry_run, verbose):
    """Convert legacy marketplace.yml to an apm.yml marketplace block."""
    logger = CommandLogger("marketplace-migrate", verbose=verbose)
    project_root = Path.cwd()

    try:
        diff = migrate_marketplace_yml(project_root, force=force, dry_run=dry_run)
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Migration failed: {exc}", symbol="error")
        logger.verbose_detail(traceback.format_exc())
        sys.exit(1)

    if dry_run:
        logger.progress(
            "Dry run -- the following changes would be applied to apm.yml:",
            symbol="info",
        )
        click.echo(diff if diff else "(no changes)")
        return

    logger.success(
        "Migrated marketplace.yml into apm.yml's 'marketplace:' block",
        symbol="check",
    )
    logger.progress(
        "marketplace.yml has been removed. Commit apm.yml to record the migration.",
        symbol="info",
    )
