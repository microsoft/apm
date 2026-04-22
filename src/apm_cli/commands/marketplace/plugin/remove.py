"""``apm marketplace plugin remove`` command."""

from __future__ import annotations

import sys

import click

from ....core.command_logger import CommandLogger
from ....marketplace.errors import MarketplaceYmlError
from . import _ensure_yml_exists, plugin


@plugin.command(help="Remove a plugin from marketplace.yml")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(name: str, yes: bool, verbose: bool) -> None:
    """Remove a plugin entry from marketplace.yml."""
    from ....marketplace.yml_editor import remove_plugin_entry

    logger = CommandLogger("marketplace-plugin-remove", verbose=verbose)
    yml = _ensure_yml_exists(logger)

    if not yes:
        try:
            click.confirm(
                f"Remove plugin '{name}' from marketplace.yml?",
                abort=True,
            )
        except click.Abort:
            click.echo("Cancelled.")
            return

    try:
        remove_plugin_entry(yml, name)
    except MarketplaceYmlError as exc:
        logger.error(str(exc), symbol="error")
        sys.exit(2)

    logger.success(f"Removed plugin '{name}'", symbol="check")
