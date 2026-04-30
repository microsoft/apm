"""Top-level ``apm view`` command (renamed from ``apm info``).

Shows detailed metadata for an installed package.  Also exposes helpers
reused by the backward-compatible ``apm deps info`` alias.

``apm info`` is kept as a hidden backward-compatible alias.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional  # noqa: F401, UP035

import click

from ..constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ..core.auth import AuthResolver
from ..core.command_logger import CommandLogger
from ..deps.github_downloader import GitHubPackageDownloader
from ..models.dependency.reference import DependencyReference
from ..models.dependency.types import GitReferenceType, RemoteRef  # noqa: F401
from ..utils.path_security import PathTraversalError, ensure_path_within, validate_path_segments
from .deps._utils import _get_detailed_package_info

# ------------------------------------------------------------------
# Valid field names (extensible in follow-up tasks)
# ------------------------------------------------------------------
VALID_FIELDS = ("versions",)


# ------------------------------------------------------------------
# Shared helpers (used by both ``apm info`` and ``apm deps info``)
# ------------------------------------------------------------------


def resolve_package_path(
    package: str,
    apm_modules_path: Path,
    logger: CommandLogger,
) -> Path | None:
    """Locate the package directory inside *apm_modules_path*.

    Resolution order:
      1. Direct path match (handles ``org/repo`` and deeper sub-paths).
      2. Fallback two-level scan for short (repo-only) names.

    Returns *None* when path validation fails (traversal attempt).
    Exits via ``sys.exit(1)`` when the package cannot be found so that
    callers do not need to duplicate error handling.
    """
    # Guard: reject traversal sequences before building any path
    try:
        validate_path_segments(package, context="package name")
    except PathTraversalError as exc:
        logger.error(str(exc))
        return None

    # 1 -- direct match
    direct_match = apm_modules_path / package

    # Guard: ensure resolved path stays within apm_modules/
    try:
        ensure_path_within(direct_match, apm_modules_path)
    except PathTraversalError as exc:
        logger.error(str(exc))
        return None
    if direct_match.is_dir() and (
        (direct_match / APM_YML_FILENAME).exists() or (direct_match / SKILL_MD_FILENAME).exists()
    ):
        return direct_match

    # 2 -- fallback scan
    for org_dir in apm_modules_path.iterdir():
        if org_dir.is_dir() and not org_dir.name.startswith("."):
            for package_dir in org_dir.iterdir():
                if package_dir.is_dir() and not package_dir.name.startswith("."):
                    if (
                        package_dir.name == package  # noqa: PLR1714
                        or f"{org_dir.name}/{package_dir.name}" == package
                    ):
                        return package_dir

    # Not found -- show available packages and exit
    logger.error(f"Package '{package}' not found in apm_modules/")
    logger.progress("Available packages:")
    for org_dir in apm_modules_path.iterdir():
        if org_dir.is_dir() and not org_dir.name.startswith("."):
            for package_dir in org_dir.iterdir():
                if package_dir.is_dir() and not package_dir.name.startswith("."):
                    click.echo(f"  - {org_dir.name}/{package_dir.name}")
    sys.exit(1)


def _lookup_lockfile_ref(package: str, project_root: Path):
    """Return (ref, commit) from the lockfile for *package*, or ("", "")."""
    try:
        from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed

        migrate_lockfile_if_needed(project_root)
        lockfile_path = get_lockfile_path(project_root)
        lockfile = LockFile.read(lockfile_path)
        if lockfile is None:
            return "", ""

        # Try exact key first, then substring match
        dep = lockfile.dependencies.get(package)
        if dep is None:
            for key, d in lockfile.dependencies.items():
                if package in key or key.endswith(f"/{package}"):
                    dep = d
                    break

        if dep is not None:
            return dep.resolved_ref or "", dep.resolved_commit or ""
    except Exception:
        pass
    return "", ""


def display_package_info(
    package: str,
    package_path: Path,
    logger: CommandLogger,
    project_root: Path | None = None,
) -> None:
    """Load and render package metadata to the terminal.

    Uses a Rich panel when available, falling back to plain text.
    When *project_root* is provided, the lockfile is consulted for
    ref and commit information.
    """
    try:
        package_info = _get_detailed_package_info(package_path)

        # Look up lockfile entry for ref/commit info
        locked_ref = ""
        locked_commit = ""
        if project_root is not None:
            locked_ref, locked_commit = _lookup_lockfile_ref(package, project_root)

        try:
            from rich.console import Console
            from rich.panel import Panel

            console = Console()

            content_lines = []
            content_lines.append(f"[bold]Name:[/bold] {package_info['name']}")
            content_lines.append(f"[bold]Version:[/bold] {package_info['version']}")
            content_lines.append(f"[bold]Description:[/bold] {package_info['description']}")
            content_lines.append(f"[bold]Author:[/bold] {package_info['author']}")
            content_lines.append(f"[bold]Source:[/bold] {package_info['source']}")
            if locked_ref:
                content_lines.append(f"[bold]Ref:[/bold] {locked_ref}")
            if locked_commit:
                content_lines.append(f"[bold]Commit:[/bold] {locked_commit[:12]}")
            content_lines.append(f"[bold]Install Path:[/bold] {package_info['install_path']}")
            content_lines.append("")
            content_lines.append("[bold]Context Files:[/bold]")

            for context_type, count in package_info["context_files"].items():
                if count > 0:
                    content_lines.append(f"  * {count} {context_type}")

            if not any(count > 0 for count in package_info["context_files"].values()):
                content_lines.append("  * No context files found")

            content_lines.append("")
            content_lines.append("[bold]Agent Workflows:[/bold]")
            if package_info["workflows"] > 0:
                content_lines.append(f"  * {package_info['workflows']} executable workflows")
            else:
                content_lines.append("  * No agent workflows found")

            if package_info.get("hooks", 0) > 0:
                content_lines.append("")
                content_lines.append("[bold]Hooks:[/bold]")
                content_lines.append(f"  * {package_info['hooks']} hook file(s)")

            content = "\n".join(content_lines)
            panel = Panel(
                content,
                title=f"[[i]] Package Info: {package}",
                border_style="cyan",
            )
            console.print(panel)

        except ImportError:
            # Fallback text display
            click.echo(f"[i] Package Info: {package}")
            click.echo("=" * 40)
            click.echo(f"Name: {package_info['name']}")
            click.echo(f"Version: {package_info['version']}")
            click.echo(f"Description: {package_info['description']}")
            click.echo(f"Author: {package_info['author']}")
            click.echo(f"Source: {package_info['source']}")
            if locked_ref:
                click.echo(f"Ref: {locked_ref}")
            if locked_commit:
                click.echo(f"Commit: {locked_commit[:12]}")
            click.echo(f"Install Path: {package_info['install_path']}")
            click.echo("")
            click.echo("Context Files:")

            for context_type, count in package_info["context_files"].items():
                if count > 0:
                    click.echo(f"  * {count} {context_type}")

            if not any(count > 0 for count in package_info["context_files"].values()):
                click.echo("  * No context files found")

            click.echo("")
            click.echo("Agent Workflows:")
            if package_info["workflows"] > 0:
                click.echo(f"  * {package_info['workflows']} executable workflows")
            else:
                click.echo("  * No agent workflows found")

            if package_info.get("hooks", 0) > 0:
                click.echo("")
                click.echo("Hooks:")
                click.echo(f"  * {package_info['hooks']} hook file(s)")

    except Exception as e:
        logger.error(f"Error reading package information: {e}")
        sys.exit(1)


def _display_marketplace_plugin(
    plugin_name: str,
    marketplace_name: str,
    logger: CommandLogger,
) -> None:
    """Display metadata for a marketplace plugin.

    Fetches the marketplace manifest, finds the plugin, and renders
    its entry information (name, version, description, source).
    """
    from ..marketplace.client import fetch_or_cache
    from ..marketplace.errors import MarketplaceFetchError
    from ..marketplace.models import MarketplaceSource
    from ..marketplace.registry import get_marketplace_by_name

    # -- Fetch marketplace & plugin --
    try:
        source: MarketplaceSource = get_marketplace_by_name(marketplace_name)
    except Exception as exc:
        logger.error(str(exc))
        sys.exit(1)

    try:
        manifest = fetch_or_cache(source)
    except MarketplaceFetchError as exc:
        logger.error(str(exc))
        logger.progress("Check your network connection and try again.")
        sys.exit(1)

    plugin = manifest.find_plugin(plugin_name)
    if plugin is None:
        from ..marketplace.errors import PluginNotFoundError as _PNF

        logger.error(str(_PNF(plugin_name, marketplace_name)))
        sys.exit(1)

    # -- Build info lines --
    title = f"Plugin: {plugin.name} (marketplace: {marketplace_name})"

    # Resolve canonical reference for display
    resolved_display = None
    try:
        from ..marketplace.resolver import resolve_marketplace_plugin

        canonical_str, _resolved = resolve_marketplace_plugin(
            plugin_name,
            marketplace_name,
            plugin,
        )
        resolved_display = canonical_str
    except Exception:
        pass

    # Determine source display
    source_display = "--"
    if isinstance(plugin.source, dict):
        src_type = plugin.source.get("type", "") or plugin.source.get("source", "")
        repo = plugin.source.get("repo", "") or plugin.source.get("url", "")
        ref = plugin.source.get("ref", "")
        parts = [s for s in [src_type, repo] if s]
        source_display = " / ".join(parts)
        if ref:
            source_display += f" @ {ref}"
    elif isinstance(plugin.source, str):
        source_display = plugin.source

    try:
        from rich.console import Console
        from rich.panel import Panel

        console = Console()
        lines = []
        lines.append(f"[bold]Name:[/bold]        {plugin.name}")
        if plugin.version:
            lines.append(f"[bold]Version:[/bold]     {plugin.version}")
        if plugin.description:
            lines.append(f"[bold]Description:[/bold] {plugin.description}")
        lines.append(f"[bold]Source:[/bold]      {source_display}")
        if resolved_display:
            lines.append(f"[bold]Resolved:[/bold]    {resolved_display}")
        if plugin.tags:
            lines.append(f"[bold]Tags:[/bold]        {', '.join(plugin.tags)}")

        console.print(
            Panel(
                "\n".join(lines),
                title=title,
                border_style="cyan",
            )
        )
        click.echo("")
        click.echo(f"  Install: apm install {plugin.name}@{marketplace_name}")

    except ImportError:
        # Plain-text fallback
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
        click.echo(f"  Install: apm install {plugin.name}@{marketplace_name}")


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


# ------------------------------------------------------------------
# Click command
# ------------------------------------------------------------------


@click.command(name="view")
@click.argument("package", required=True)
@click.argument("field", required=False, default=None)
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Inspect package from user scope (~/.apm/)",
)
def view(package: str, field: str | None, global_: bool):
    """View package metadata or list remote versions.

    Without FIELD, displays local metadata for an installed package.
    With FIELD, queries specific data (may contact the remote).

    \b
    Fields:
        versions    List available remote tags and branches

    \b
    Examples:
        apm view org/repo                # Local metadata
        apm view org/repo versions       # Remote tags/branches
        apm view org/repo -g             # From user scope
    """
    from ..core.scope import InstallScope, get_apm_dir

    logger = CommandLogger("view")

    # --- field validation (before any I/O) ---
    if field is not None:
        if field not in VALID_FIELDS:
            valid_list = ", ".join(VALID_FIELDS)
            logger.error(f"Unknown field '{field}'. Valid fields: {valid_list}")
            sys.exit(1)

        if field == "versions":
            display_versions(package, logger)
            return

    # --- marketplace ref without explicit field -> show versions ---
    from ..marketplace.resolver import parse_marketplace_ref

    marketplace_ref = parse_marketplace_ref(package)
    if marketplace_ref is not None:
        plugin_name, marketplace_name, _version_spec = marketplace_ref
        _display_marketplace_plugin(plugin_name, marketplace_name, logger)
        return

    # --- default: show local metadata ---
    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    if global_:
        project_root = get_apm_dir(scope)
        apm_modules_path = project_root / APM_MODULES_DIR
    else:
        project_root = Path(".")
        apm_modules_path = project_root / APM_MODULES_DIR

    if not apm_modules_path.exists():
        logger.error("No apm_modules/ directory found")
        logger.progress("Run 'apm install' to install dependencies first")
        sys.exit(1)

    package_path = resolve_package_path(package, apm_modules_path, logger)
    if package_path is None:
        sys.exit(1)
    display_package_info(package, package_path, logger, project_root=project_root)
