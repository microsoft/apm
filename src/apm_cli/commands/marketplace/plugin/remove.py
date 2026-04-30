"""``apm marketplace package remove`` command."""

from __future__ import annotations

import sys

import click

from ....core.command_logger import CommandLogger
from ....marketplace.errors import MarketplaceYmlError
from . import (
    _ensure_yml_exists,
    _is_interactive,
    package,
)


@package.command(help="Remove a package from marketplace authoring config")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(name, yes, verbose):
    """Remove a package entry from marketplace.yml."""
    from ....marketplace.yml_editor import remove_plugin_entry

    logger = CommandLogger("marketplace-package-remove", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    # Confirmation gate.
    if not yes:
        if not _is_interactive():
            logger.error(
                "Use --yes to skip confirmation in non-interactive mode",
                symbol="error",
            )
            sys.exit(1)
        try:
            click.confirm(
                f"Remove package '{name}' from marketplace authoring config?",
                abort=True,
            )
        except click.Abort:
            logger.progress("Cancelled.", symbol="info")
            return

    try:
        remove_plugin_entry(yml, name)
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(f"Removed package '{name}'", symbol="check")
