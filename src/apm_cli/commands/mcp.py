"""APM mcp command group."""

import builtins
import os
import sys

import click

from ..core.command_logger import CommandLogger
from ._helpers import _get_console

# Restore builtin since a subcommand is named ``list``
list = builtins.list

MCP_REGISTRY_ENV = "MCP_REGISTRY_URL"


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
            # Fallback for non-rich environments
            logger.progress(f"Searching for: {query}", symbol="search")
            if not servers:
                logger.warning("No servers found")
                return
            for server in servers:
                click.echo(f"  {server.get('name', 'Unknown')}")
                click.echo(f"    {server.get('description', 'No description')[:80]}")
            return

        # Professional header with search context
        console.print("\n[bold cyan]MCP Registry Search[/bold cyan]")
        console.print(f"[muted]Query: {query}[/muted]")

        if not servers:
            console.print(
                f"\n[yellow][!][/yellow] No MCP servers found matching '[bold]{query}[/bold]'"
            )
            console.print("\n[muted] Try broader search terms or check the spelling[/muted]")
            return

        # Results summary
        total_shown = len(servers)
        console.print(
            f"\n[green]+[/green] Found [bold]{total_shown}[/bold] MCP server{'s' if total_shown != 1 else ''}"
        )

        # Professional results table
        from rich.table import Table

        table = Table(show_header=True, header_style="bold cyan", border_style="cyan")
        table.add_column("Name", style="bold white", no_wrap=True, min_width=20)
        table.add_column("Description", style="white", ratio=1)
        table.add_column("Latest", style="cyan", justify="center", min_width=8)

        for server in servers:
            name = server.get("name", "Unknown")
            desc = server.get("description", "No description available")
            version = server.get("version", " --")

            # Intelligent description truncation
            if len(desc) > 80:
                # Find a good break point near the limit
                truncate_pos = 77
                if " " in desc[70:85]:
                    space_pos = desc.rfind(" ", 70, 85)
                    if space_pos > 70:
                        truncate_pos = space_pos
                desc = desc[:truncate_pos] + "..."

            table.add_row(name, desc, version)

        console.print(table)

        # Helpful next steps
        console.print(
            "\n[muted] Use [bold cyan]apm mcp show <name>[/bold cyan] for detailed information[/muted]"
        )
        if total_shown == limit:
            console.print(
                f"[muted]   Use [bold cyan]--limit {limit * 2}[/bold cyan] to see more results[/muted]"
            )

    except Exception as e:
        try:
            import requests

            if isinstance(e, requests.RequestException) and _handle_registry_network_error(
                e, registry, _get_console(), logger, "reach"
            ):
                sys.exit(1)
        except ImportError:
            pass
        logger.error(f"Error searching registry: {e}")
        sys.exit(1)


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
            # Fallback for non-rich environments
            logger.progress(f"Getting details for: {server_name}", symbol="search")
            try:
                server_info = registry.get_package_info(server_name)
                click.echo(f"Name: {server_info.get('name', 'Unknown')}")
                click.echo(f"Description: {server_info.get('description', 'No description')}")
                click.echo(f"Repository: {server_info.get('repository', {}).get('url', 'Unknown')}")
            except ValueError:
                logger.error(f"Server '{server_name}' not found")
                sys.exit(1)
            return

        # Professional loading indicator
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

        # Main server information in professional table format
        name = server_info.get("name", "Unknown")
        description = server_info.get("description", "No description available")

        # Get key metadata
        version = "Unknown"
        if "version_detail" in server_info:
            version = server_info["version_detail"].get("version", "Unknown")
        elif "version" in server_info:
            version = server_info["version"]

        repo_url = "Unknown"
        if "repository" in server_info:
            repo_url = server_info["repository"].get("url", "Unknown")

        # Professional server info table with consistent styling
        from rich.table import Table

        # Main server information table
        info_table = Table(
            title=f" MCP Server: {name}",
            show_header=True,
            header_style="bold cyan",
            border_style="cyan",
        )
        info_table.add_column("Property", style="bold white", min_width=12)
        info_table.add_column("Value", style="white", min_width=40)

        info_table.add_row("Name", f"[bold white]{name}[/bold white]")
        info_table.add_row("Version", f"[cyan]{version}[/cyan]")
        info_table.add_row("Description", description)
        info_table.add_row("Repository", repo_url)
        if "id" in server_info:
            info_table.add_row("Registry ID", server_info["id"][:8] + "...")

        # Add deployment type information
        remotes = server_info.get("remotes", [])
        packages = server_info.get("packages", [])

        deployment_info = []
        if remotes:
            for remote in remotes:
                transport_type = remote.get("transport_type", "unknown")
                if transport_type == "sse":
                    deployment_info.append(" Remote SSE Endpoint")
        if packages:
            deployment_info.append(" Local Package")

        if deployment_info:
            info_table.add_row("Deployment Type", " + ".join(deployment_info))

        console.print(info_table)

        # Show remote endpoints if available
        if remotes:
            remote_table = Table(
                title=" Remote Endpoints",
                show_header=True,
                header_style="bold cyan",
                border_style="cyan",
            )
            remote_table.add_column("Type", style="yellow", width=10)
            remote_table.add_column("URL", style="white", min_width=40)
            remote_table.add_column("Features", style="cyan", min_width=20)

            for remote in remotes:
                transport_type = remote.get("transport_type", "unknown")
                url = remote.get("url", "unknown")

                # Describe features/limitations of remote endpoints
                features = "Hosted by provider"
                if "github" in name.lower():
                    features = "No toolset customization"

                remote_table.add_row(transport_type.upper(), url, features)

            console.print(remote_table)

        # Installation packages in consistent table format
        if packages:
            pkg_table = Table(
                title=" Local Packages",
                show_header=True,
                header_style="bold cyan",
                border_style="cyan",
            )
            pkg_table.add_column("Registry", style="yellow", width=10)
            pkg_table.add_column("Package", style="white", min_width=25)
            pkg_table.add_column("Runtime", style="cyan", width=8, justify="center")
            pkg_table.add_column("Features", style="green", min_width=20)

            for pkg in packages:
                registry_name = pkg.get("registry_name", "unknown")
                pkg_name = pkg.get("name", "unknown")
                runtime_hint = pkg.get("runtime_hint", " --")

                # Describe features of local packages
                features = "Full configuration control"
                if "github" in name.lower():
                    features = "Supports GITHUB_TOOLSETS"

                # Truncate long package names intelligently
                if len(pkg_name) > 25:
                    pkg_name = pkg_name[:22] + "..."

                pkg_table.add_row(registry_name, pkg_name, runtime_hint, features)

            console.print(pkg_table)

        # Installation instructions in structured table format
        install_name = server_info.get("name", server_name)
        install_table = Table(
            title="* Installation Guide",
            show_header=True,
            header_style="bold cyan",
            border_style="green",
        )
        install_table.add_column("Step", style="bold white", width=5)
        install_table.add_column("Action", style="white", min_width=30)
        install_table.add_column("Command/Config", style="cyan", min_width=25)

        install_table.add_row(
            "1",
            "Add to apm.yml dependencies",
            f"[yellow]mcp:[/yellow] [cyan]- {install_name}[/cyan]",
        )
        install_table.add_row("2", "Install dependencies", "[bold cyan]apm install[/bold cyan]")
        install_table.add_row(
            "3",
            "Direct install (coming soon)",
            f"[bold cyan]apm install {install_name}[/bold cyan]",
        )

        console.print(install_table)

    except Exception as e:
        try:
            import requests

            if isinstance(e, requests.RequestException) and _handle_registry_network_error(
                e, registry, _get_console(), logger, "reach"
            ):
                sys.exit(1)
        except ImportError:
            pass
        logger.error(f"Error getting server details: {e}")
        sys.exit(1)


