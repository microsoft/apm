"""Consumer-facing marketplace commands: add / list / browse / update / remove / search."""

from __future__ import annotations

import sys
import traceback

import click

from ...core.command_logger import CommandLogger
from ...utils.path_security import PathTraversalError
from .._helpers import _get_console, _is_interactive
from . import marketplace  # noqa: E402
from ._add_helpers import (
    _check_trusted_host,
    _is_valid_alias,
    _parse_marketplace_repo,
    _resolve_display_name_for_add,
    _resolve_host_for_add,
)


@marketplace.command(help="Register a marketplace")
@click.argument("repo", required=True)
@click.option("--name", "-n", default=None, help="Display name (defaults to repo name)")
@click.option("--branch", "-b", default="main", show_default=True, help="Branch to use")
@click.option("--host", default=None, help="Git host FQDN (default: github.com)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(repo, name, branch, host, verbose):
    """Register a marketplace from OWNER/REPO, HOST/OWNER/.../REPO, or an HTTPS URL."""
    logger = CommandLogger("marketplace-add", verbose=verbose)
    try:
        from ...marketplace.client import _auto_detect_path, fetch_marketplace
        from ...marketplace.models import MarketplaceSource
        from ...marketplace.registry import add_marketplace

        try:
            owner, repo_name, embedded_host = _parse_marketplace_repo(repo, host)
        except PathTraversalError:
            logger.error(
                f"Invalid repo path '{repo}': contains a path-traversal sequence. "
                f"Remove '..', '.', or '~' from each path segment."
            )
            sys.exit(1)
        except ValueError as exc:
            logger.error(str(exc))
            sys.exit(1)

        # Resolve the effective host: explicit --host wins, then host embedded
        # in the argument (HOST/... shorthand or HTTPS URL), then GITHUB_HOST.
        resolved_host = _resolve_host_for_add(host, embedded_host, logger)
        # Trusted-host gate.
        _check_trusted_host(resolved_host, repo, logger)

        # Hard-fail if the user-supplied --name flag is malformed; the
        # manifest's name is validated softly below (publisher mistakes
        # shouldn't break a successful add).
        if name is not None and not _is_valid_alias(name):
            logger.error(
                f"Invalid marketplace name: '{name}'. "
                f"Names must only contain letters, digits, '.', '_', and '-' "
                f"(required for 'apm install plugin@marketplace' syntax).",
                symbol="error",
            )
            sys.exit(1)

        # Probe for the marketplace.json location. The probe source's name
        # is a placeholder -- _auto_detect_path only consults host/owner/repo.
        probe_source = MarketplaceSource(
            name=name or repo_name,
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
                f".claude-plugin/marketplace.json",
                symbol="error",
            )
            sys.exit(1)

        # Fetch and validate the manifest before logging start, so that the
        # success/start lines display the *final* alias the user must use.
        fetch_source = MarketplaceSource(
            name=name or repo_name,
            owner=owner,
            repo=repo_name,
            branch=branch,
            host=resolved_host,
            path=detected_path,
        )
        manifest = fetch_marketplace(fetch_source, force_refresh=True)
        plugin_count = len(manifest.plugins)

        # Resolve final alias: --name flag > manifest.name (if valid) > repo name.
        manifest_name = (manifest.name or "").strip()
        display_name, alias_source = _resolve_display_name_for_add(
            name, manifest_name, repo_name, logger
        )

        # Defense-in-depth: repo names from GitHub already satisfy the alias
        # regex, so this invariant should always hold by the time we register.
        assert _is_valid_alias(display_name), (  # noqa: S101
            f"Resolved marketplace alias '{display_name}' failed validation"
        )

        logger.start(f"Registering marketplace '{display_name}'...", symbol="gear")
        logger.verbose_detail(f"    Repository: {owner}/{repo_name}")
        logger.verbose_detail(f"    Branch: {branch}")
        if resolved_host != "github.com":
            logger.verbose_detail(f"    Host: {resolved_host}")
        logger.verbose_detail(f"    Detected path: {detected_path}")
        logger.verbose_detail(f"    Alias source: {alias_source}")

        # Persist with the final alias.
        source = MarketplaceSource(
            name=display_name,
            owner=owner,
            repo=repo_name,
            branch=branch,
            host=resolved_host,
            path=detected_path,
        )
        add_marketplace(source)

        logger.success(
            f"Marketplace '{display_name}' registered ({plugin_count} plugins)",
            symbol="check",
        )
        if manifest.description:
            logger.verbose_detail(f"    {manifest.description}")

        # Surface the install syntax only when the alias is something the user
        # could not have predicted from OWNER/REPO. Silence is fine otherwise.
        if name is None and display_name != repo_name:
            logger.progress(
                f"Install plugins with: apm install <plugin>@{display_name}",
                symbol="info",
            )

    except Exception as e:
        logger.error(f"Failed to register marketplace: {e}")
        if verbose:
            logger.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)


