"""Marketplace search command extracted from _consumer_cmds.py."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import MarketplaceNotFoundError
from .._helpers import _get_console
from . import marketplace


def _render_search_results(
    results: list, query: str, marketplace_name: str, console, logger: CommandLogger
) -> None:
    """Render search results to console (rich table) or plain text."""
    if not console:
        logger.success(f"Found {len(results)} plugin(s):", symbol="check")
        for p in results:
            desc = f" -- {p.description}" if p.description else ""
            logger.tree_item(f"  {p.name}@{marketplace_name}{desc}")
        logger.progress(
            f"Install: apm install <plugin-name>@{marketplace_name}",
            symbol="info",
        )
        return

    from rich.table import Table

    table = Table(
        title=f"Search Results: '{query}' in {marketplace_name}",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Plugin", style="bold white", no_wrap=True)
    table.add_column("Description", style="white", ratio=1)
    table.add_column("Install", style="green")

    for p in results:
        desc = p.description or "--"
        if len(desc) > 60:
            desc = desc[:57] + "..."
        table.add_row(p.name, desc, f"{p.name}@{marketplace_name}")

    console.print()
    console.print(table)
    logger.progress(
        f"Install: apm install <plugin-name>@{marketplace_name}",
        symbol="info",
    )


@marketplace.command(
    name="search",
    help="Search plugins in a marketplace (QUERY@MARKETPLACE)",
)
@click.argument("expression", required=True, metavar="QUERY@MARKETPLACE")
@click.option("--limit", default=20, show_default=True, help="Max results to show")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def search(expression, limit, verbose):
    """Search for plugins in a specific marketplace.

    Use QUERY@MARKETPLACE format, e.g.:  apm marketplace search security@skills
    """
    logger = CommandLogger("marketplace-search", verbose=verbose)
    try:
        from ...marketplace.client import search_marketplace
        from ...marketplace.registry import get_marketplace_by_name

        if "@" not in expression:
            logger.error(
                f"Invalid format: '{expression}'. "
                "Use QUERY@MARKETPLACE, e.g.: apm marketplace search security@skills"
            )
            sys.exit(1)

        query, marketplace_name = expression.rsplit("@", 1)
        if not query or not marketplace_name:
            logger.error(
                "Both QUERY and MARKETPLACE are required. "
                "Use QUERY@MARKETPLACE, e.g.: apm marketplace search security@skills"
            )
            sys.exit(1)

        try:
            source = get_marketplace_by_name(marketplace_name)
        except MarketplaceNotFoundError:
            logger.error(
                f"Marketplace '{marketplace_name}' is not registered. "
                "Use 'apm marketplace list' to see registered marketplaces."
            )
            sys.exit(1)

        logger.start(f"Searching '{marketplace_name}' for '{query}'...", symbol="search")
        results = search_marketplace(query, source)[:limit]

        if not results:
            logger.warning(
                f"No plugins found matching '{query}' in '{marketplace_name}'. "
                f"Try 'apm marketplace browse {marketplace_name}' to see all plugins."
            )
            return

        console = _get_console()
        _render_search_results(results, query, marketplace_name, console, logger)

    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"Search failed: {e}")
        logger.verbose_detail(traceback.format_exc())
        sys.exit(1)