@mcp.command(help="List all available MCP servers")
@click.option("--limit", default=20, show_default=True, help="Number of results to show")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def list(ctx, limit, verbose):  # noqa: F811
    """List all available MCP servers in the registry."""
    logger = CommandLogger("mcp-list", verbose=verbose)
    registry = None
    try:
        console = _get_console()
        registry = _build_registry_with_diag(console, logger)

        if not console:
            # Fallback for non-rich environments
            logger.progress("Fetching available MCP servers...", symbol="search")
            servers = registry.list_available_packages()[:limit]
            if not servers:
                logger.warning("No servers found")
                return
            for server in servers:
                click.echo(f"  {server.get('name', 'Unknown')}")
                click.echo(f"    {server.get('description', 'No description')[:80]}")
            return

        # Professional header
        console.print("\n[bold cyan]MCP Registry Catalog[/bold cyan]")
        console.print("[muted]Discovering available servers...[/muted]")

        servers = registry.list_available_packages()[:limit]

        if not servers:
            console.print("\n[yellow][!][/yellow] No MCP servers found in registry")
            console.print("\n[muted] The registry might be temporarily unavailable[/muted]")
            return

        # Results summary with pagination info
        total_shown = len(servers)
        console.print(f"\n[green]+[/green] Showing [bold]{total_shown}[/bold] MCP servers")
        if total_shown == limit:
            console.print(
                f"[muted]Use [bold cyan]--limit {limit * 2}[/bold cyan] to see more results[/muted]"
            )

        # Professional catalog table
        from rich.table import Table

        table = Table(show_header=True, header_style="bold cyan", border_style="cyan")
        table.add_column("Name", style="bold white", no_wrap=True, min_width=25)
        table.add_column("Description", style="white", ratio=1)
        table.add_column("Latest", style="cyan", justify="center", min_width=8)

        for server in servers:
            name = server.get("name", "Unknown")
            desc = server.get("description", "No description available")
            version = server.get("version", " --")

            # Intelligent description truncation
            if len(desc) > 80:
                # Find a good break point near the limit
                truncate_pos = 77
                if " " in desc[70:85]:
                    space_pos = desc.rfind(" ", 70, 85)
                    if space_pos > 70:
                        truncate_pos = space_pos
                desc = desc[:truncate_pos] + "..."

            table.add_row(name, desc, version)

        console.print(table)

        # Helpful navigation
        console.print(
            "\n[muted] Use [bold cyan]apm mcp show <name>[/bold cyan] for detailed information[/muted]"
        )
        console.print(
            "[muted]   Use [bold cyan]apm mcp search <query>[/bold cyan] to find specific servers[/muted]"
        )

    except Exception as e:
        try:
            import requests

            if isinstance(e, requests.RequestException) and _handle_registry_network_error(
                e, registry, _get_console(), logger, "reach"
            ):
                sys.exit(1)
        except ImportError:
            pass
        logger.error(f"Error listing servers: {e}")
        sys.exit(1)