@marketplace.command(name="list", help="List registered marketplaces")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def list_cmd(verbose):
    """Show all registered marketplaces."""
    logger = CommandLogger("marketplace-list", verbose=verbose)
    try:
        from ...marketplace.registry import get_registered_marketplaces

        sources = get_registered_marketplaces()

        if not sources:
            logger.progress(
                "No marketplaces registered. Use 'apm marketplace add OWNER/REPO' to register one.",
                symbol="info",
            )
            return

        console = _get_console()
        if not console:
            # Colorama fallback
            logger.progress(f"{len(sources)} marketplace(s) registered:", symbol="info")
            for s in sources:
                logger.tree_item(f"  {s.name}  ({s.owner}/{s.repo})")
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
        logger.progress(
            "Use 'apm marketplace browse <name>' to see plugins",
            symbol="info",
        )

    except Exception as e:
        logger.error(f"Failed to list marketplaces: {e}")
        if verbose:
            logger.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)


@marketplace.command(help="Browse plugins in a marketplace")
@click.argument("name", required=True)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def browse(name, verbose):
    """Show available plugins in a marketplace."""
    logger = CommandLogger("marketplace-browse", verbose=verbose)
    try:
        from ...marketplace.client import fetch_marketplace
        from ...marketplace.registry import get_marketplace_by_name

        source = get_marketplace_by_name(name)
        logger.start(f"Fetching plugins from '{name}'...", symbol="search")

        manifest = fetch_marketplace(source, force_refresh=True)

        if not manifest.plugins:
            logger.warning(f"Marketplace '{name}' has no plugins")
            return

        console = _get_console()
        if not console:
            # Colorama fallback
            logger.success(f"{len(manifest.plugins)} plugin(s) in '{name}':", symbol="check")
            for p in manifest.plugins:
                desc = f" -- {p.description}" if p.description else ""
                logger.tree_item(f"  {p.name}{desc}")
            logger.progress(f"Install: apm install <plugin-name>@{name}", symbol="info")
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
        logger.progress(
            f"Install a plugin: apm install <plugin-name>@{name}",
            symbol="info",
        )

    except Exception as e:
        logger.error(f"Failed to browse marketplace: {e}")
        if verbose:
            logger.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)


@marketplace.command(help="Refresh marketplace cache")
@click.argument("name", required=False, default=None)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def update(name, verbose):
    """Refresh cached marketplace data (one or all)."""
    logger = CommandLogger("marketplace-update", verbose=verbose)
    try:
        from ...marketplace.client import clear_marketplace_cache, fetch_marketplace
        from ...marketplace.registry import (
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
                logger.progress("No marketplaces registered.", symbol="info")
                return
            logger.start(f"Refreshing {len(sources)} marketplace(s)...", symbol="gear")
            for s in sources:
                try:
                    clear_marketplace_cache(s.name, host=s.host)
                    manifest = fetch_marketplace(s, force_refresh=True)
                    logger.tree_item(f"  {s.name} ({len(manifest.plugins)} plugins)")
                except Exception as exc:
                    logger.warning(f"  {s.name}: {exc}")
                    if verbose:
                        logger.progress(traceback.format_exc(), symbol="info")
            logger.success("Marketplace cache refreshed", symbol="check")

    except Exception as e:
        logger.error(f"Failed to update marketplace: {e}")
        if verbose:
            logger.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)


@marketplace.command(help="Remove a registered marketplace")
@click.argument("name", required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(name, yes, verbose):
    """Unregister a marketplace."""
    logger = CommandLogger("marketplace-remove", verbose=verbose)
    try:
        from ...marketplace.client import clear_marketplace_cache
        from ...marketplace.registry import get_marketplace_by_name, remove_marketplace

        # Verify it exists first
        source = get_marketplace_by_name(name)

        if not yes:
            if not _is_interactive():
                logger.error(
                    "Use --yes to skip confirmation in non-interactive mode",
                    symbol="error",
                )
                sys.exit(1)
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
        if verbose:
            logger.progress(traceback.format_exc(), symbol="info")
        sys.exit(1)
