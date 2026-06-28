"""Top-level ``apm view`` command (renamed from ``apm info``).

Shows detailed metadata for an installed package.  Also exposes helpers
reused by the backward-compatible ``apm deps info`` alias.

``apm info`` is kept as a hidden backward-compatible alias.
"""

import sys
from pathlib import Path

import click

from ..constants import APM_MODULES_DIR, APM_YML_FILENAME, SKILL_MD_FILENAME
from ..core.auth import AuthResolver
from ..core.command_logger import CommandLogger
from ..deps.github_downloader import GitHubPackageDownloader
from ..models.dependency.reference import DependencyReference
from ..models.dependency.types import RemoteRef
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


def _lookup_lockfile_ref(package: str, project_root: Path) -> tuple[str, str, str]:
    """Return (ref, commit, source) from the lockfile for *package*, or ("", "", "")."""
    try:
        from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed

        migrate_lockfile_if_needed(project_root)
        lockfile_path = get_lockfile_path(project_root)
        lockfile = LockFile.read(lockfile_path)
        if lockfile is None:
            return "", "", ""

        # Try exact key first, then substring match
        dep = lockfile.dependencies.get(package)
        if dep is None:
            for key, d in lockfile.dependencies.items():
                if package in key or key.endswith(f"/{package}"):
                    dep = d
                    break

        if dep is not None:
            return dep.resolved_ref or "", dep.resolved_commit or "", dep.source or ""
    except Exception:
        pass
    return "", "", ""


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

        # Look up lockfile entry for ref/commit/source info
        locked_ref = ""
        locked_commit = ""
        locked_source = ""
        if project_root is not None:
            locked_ref, locked_commit, locked_source = _lookup_lockfile_ref(package, project_root)

        try:
            from rich.console import Console
            from rich.panel import Panel

            console = Console()

            content_lines = []
            content_lines.append(f"[bold]Name:[/bold] {package_info['name']}")
            content_lines.append(f"[bold]Version:[/bold] {package_info['version']}")
            content_lines.append(f"[bold]Description:[/bold] {package_info['description']}")
            content_lines.append(f"[bold]Author:[/bold] {package_info['author']}")
            content_lines.append(f"[bold]Source:[/bold] {locked_source or package_info['source']}")
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
            click.echo(f"Source: {locked_source or package_info['source']}")
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


