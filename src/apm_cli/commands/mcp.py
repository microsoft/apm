"""APM mcp command group."""

from __future__ import annotations

import os
import sys

import click

from ..core.command_logger import CommandLogger
from ._helpers import _get_console
from ._mcp_show import (
    _collect_deployment_types,
    _get_server_version,
    _render_install_table,
    _render_packages_table,
    _render_remotes_table,
)

MCP_REGISTRY_ENV = "MCP_REGISTRY_URL"


def _truncate_registry_description(description: str, limit: int = 80) -> str:
    if len(description) <= limit:
        return description
    truncate_pos = limit - 3
    if " " in description[70:85]:
        space_pos = description.rfind(" ", 70, 85)
        if space_pos > 70:
            truncate_pos = space_pos
    return description[:truncate_pos] + "..."


def _render_plain_server_summaries(servers, logger, prefix_message: str) -> None:
    logger.progress(prefix_message, symbol="search")
    if not servers:
        logger.warning("No servers found")
        return
    for server in servers:
        click.echo(f"  {server.get('name', 'Unknown')}")
        click.echo(
            f"    {_truncate_registry_description(server.get('description', 'No description'))}"
        )


def _build_registry_results_table(servers, title: str):
    from rich.table import Table

    table = Table(title=title, show_header=True, header_style="bold cyan", border_style="cyan")
    table.add_column("Name", style="bold white", no_wrap=True, min_width=20)
    table.add_column("Description", style="white", ratio=1)
    table.add_column("Latest", style="cyan", justify="center", min_width=8)
    for server in servers:
        table.add_row(
            server.get("name", "Unknown"),
            _truncate_registry_description(server.get("description", "No description available")),
            server.get("version", " --"),
        )
    return table


def _handle_registry_command_error(exc, registry, logger, action: str, generic_prefix: str) -> None:
    try:
        import requests

        if isinstance(exc, requests.RequestException) and _handle_registry_network_error(
            exc, registry, _get_console(), logger, action
        ):
            sys.exit(1)
    except ImportError:
        pass
    logger.error(f"{generic_prefix}: {exc}")
    sys.exit(1)


def _render_plain_server_details(server_name: str, registry, logger) -> None:
    logger.progress(f"Getting details for: {server_name}", symbol="search")
    try:
        server_info = registry.get_package_info(server_name)
    except ValueError:
        logger.error(f"Server '{server_name}' not found")
        sys.exit(1)
    click.echo(f"Name: {server_info.get('name', 'Unknown')}")
    click.echo(f"Description: {server_info.get('description', 'No description')}")
    click.echo(f"Repository: {server_info.get('repository', {}).get('url', 'Unknown')}")


def _get_server_repo_url(server_info: dict) -> str:
    repository = server_info.get("repository", {})
    return repository.get("url", "Unknown") if isinstance(repository, dict) else "Unknown"


