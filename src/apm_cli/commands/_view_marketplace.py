"""Marketplace-specific view helpers extracted from view.py."""

from __future__ import annotations

import sys

import click

from ..core.auth import AuthResolver
from ..core.command_logger import CommandLogger
from ..deps.github_downloader import GitHubPackageDownloader
from ..models.dependency.reference import DependencyReference
from ..models.dependency.types import RemoteRef


def _resolve_marketplace_source(marketplace_name: str, logger: CommandLogger):
    from ..marketplace.models import MarketplaceSource
    from ..marketplace.registry import get_marketplace_by_name

    try:
        source: MarketplaceSource = get_marketplace_by_name(marketplace_name)
    except Exception as exc:
        logger.error(str(exc))
        sys.exit(1)
    return source


def _fetch_marketplace_manifest(source, logger: CommandLogger):
    from ..marketplace.client import fetch_or_cache
    from ..marketplace.errors import MarketplaceFetchError

    try:
        return fetch_or_cache(source)
    except MarketplaceFetchError as exc:
        logger.error(str(exc))
        logger.progress("Check your network connection and try again.")
        sys.exit(1)


def _find_marketplace_plugin(
    manifest, plugin_name: str, marketplace_name: str, logger: CommandLogger
):
    plugin = manifest.find_plugin(plugin_name)
    if plugin is not None:
        return plugin
    from ..marketplace.errors import PluginNotFoundError as _PNF

    logger.error(str(_PNF(plugin_name, marketplace_name)))
    sys.exit(1)


def _resolve_plugin_display(plugin_name: str, marketplace_name: str, plugin) -> str | None:
    try:
        from ..marketplace.resolver import resolve_marketplace_plugin

        canonical_str, _resolved = resolve_marketplace_plugin(
            plugin_name,
            marketplace_name,
            plugin,
        )
        return canonical_str
    except Exception:
        return None


def _build_plugin_source_display(plugin) -> str:
    if isinstance(plugin.source, str):
        return plugin.source
    if not isinstance(plugin.source, dict):
        return "--"
    src_type = plugin.source.get("type", "") or plugin.source.get("source", "")
    repo = plugin.source.get("repo", "") or plugin.source.get("url", "")
    ref = plugin.source.get("ref", "")
    source_display = " / ".join(part for part in [src_type, repo] if part) or "--"
    return f"{source_display} @ {ref}" if ref else source_display


def _build_plugin_lines(plugin, source_display: str, resolved_display: str | None) -> list[str]:
    lines = [f"[bold]Name:[/bold]        {plugin.name}"]
    if plugin.version:
        lines.append(f"[bold]Version:[/bold]     {plugin.version}")
    if plugin.description:
        lines.append(f"[bold]Description:[/bold] {plugin.description}")
    lines.append(f"[bold]Source:[/bold]      {source_display}")
    if resolved_display:
        lines.append(f"[bold]Resolved:[/bold]    {resolved_display}")
    if plugin.tags:
        lines.append(f"[bold]Tags:[/bold]        {', '.join(plugin.tags)}")
    return lines


def _render_marketplace_plugin_rich(title: str, lines: list[str], plugin_name: str) -> None:
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    console.print(Panel("\n".join(lines), title=title, border_style="cyan"))
    click.echo("")
    click.echo(f"  Install: apm install {plugin_name}")


def _render_marketplace_plugin_plain(
    title: str, plugin, source_display: str, resolved_display: str | None, install_ref: str
) -> None:
    click.echo(title)
    click.echo("-" * 60)
    click.echo(f"  Name:        {plugin.name}")
    if plugin.version:
        click.echo(f"  Version:     {plugin.version}")
    if plugin.description:
        click.echo(f"  Description: {plugin.description}")
    click.echo(f"  Source:      {source_display}")
    if resolved_display:
        click.echo(f"  Resolved:    {resolved_display}")
    if plugin.tags:
        click.echo(f"  Tags:        {', '.join(plugin.tags)}")
    click.echo("")
    click.echo(f"  Install: apm install {install_ref}")


def _display_marketplace_plugin(
    plugin_name: str,
    marketplace_name: str,
    logger: CommandLogger,
) -> None:
    """Display metadata for a marketplace plugin."""
    source = _resolve_marketplace_source(marketplace_name, logger)
    manifest = _fetch_marketplace_manifest(source, logger)
    plugin = _find_marketplace_plugin(manifest, plugin_name, marketplace_name, logger)
    title = f"Plugin: {plugin.name} (marketplace: {marketplace_name})"
    install_ref = f"{plugin.name}@{marketplace_name}"
    resolved_display = _resolve_plugin_display(plugin_name, marketplace_name, plugin)
    source_display = _build_plugin_source_display(plugin)
    lines = _build_plugin_lines(plugin, source_display, resolved_display)
    try:
        from rich.console import Console
        from rich.panel import Panel

        console = Console()
        console.print(Panel("\n".join(lines), title=title, border_style="cyan"))
        click.echo("")
        click.echo(f"  Install: apm install {install_ref}")
    except ImportError:
        _render_marketplace_plugin_plain(
            title,
            plugin,
            source_display,
            resolved_display,
            install_ref,
        )


def display_versions(package: str, logger: CommandLogger) -> None:
    """Query and display available remote versions (tags/branches).

    This is a purely remote operation -- it does NOT require the package
    to be installed locally.  It parses *package* as a
    ``DependencyReference``, queries remote refs via
    ``GitHubPackageDownloader.list_remote_refs``, and renders the result
    as a Rich table (with a plain-text fallback).

    When *package* matches the ``NAME@MARKETPLACE`` pattern, the
    marketplace manifest is fetched instead and the plugin's marketplace
    metadata is displayed.
    """
    # -- Marketplace path: NAME@MARKETPLACE --
    from ..marketplace.resolver import parse_marketplace_ref

    marketplace_ref = parse_marketplace_ref(package)
    if marketplace_ref is not None:
        plugin_name, marketplace_name, _version_spec = marketplace_ref
        _display_marketplace_plugin(plugin_name, marketplace_name, logger)
        return

    # -- Git-based path (unchanged) --
    try:
        dep_ref = DependencyReference.parse(package)
    except ValueError as exc:
        logger.error(f"Invalid package reference '{package}': {exc}")
        sys.exit(1)

    try:
        downloader = GitHubPackageDownloader(auth_resolver=AuthResolver())
        refs: list[RemoteRef] = downloader.list_remote_refs(dep_ref)
    except RuntimeError as exc:
        logger.error(f"Failed to list versions for '{package}': {exc}")
        sys.exit(1)

    if not refs:
        logger.progress(f"No versions found for '{package}'")
        return

    # -- render with Rich table (fallback to plain text) ---------------
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(
            title=f"Available versions: {package}",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Name", style="bold white")
        table.add_column("Type", style="yellow")
        table.add_column("Commit", style="dim white")

        for ref in refs:
            table.add_row(
                ref.name,
                ref.ref_type.value,
                ref.commit_sha[:8],
            )

        console.print(table)

    except ImportError:
        # Plain-text fallback
        click.echo(f"Available versions: {package}")
        click.echo("-" * 50)
        click.echo(f"{'Name':<30} {'Type':<10} {'Commit':<10}")
        click.echo("-" * 50)
        for ref in refs:
            click.echo(f"{ref.name:<30} {ref.ref_type.value:<10} {ref.commit_sha[:8]:<10}")
