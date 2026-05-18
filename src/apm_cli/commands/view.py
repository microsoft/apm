"""Top-level ``apm view`` command (renamed from ``apm info``).

Shows detailed metadata for an installed package.  Also exposes helpers
reused by the backward-compatible ``apm deps info`` alias.

``apm info`` is kept as a hidden backward-compatible alias.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ..core.auth import AuthResolver
from ..core.command_logger import CommandLogger
from ..deps.github_downloader import GitHubPackageDownloader
from ..models.dependency.reference import DependencyReference
from ..utils.path_security import PathTraversalError, ensure_path_within, validate_path_segments
from . import _view_marketplace
from .deps._utils import _get_detailed_package_info

_display_marketplace_plugin = _view_marketplace._display_marketplace_plugin

# ------------------------------------------------------------------
# Valid field names (extensible in follow-up tasks)
# ------------------------------------------------------------------
VALID_FIELDS = ("versions",)


# ------------------------------------------------------------------
# Shared helpers (used by both ``apm info`` and ``apm deps info``)
# ------------------------------------------------------------------


def _iter_available_packages(apm_modules_path: Path):
    for org_dir in apm_modules_path.iterdir():
        if not org_dir.is_dir() or org_dir.name.startswith("."):
            continue
        for package_dir in org_dir.iterdir():
            if package_dir.is_dir() and not package_dir.name.startswith("."):
                yield org_dir, package_dir


def _find_direct_package_match(package: str, apm_modules_path: Path) -> Path | None:
    direct_match = apm_modules_path / package
    if direct_match.is_dir() and (
        (direct_match / APM_YML_FILENAME).exists() or (direct_match / SKILL_MD_FILENAME).exists()
    ):
        return direct_match
    return None


def _find_scanned_package_match(package: str, apm_modules_path: Path) -> Path | None:
    for org_dir, package_dir in _iter_available_packages(apm_modules_path):
        if package in (package_dir.name, f"{org_dir.name}/{package_dir.name}"):
            return package_dir
    return None


def _show_available_packages(apm_modules_path: Path, logger: CommandLogger) -> None:
    logger.progress("Available packages:")
    for org_dir, package_dir in _iter_available_packages(apm_modules_path):
        click.echo(f"  - {org_dir.name}/{package_dir.name}")


def _lookup_locked_version(package: str, project_root: Path | None) -> tuple[str, str]:
    if project_root is None:
        return "", ""
    return _lookup_lockfile_ref(package, project_root)


def _append_context_file_lines(lines: list[str], context_files: dict[str, int]) -> None:
    lines.append("")
    lines.append("[bold]Context Files:[/bold]")
    found_context = False
    for context_type, count in context_files.items():
        if count > 0:
            found_context = True
            lines.append(f"  * {count} {context_type}")
    if not found_context:
        lines.append("  * No context files found")


def _append_workflow_lines(lines: list[str], package_info: dict) -> None:
    lines.append("")
    lines.append("[bold]Agent Workflows:[/bold]")
    if package_info["workflows"] > 0:
        lines.append(f"  * {package_info['workflows']} executable workflows")
    else:
        lines.append("  * No agent workflows found")
    if package_info.get("hooks", 0) > 0:
        lines.append("")
        lines.append("[bold]Hooks:[/bold]")
        lines.append(f"  * {package_info['hooks']} hook file(s)")


def _build_package_info_lines(package_info: dict, locked_ref: str, locked_commit: str) -> list[str]:
    lines = [
        f"[bold]Name:[/bold] {package_info['name']}",
        f"[bold]Version:[/bold] {package_info['version']}",
        f"[bold]Description:[/bold] {package_info['description']}",
        f"[bold]Author:[/bold] {package_info['author']}",
        f"[bold]Source:[/bold] {package_info['source']}",
    ]
    if locked_ref:
        lines.append(f"[bold]Ref:[/bold] {locked_ref}")
    if locked_commit:
        lines.append(f"[bold]Commit:[/bold] {locked_commit[:12]}")
    lines.append(f"[bold]Install Path:[/bold] {package_info['install_path']}")
    _append_context_file_lines(lines, package_info["context_files"])
    _append_workflow_lines(lines, package_info)
    return lines


def _render_package_info_rich(package: str, lines: list[str]) -> None:
    from rich.console import Console
    from rich.panel import Panel

    Console().print(
        Panel(
            "\n".join(lines),
            title=f"[[i]] Package Info: {package}",
            border_style="cyan",
        )
    )


def _render_package_info_plain(
    package_info: dict, locked_ref: str, locked_commit: str, package: str
) -> None:
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
    found_context = False
    for context_type, count in package_info["context_files"].items():
        if count > 0:
            found_context = True
            click.echo(f"  * {count} {context_type}")
    if not found_context:
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


def display_versions(package: str, logger: CommandLogger) -> None:
    """List remote package versions using this module's patchable symbols."""
    _view_marketplace.AuthResolver = AuthResolver
    _view_marketplace.GitHubPackageDownloader = GitHubPackageDownloader
    _view_marketplace.DependencyReference = DependencyReference
    _view_marketplace.display_versions(package, logger)


def resolve_package_path(
    package: str,
    apm_modules_path: Path,
    logger: CommandLogger,
) -> Path | None:
    """Locate the package directory inside *apm_modules_path*."""
    try:
        validate_path_segments(package, context="package name")
        ensure_path_within(apm_modules_path / package, apm_modules_path)
    except PathTraversalError as exc:
        logger.error(str(exc))
        return None

    direct_match = _find_direct_package_match(package, apm_modules_path)
    if direct_match is not None:
        return direct_match

    scanned_match = _find_scanned_package_match(package, apm_modules_path)
    if scanned_match is not None:
        return scanned_match

    logger.error(f"Package '{package}' not found in apm_modules/")
    _show_available_packages(apm_modules_path, logger)
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
    """Load and render package metadata to the terminal."""
    try:
        package_info = _get_detailed_package_info(package_path)
        locked_ref, locked_commit = _lookup_locked_version(package, project_root)
        lines = _build_package_info_lines(package_info, locked_ref, locked_commit)
        try:
            _render_package_info_rich(package, lines)
        except ImportError:
            _render_package_info_plain(package_info, locked_ref, locked_commit, package)
    except Exception as e:
        logger.error(f"Error reading package information: {e}")
        sys.exit(1)


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