def _render_rich_server_details(console, server_name: str, registry) -> None:
    from rich.table import Table

    console.print("\n[bold cyan]MCP Server Details[/bold cyan]")
    console.print(f"[muted]Fetching: {server_name}[/muted]")
    try:
        server_info = registry.get_package_info(server_name)
    except ValueError:
        console.print(
            f"\n[red]x[/red] MCP server '[bold]{server_name}[/bold]' not found in registry"
        )
        console.print(
            "\n[muted] Use [bold cyan]apm mcp search <query>[/bold cyan] to find available servers[/muted]"
        )
        sys.exit(1)

    name = server_info.get("name", "Unknown")
    info_table = Table(
        title=f" MCP Server: {name}",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    info_table.add_column("Property", style="bold white", min_width=12)
    info_table.add_column("Value", style="white", min_width=40)
    info_table.add_row("Name", f"[bold white]{name}[/bold white]")
    info_table.add_row("Version", f"[cyan]{_get_server_version(server_info)}[/cyan]")
    info_table.add_row("Description", server_info.get("description", "No description available"))
    info_table.add_row("Repository", _get_server_repo_url(server_info))
    if "id" in server_info:
        info_table.add_row("Registry ID", server_info["id"][:8] + "...")
    remotes = server_info.get("remotes", [])
    packages = server_info.get("packages", [])
    deployment_info = _collect_deployment_types(remotes, packages)
    if deployment_info:
        info_table.add_row("Deployment Type", " + ".join(deployment_info))
    console.print(info_table)
    if remotes:
        _render_remotes_table(console, remotes, name)
    if packages:
        _render_packages_table(console, packages, name)
    _render_install_table(console, server_info.get("name", server_name))


def _build_registry_with_diag(console, logger):
    """Construct ``RegistryIntegration`` honouring ``MCP_REGISTRY_URL``.

    Emits a one-line diagnostic naming the resolved registry URL whenever
    the env var is set, so enterprise users can confirm they are hitting
    the override and not the public default. Stays silent for the default
    public registry (defaults are quiet, overrides are visible).
    """
    from ..registry.integration import RegistryIntegration

    registry = RegistryIntegration()
    override = os.environ.get(MCP_REGISTRY_ENV)
    if override:
        url = registry.client.registry_url
        if console:
            console.print(f"[muted]Registry: {url}[/muted]")
        else:
            logger.progress(f"Registry: {url}")
    return registry


def _handle_registry_network_error(exc, registry, console, logger, action):
    """Render a registry network failure with env-var-aware guidance.

    ``action`` is a short verb phrase like ``"reach"`` so the message reads
    naturally: ``Could not <action> MCP registry at <url>``. Returns once
    the message is emitted; caller is responsible for ``sys.exit(1)``.
    """
    if registry is None:
        # Fell over before the registry was constructed; let the caller
        # emit its generic error path with the original exception.
        return False
    url = registry.client.registry_url
    override = os.environ.get(MCP_REGISTRY_ENV)
    if override:
        hint = f"{MCP_REGISTRY_ENV} is set -- verify the URL is correct and reachable."
    else:
        hint = "The registry may be temporarily unavailable. Retry shortly."

    msg = f"Could not {action} MCP registry at {url}"
    if console:
        from ..utils.console import STATUS_SYMBOLS

        console.print(f"\n{STATUS_SYMBOLS['error']} {msg}", style="red")
        console.print(f"  -> {hint}", style="dim")
    else:
        logger.error(msg)
        logger.error(hint)
    return True


@click.group(help="Discover, inspect, and install MCP servers")
def mcp():
    """Manage MCP server discovery, inspection, and installation."""
    pass


@mcp.command(
    name="install",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    help=(
        "Add an MCP server to apm.yml. Alias for 'apm install --mcp'.\n\n"
        "Examples:\n\n"
        "  apm mcp install fetch -- npx -y @modelcontextprotocol/server-fetch\n\n"
        "  apm mcp install api --transport http --url https://example.com/mcp"
    ),
    epilog=(
        "Common options (see `apm install --mcp --help` for full list):\n"
        "  --transport [stdio|http|sse|streamable-http]\n"
        "  --url URL           Server URL for remote transports\n"
        "  --env KEY=VALUE     Environment variable (repeatable)\n"
        "  --header KEY=VALUE  HTTP header (repeatable)\n"
        "  --registry URL      Custom registry URL\n"
        "  --mcp-version VER    Pin registry entry to a specific version\n"
        "  --dev / --dry-run / --force / --verbose / --no-policy\n"
    ),
)
@click.argument("name", required=True)
@click.pass_context
def mcp_install(ctx, name):
    """Forward all args to 'apm install --mcp ...'.

    Examples:
        apm mcp install fetch -- npx -y @modelcontextprotocol/server-fetch
        apm mcp install api --transport http --url https://example.com/mcp
    """
    from apm_cli.cli import cli
    from apm_cli.commands.install import (
        _get_invocation_argv,
        _split_argv_at_double_dash,
    )

    # Click strips the ``--`` separator from ``ctx.args`` even when
    # ``ignore_unknown_options`` is set, so post-``--`` tokens like
    # ``-y`` would be re-parsed as Click options when forwarded to
    # ``cli.main()``.  Re-insert the boundary by inspecting the raw
    # process argv (same seam the ``install`` command uses).
    _, post_dd = _split_argv_at_double_dash(_get_invocation_argv())
    if post_dd:
        pre_args = ctx.args[: len(ctx.args) - len(post_dd)]
        forwarded = ["install", "--mcp", name, *pre_args, "--", *post_dd]
    else:
        forwarded = ["install", "--mcp", name, *ctx.args]

    try:
        cli.main(args=forwarded, standalone_mode=False)
    except SystemExit as e:
        sys.exit(e.code if e.code is not None else 0)
    except click.ClickException as e:
        e.show()
        sys.exit(e.exit_code)


@mcp.command(help="Search MCP servers in registry")
@click.argument("query", required=True)
@click.option("--limit", default=10, show_default=True, help="Number of results to show")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def search(ctx, query, limit, verbose):
    """Search for MCP servers in the registry."""
    logger = CommandLogger("mcp-search", verbose=verbose)
    registry = None
    try:
        console = _get_console()
        registry = _build_registry_with_diag(console, logger)
        servers = registry.search_packages(query)[:limit]
        if not console:
            _render_plain_server_summaries(servers, logger, f"Searching for: {query}")
            return
        console.print("\n[bold cyan]MCP Registry Search[/bold cyan]")
        console.print(f"[muted]Query: {query}[/muted]")
        if not servers:
            console.print(
                f"\n[yellow][!][/yellow] No MCP servers found matching '[bold]{query}[/bold]'"
            )
            console.print("\n[muted] Try broader search terms or check the spelling[/muted]")
            return
        total_shown = len(servers)
        console.print(
            f"\n[green]+[/green] Found [bold]{total_shown}[/bold] MCP server{'s' if total_shown != 1 else ''}"
        )
        console.print(_build_registry_results_table(servers, ""))
        console.print(
            "\n[muted] Use [bold cyan]apm mcp show <name>[/bold cyan] for detailed information[/muted]"
        )
        if total_shown == limit:
            console.print(
                f"[muted]   Use [bold cyan]--limit {limit * 2}[/bold cyan] to see more results[/muted]"
            )
    except Exception as e:
        _handle_registry_command_error(e, registry, logger, "reach", "Error searching registry")


@mcp.command(help="Show detailed MCP server information")
@click.argument("server_name", required=True)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def show(ctx, server_name, verbose):
    """Show detailed information about an MCP server."""
    logger = CommandLogger("mcp-show", verbose=verbose)
    registry = None
    try:
        console = _get_console()
        registry = _build_registry_with_diag(console, logger)
        if not console:
            _render_plain_server_details(server_name, registry, logger)
            return
        _render_rich_server_details(console, server_name, registry)
    except Exception as e:
        _handle_registry_command_error(e, registry, logger, "reach", "Error getting server details")


@mcp.command(help="List all available MCP servers")
@click.option("--limit", default=20, show_default=True, help="Number of results to show")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def list(ctx, limit, verbose):
    """List all available MCP servers in the registry."""
    logger = CommandLogger("mcp-list", verbose=verbose)
    registry = None
    try:
        console = _get_console()
        registry = _build_registry_with_diag(console, logger)
        servers = registry.list_available_packages()[:limit]
        if not console:
            _render_plain_server_summaries(servers, logger, "Fetching available MCP servers...")
            return
        console.print("\n[bold cyan]MCP Registry Catalog[/bold cyan]")
        console.print("[muted]Discovering available servers...[/muted]")
        if not servers:
            console.print("\n[yellow][!][/yellow] No MCP servers found in registry")
            console.print("\n[muted] The registry might be temporarily unavailable[/muted]")
            return
        total_shown = len(servers)
        console.print(f"\n[green]+[/green] Showing [bold]{total_shown}[/bold] MCP servers")
        if total_shown == limit:
            console.print(
                f"[muted]Use [bold cyan]--limit {limit * 2}[/bold cyan] to see more results[/muted]"
            )
        console.print(_build_registry_results_table(servers, ""))
        console.print(
            "\n[muted] Use [bold cyan]apm mcp show <name>[/bold cyan] for detailed information[/muted]"
        )
        console.print(
            "[muted]   Use [bold cyan]apm mcp search <query>[/bold cyan] to find specific servers[/muted]"
        )
    except Exception as e:
        _handle_registry_command_error(e, registry, logger, "reach", "Error listing servers")
