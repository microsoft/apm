"""APM marketplace command group.

Manages plugin marketplace discovery and governance. Follows the same
Click group pattern as ``mcp.py``.
"""

import builtins
import sys

import click

from ..core.command_logger import CommandLogger
from ._helpers import _get_console

# Restore builtins shadowed by subcommand names
list = builtins.list


@click.group(help="Manage plugin marketplaces for discovery and governance")
def marketplace():
    """Register, browse, and search plugin marketplaces."""
    pass


# ---------------------------------------------------------------------------
# marketplace add
# ---------------------------------------------------------------------------


@marketplace.command(help="Register a plugin marketplace")
@click.argument("repo", required=True)
@click.option("--name", "-n", default=None, help="Display name (defaults to repo name)")
@click.option("--branch", "-b", default="main", show_default=True, help="Branch to use")
@click.option("--host", default=None, help="Git host FQDN (default: github.com)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(repo, name, branch, host, verbose):
    """Register a marketplace from OWNER/REPO or HOST/OWNER/REPO."""
    logger = CommandLogger("marketplace-add", verbose=verbose)
    try:
        from ..marketplace.client import _auto_detect_path, fetch_marketplace
        from ..marketplace.models import MarketplaceSource
        from ..marketplace.registry import add_marketplace

        # Parse OWNER/REPO or HOST/OWNER/REPO
        if "/" not in repo:
            logger.error(
                f"Invalid format: '{repo}'. Use 'OWNER/REPO' "
                f"(e.g., 'acme-org/plugin-marketplace')"
            )
            sys.exit(1)

        from ..utils.github_host import default_host, is_valid_fqdn

        parts = repo.split("/")
        if len(parts) == 3 and parts[0] and parts[1] and parts[2]:
            if not is_valid_fqdn(parts[0]):
                logger.error(
                    f"Invalid host: '{parts[0]}'. "
                    f"Use 'OWNER/REPO' or 'HOST/OWNER/REPO' format."
                )
                sys.exit(1)
            if host and host != parts[0]:
                logger.error(
                    f"Conflicting host: --host '{host}' vs '{parts[0]}' in argument."
                )
                sys.exit(1)
            host = parts[0]
            owner, repo_name = parts[1], parts[2]
        elif len(parts) == 2 and parts[0] and parts[1]:
            owner, repo_name = parts[0], parts[1]
        else:
            logger.error(f"Invalid format: '{repo}'. Expected 'OWNER/REPO'")
            sys.exit(1)

        if host is not None:
            normalized_host = host.strip().lower()
            if not is_valid_fqdn(normalized_host):
                logger.error(
                    f"Invalid host: '{host}'. Expected a valid host FQDN "
                    f"(for example, 'github.com')."
                )
                sys.exit(1)
            resolved_host = normalized_host
        else:
            resolved_host = default_host()
        display_name = name or repo_name

        # Validate name is identifier-compatible for NAME@MARKETPLACE syntax
        import re

        if not re.match(r"^[a-zA-Z0-9._-]+$", display_name):
            logger.error(
                f"Invalid marketplace name: '{display_name}'. "
                f"Names must only contain letters, digits, '.', '_', and '-' "
                f"(required for 'apm install plugin@marketplace' syntax)."
            )
            sys.exit(1)

        logger.start(f"Registering marketplace '{display_name}'...", symbol="gear")
        logger.verbose_detail(f"    Repository: {owner}/{repo_name}")
        logger.verbose_detail(f"    Branch: {branch}")
        if resolved_host != "github.com":
            logger.verbose_detail(f"    Host: {resolved_host}")

        # Auto-detect marketplace.json location
        probe_source = MarketplaceSource(
            name=display_name,
            owner=owner,
            repo=repo_name,
            branch=branch,
            host=resolved_host,
        )
        detected_path = _auto_detect_path(probe_source)

        if detected_path is None:
            logger.error(
                f"No marketplace.json found in '{owner}/{repo_name}'. "
                f"Checked: marketplace.json, .github/plugin/marketplace.json, "
                f".claude-plugin/marketplace.json"
            )
            sys.exit(1)

        logger.verbose_detail(f"    Detected path: {detected_path}")

        # Create source with detected path
        source = MarketplaceSource(
            name=display_name,
            owner=owner,
            repo=repo_name,
            branch=branch,
            host=resolved_host,
            path=detected_path,
        )

        # Fetch and validate
        manifest = fetch_marketplace(source, force_refresh=True)
        plugin_count = len(manifest.plugins)

        # Register
        add_marketplace(source)

        logger.success(
            f"Marketplace '{display_name}' registered ({plugin_count} plugins)",
            symbol="check",
        )
        if manifest.description:
            logger.verbose_detail(f"    {manifest.description}")

    except Exception as e:
        logger.error(f"Failed to register marketplace: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# marketplace list
# ---------------------------------------------------------------------------


@marketplace.command(name="list", help="List registered marketplaces")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def list_cmd(verbose):
    """Show all registered marketplaces."""
    logger = CommandLogger("marketplace-list", verbose=verbose)
    try:
        from ..marketplace.registry import get_registered_marketplaces

        sources = get_registered_marketplaces()

        if not sources:
            logger.progress(
                "No marketplaces registered. "
                "Use 'apm marketplace add OWNER/REPO' to register one.",
                symbol="info",
            )
            return

        console = _get_console()
        if not console:
            # Colorama fallback
            logger.progress(
                f"{len(sources)} marketplace(s) registered:", symbol="info"
            )
            for s in sources:
                click.echo(f"  {s.name}  ({s.owner}/{s.repo})")
            return

        from rich.table import Table

        table = Table(
            title="Registered Marketplaces",
            show_header=True,
            header_style="bold cyan",
            border_style="cyan",
        )
        table.add_column("Name", style="bold white", no_wrap=True)
        table.add_column("Repository", style="white")
        table.add_column("Branch", style="cyan")
        table.add_column("Path", style="dim")

        for s in sources:
            table.add_row(s.name, f"{s.owner}/{s.repo}", s.branch, s.path)

        console.print()
        console.print(table)
        console.print(
            f"\n[dim]Use 'apm marketplace browse <name>' to see plugins[/dim]"
        )

    except Exception as e:
        logger.error(f"Failed to list marketplaces: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# marketplace browse
# ---------------------------------------------------------------------------


@marketplace.command(help="Browse plugins in a marketplace")
@click.argument("name", required=True)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def browse(name, verbose):
    """Show available plugins in a marketplace."""
    logger = CommandLogger("marketplace-browse", verbose=verbose)
    try:
        from ..marketplace.client import fetch_marketplace
        from ..marketplace.registry import get_marketplace_by_name

        source = get_marketplace_by_name(name)
        logger.start(f"Fetching plugins from '{name}'...", symbol="search")

        manifest = fetch_marketplace(source, force_refresh=True)

        if not manifest.plugins:
            logger.warning(f"Marketplace '{name}' has no plugins")
            return

        console = _get_console()
        if not console:
            # Colorama fallback
            logger.success(
                f"{len(manifest.plugins)} plugin(s) in '{name}':", symbol="check"
            )
            for p in manifest.plugins:
                desc = f" -- {p.description}" if p.description else ""
                click.echo(f"  {p.name}{desc}")
            click.echo(
                f"\n  Install: apm install <plugin-name>@{name}"
            )
            return

        from rich.table import Table

        table = Table(
            title=f"Plugins in '{name}'",
            show_header=True,
            header_style="bold cyan",
            border_style="cyan",
        )
        table.add_column("Plugin", style="bold white", no_wrap=True)
        table.add_column("Description", style="white", ratio=1)
        table.add_column("Version", style="cyan", justify="center")
        table.add_column("Install", style="green")

        for p in manifest.plugins:
            desc = p.description or "--"
            ver = p.version or "--"
            table.add_row(p.name, desc, ver, f"{p.name}@{name}")

        console.print()
        console.print(table)
        console.print(
            f"\n[dim]Install a plugin: apm install <plugin-name>@{name}[/dim]"
        )

    except Exception as e:
        logger.error(f"Failed to browse marketplace: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# marketplace update
# ---------------------------------------------------------------------------


@marketplace.command(help="Refresh marketplace cache")
@click.argument("name", required=False, default=None)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def update(name, verbose):
    """Refresh cached marketplace data (one or all)."""
    logger = CommandLogger("marketplace-update", verbose=verbose)
    try:
        from ..marketplace.client import clear_marketplace_cache, fetch_marketplace
        from ..marketplace.registry import (
            get_marketplace_by_name,
            get_registered_marketplaces,
        )

        if name:
            source = get_marketplace_by_name(name)
            logger.start(f"Refreshing marketplace '{name}'...", symbol="gear")
            clear_marketplace_cache(name, host=source.host)
            manifest = fetch_marketplace(source, force_refresh=True)
            logger.success(
                f"Marketplace '{name}' updated ({len(manifest.plugins)} plugins)",
                symbol="check",
            )
        else:
            sources = get_registered_marketplaces()
            if not sources:
                logger.progress(
                    "No marketplaces registered.", symbol="info"
                )
                return
            logger.start(
                f"Refreshing {len(sources)} marketplace(s)...", symbol="gear"
            )
            for s in sources:
                try:
                    clear_marketplace_cache(s.name, host=s.host)
                    manifest = fetch_marketplace(s, force_refresh=True)
                    logger.tree_item(
                        f"  {s.name} ({len(manifest.plugins)} plugins)"
                    )
                except Exception as exc:
                    logger.warning(f"  {s.name}: {exc}")
            logger.success("Marketplace cache refreshed", symbol="check")

    except Exception as e:
        logger.error(f"Failed to update marketplace: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# marketplace remove
# ---------------------------------------------------------------------------


@marketplace.command(help="Remove a registered marketplace")
@click.argument("name", required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(name, yes, verbose):
    """Unregister a marketplace."""
    logger = CommandLogger("marketplace-remove", verbose=verbose)
    try:
        from ..marketplace.client import clear_marketplace_cache
        from ..marketplace.registry import get_marketplace_by_name, remove_marketplace

        # Verify it exists first
        source = get_marketplace_by_name(name)

        if not yes:
            confirmed = click.confirm(
                f"Remove marketplace '{source.name}' ({source.owner}/{source.repo})?",
                default=False,
            )
            if not confirmed:
                logger.progress("Cancelled", symbol="info")
                return

        remove_marketplace(name)
        clear_marketplace_cache(name, host=source.host)
        logger.success(f"Marketplace '{name}' removed", symbol="check")

    except Exception as e:
        logger.error(f"Failed to remove marketplace: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# marketplace validate
# ---------------------------------------------------------------------------


@marketplace.command(help="Validate a marketplace manifest")
@click.argument("name", required=True)
@click.option(
    "--check-refs", is_flag=True, help="Verify version refs are reachable (network)"
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def validate(name, check_refs, verbose):
    """Validate the manifest of a registered marketplace."""
    logger = CommandLogger("marketplace-validate", verbose=verbose)
    try:
        from ..marketplace.client import fetch_marketplace
        from ..marketplace.registry import get_marketplace_by_name
        from ..marketplace.validator import validate_marketplace

        source = get_marketplace_by_name(name)
        logger.start(f"Validating marketplace '{name}'...", symbol="gear")

        manifest = fetch_marketplace(source, force_refresh=True)

        logger.progress(
            f"Found {len(manifest.plugins)} plugins",
            symbol="info",
        )

        # Verbose: per-plugin details
        if verbose:
            for p in manifest.plugins:
                source_type = "dict" if isinstance(p.source, dict) else "string"
                logger.verbose_detail(
                    f"    {p.name}: source type: {source_type}"
                )

        # Run validation
        results = validate_marketplace(manifest)

        # Check-refs placeholder
        if check_refs:
            logger.warning(
                "Ref checking not yet implemented -- skipping ref "
                "reachability checks",
                symbol="warning",
            )

        # Render results
        passed = 0
        warning_count = 0
        error_count = 0
        click.echo()
        click.echo("Validation Results:")
        for r in results:
            if r.passed and not r.warnings:
                logger.success(
                    f"  {r.check_name}: all plugins valid", symbol="check"
                )
                passed += 1
            elif r.warnings and not r.errors:
                for w in r.warnings:
                    logger.warning(f"  {r.check_name}: {w}", symbol="warning")
                warning_count += len(r.warnings)
            else:
                for e in r.errors:
                    logger.error(f"  {r.check_name}: {e}", symbol="error")
                for w in r.warnings:
                    logger.warning(f"  {r.check_name}: {w}", symbol="warning")
                error_count += len(r.errors)
                warning_count += len(r.warnings)

        click.echo()
        click.echo(
            f"Summary: {passed} passed, {warning_count} warnings, "
            f"{error_count} errors"
        )

        if error_count > 0:
            sys.exit(1)

    except Exception as e:
        logger.error(f"Failed to validate marketplace: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Top-level search command (registered separately in cli.py)
# ---------------------------------------------------------------------------


@click.command(
    name="search",
    help="Search plugins in a marketplace (QUERY@MARKETPLACE)",
)
@click.argument("expression", required=True)
@click.option("--limit", default=20, show_default=True, help="Max results to show")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def search(expression, limit, verbose):
    """Search for plugins in a specific marketplace.

    Use QUERY@MARKETPLACE format, e.g.:  apm marketplace search security@skills
    """
    logger = CommandLogger("marketplace-search", verbose=verbose)
    try:
        from ..marketplace.client import search_marketplace
        from ..marketplace.registry import get_marketplace_by_name

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
        except Exception:
            logger.error(
                f"Marketplace '{marketplace_name}' is not registered. "
                "Use 'apm marketplace list' to see registered marketplaces."
            )
            sys.exit(1)

        logger.start(
            f"Searching '{marketplace_name}' for '{query}'...", symbol="search"
        )
        results = search_marketplace(query, source)[:limit]

        if not results:
            logger.warning(
                f"No plugins found matching '{query}' in '{marketplace_name}'. "
                f"Try 'apm marketplace browse {marketplace_name}' to see all plugins."
            )
            return

        console = _get_console()
        if not console:
            # Colorama fallback
            logger.success(f"Found {len(results)} plugin(s):", symbol="check")
            for p in results:
                desc = f" -- {p.description}" if p.description else ""
                click.echo(f"  {p.name}@{marketplace_name}{desc}")
            click.echo(
                f"\n  Install: apm install <plugin-name>@{marketplace_name}"
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
        console.print(
            f"\n[dim]Install: apm install <plugin-name>@{marketplace_name}[/dim]"
        )

    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"Search failed: {e}")
        sys.exit(1)