def _display_registry_versions(
    package: str,
    dep_ref: DependencyReference,
    logger: CommandLogger,
    registry_name: str | None = None,
) -> None:
    """List available versions from the configured registry for a registry dep.

    *registry_name* forces a specific registry by name (from the merged
    registries map), bypassing the lockfile/default resolution below. An empty
    string or ``None`` falls back to the lockfile's recorded registry, then the
    configured default.
    """
    from ..deps.registry.auth import resolve_for_url
    from ..deps.registry.client import RegistryClient, RegistryError
    from ..deps.registry.config_loader import resolve_effective_registries

    repo_url = dep_ref.repo_url
    parts = [p for p in repo_url.split("/") if p]
    if len(parts) < 2:
        logger.error(f"Cannot parse owner/repo from '{package}'")
        sys.exit(1)
    owner = "/".join(parts[:-1]) if len(parts) > 2 else parts[0]
    repo = parts[-1]

    # Build the merged registries map (config.json + ~/.apm/apm.yml + project).
    project_registries = None
    project_default = None
    try:
        from ..models.apm_package import APMPackage

        apm_yml = Path(".") / "apm.yml"
        if apm_yml.is_file():
            pkg = APMPackage.from_apm_yml(apm_yml)
            project_registries = pkg.registries
            project_default = pkg.default_registry
    except Exception:
        pass

    registries, default_registry = resolve_effective_registries(project_registries, project_default)
    if registries is None:
        registries = {}

    # An explicit --registry NAME forces that registry, bypassing lockfile and
    # default resolution.
    explicit = registry_name or None
    registry_url = None
    if explicit:
        registry_url = registries.get(explicit)
        if not registry_url:
            known = ", ".join(sorted(registries)) or "none configured"
            logger.error(f"Registry '{explicit}' is not configured (known: {known})")
            sys.exit(1)

    # Prefer resolved_url from lockfile: it names the exact registry used for
    # this dep and works for non-default registries without any extra lookup.
    if registry_url is None:
        try:
            from ..deps.lockfile import LockFile, get_lockfile_path

            lf_path = get_lockfile_path(Path("."))
            lf = LockFile.read(lf_path)
            if lf:
                locked = lf.dependencies.get(package)
                if locked is None:
                    for key, d in lf.dependencies.items():
                        if package in key or key.endswith(f"/{package}"):
                            locked = d
                            break
                if locked and locked.resolved_url:
                    sep = "/v1/packages/"
                    idx = locked.resolved_url.find(sep)
                    if idx != -1:
                        registry_url = locked.resolved_url[:idx]
        except Exception:
            pass

    if registry_url is None:
        if not default_registry:
            logger.error(f"No registry configured; cannot list versions for '{package}'")
            sys.exit(1)
        registry_url = registries.get(default_registry) if registries else None
        if not registry_url:
            logger.error(f"Registry '{default_registry}' has no URL configured")
            sys.exit(1)

    try:
        auth = resolve_for_url(registry_url, registries or {})
        client = RegistryClient(registry_url, auth)
        versions = client.list_versions(owner, repo)
    except RegistryError as exc:
        logger.error(f"Failed to list versions for '{package}': {exc}")
        sys.exit(1)

    if not versions:
        logger.warning(f"No versions found for '{package}'. Verify the package name and registry.")
        return

    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(
            title=f"Available versions: {package}",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Version", style="bold white")
        table.add_column("Published", style="dim white")

        for entry in versions:
            table.add_row(entry.version, entry.published_at)

        console.print(table)

    except ImportError:
        click.echo(f"Available versions: {package}")
        click.echo("-" * 50)
        click.echo(f"{'Version':<20} {'Published':<30}")
        click.echo("-" * 50)
        for entry in versions:
            click.echo(f"{entry.version:<20} {entry.published_at:<30}")


def _is_explicit_git_form(package: str) -> bool:
    """True when *package* is an explicit git reference (URL or SCP form).

    Such references always route to the git path even when a default registry
    is configured -- they are the escape hatch for viewing git versions,
    mirroring the ``- git:`` manifest form.
    """
    p = package.strip().lower()
    return "://" in p or p.startswith("git@")


def _effective_default_registry(project_root: Path) -> str | None:
    """Return the effective default registry name (config.json + project apm.yml).

    Returns ``None`` when no default registry is configured or the registry
    feature is unavailable. Best-effort: any parsing/gate error is treated as
    "no default" so version lookups fall back to git.
    """
    from ..deps.registry.config_loader import resolve_effective_registries

    project_registries = None
    project_default = None
    try:
        apm_yml = project_root / "apm.yml"
        if apm_yml.is_file():
            from ..models.apm_package import APMPackage

            pkg = APMPackage.from_apm_yml(apm_yml)
            project_registries = pkg.registries
            project_default = pkg.default_registry
        _, default_registry = resolve_effective_registries(project_registries, project_default)
    except Exception:
        return None
    return default_registry


def display_versions(
    package: str,
    logger: CommandLogger,
    project_root: Path | None = None,
    registry: str | None = None,
) -> None:
    """Query and display available remote versions (tags/branches).

    This is a purely remote operation -- it does NOT require the package
    to be installed locally.  It parses *package* as a
    ``DependencyReference``, queries remote refs via
    ``GitHubPackageDownloader.list_remote_refs``, and renders the result
    as a Rich table (with a plain-text fallback).

    When *package* matches the ``NAME@MARKETPLACE`` pattern, the
    marketplace manifest is fetched instead and the plugin's marketplace
    metadata is displayed.

    Registry routing (highest precedence first):

    1. ``registry`` is not ``None`` -- ``--registry`` was passed: force the
       registry path. A non-empty value names the registry; ``""`` uses the
       lockfile/default. An explicit git URL still overrides nothing here --
       the flag wins.
    2. The lockfile records the package as ``source: registry``.
    3. A default registry is configured and *package* is a plain shorthand
       (not an explicit git URL) -- mirrors how ``apm install`` routes
       shorthand to the default registry.

    Otherwise the git path is used. *project_root* scopes the lockfile and
    apm.yml lookups; pass ``None`` (``--global``) to read from the cwd.
    """
    # -- Marketplace path: NAME@MARKETPLACE --
    from ..marketplace.resolver import parse_marketplace_ref

    marketplace_ref = parse_marketplace_ref(package)
    if marketplace_ref is not None:
        plugin_name, marketplace_name, _version_spec = marketplace_ref
        _display_marketplace_plugin(plugin_name, marketplace_name, logger)
        return

    # -- Git-based path --
    try:
        dep_ref = DependencyReference.parse(package)
    except ValueError as exc:
        logger.error(f"Invalid package reference '{package}': {exc}")
        sys.exit(1)

    # (1) Explicit --registry forces the registry path.
    if registry is not None:
        _display_registry_versions(package, dep_ref, logger, registry_name=registry or None)
        return

    # (2) Detect registry dep via lockfile and route to registry API.
    # Only consult the lockfile when we have a project root with an apm.yml;
    # this prevents mis-routing when the user is outside an APM project or
    # running with --global (project_root=None).
    _pr = project_root if project_root is not None else Path(".")
    if (_pr / "apm.yml").is_file():
        _, _, _locked_source = _lookup_lockfile_ref(package, _pr)
        if _locked_source == "registry":
            _display_registry_versions(package, dep_ref, logger)
            return

    # (3) No lockfile signal, but a default registry is configured and the
    # reference is a plain shorthand -- consult the registry, mirroring how
    # `apm install` routes unscoped shorthands to the default registry. An
    # explicit git URL is the escape hatch to force the git path below.
    if not _is_explicit_git_form(package) and _effective_default_registry(_pr):
        _display_registry_versions(package, dep_ref, logger)
        return

    try:
        downloader = GitHubPackageDownloader(auth_resolver=AuthResolver())
        refs: list[RemoteRef] = downloader.list_remote_refs(dep_ref)
    except RuntimeError as exc:
        logger.error(f"Failed to list versions for '{package}': {exc}")
        sys.exit(1)

    if not refs:
        logger.warning(f"No versions found for '{package}'. Verify the package name and remote.")
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


_VIEW_HELP = (
    "View package metadata or list remote versions.\n\n"
    "Without FIELD, displays local metadata for an installed package. "
    "With FIELD, queries specific data (may contact the remote).\n\n"
    "\b\n"
    "Fields:\n"
    "    versions    List available remote tags and branches\n\n"
    "\b\n"
    "Examples:\n"
    "    apm view org/repo                       # Local metadata\n"
    "    apm view org/repo versions              # Remote tags/branches (or registry)\n"
    "    apm view org/repo versions --registry   # From the default registry\n"
    "    apm view org/repo versions --registry myreg  # From a named registry\n"
    "    apm view https://github.com/org/repo versions  # Force the git path\n"
    "    apm view org/repo -g                    # From user scope"
)


@click.command(
    name="view",
    help=_VIEW_HELP,
    short_help="View package metadata or list remote versions",
)
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
@click.option(
    "--registry",
    "registry",
    is_flag=False,
    flag_value="",
    default=None,
    metavar="[NAME]",
    help="List versions from a registry (bare to use the default, or name one). "
    "Use a full git URL to force the git path instead.",
)
def view(package: str, field: str | None, global_: bool, registry: str | None):
    """View package metadata or list remote versions."""
    from ..core.scope import InstallScope, get_apm_dir

    logger = CommandLogger("view")

    # --- field validation (before any I/O) ---
    if field is not None:
        if field not in VALID_FIELDS:
            valid_list = ", ".join(VALID_FIELDS)
            logger.error(f"Unknown field '{field}'. Valid fields: {valid_list}")
            sys.exit(1)

        if field == "versions":
            display_versions(
                package,
                logger,
                project_root=None if global_ else Path("."),
                registry=registry,
            )
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
