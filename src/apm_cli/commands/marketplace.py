"""APM marketplace command group.

Manages plugin marketplace discovery and governance. Follows the same
Click group pattern as ``mcp.py``.
"""

import builtins
import json
import os
import subprocess
import sys
from pathlib import Path

import click
import yaml

from ..core.command_logger import CommandLogger
from ..marketplace.builder import BuildOptions, BuildReport, MarketplaceBuilder, ResolvedPackage
from ..marketplace.errors import (
    BuildError,
    GitLsRemoteError,
    HeadNotAllowedError,
    MarketplaceYmlError,
    NoMatchingVersionError,
    OfflineMissError,
    RefNotFoundError,
)
from ..marketplace.git_stderr import translate_git_stderr
from ..marketplace.pr_integration import PrIntegrator, PrResult, PrState
from ..marketplace.publisher import (
    ConsumerTarget,
    MarketplacePublisher,
    PublishOutcome,
    PublishPlan,
    TargetResult,
)
from ..marketplace.ref_resolver import RefResolver, RemoteRef
from ..marketplace.semver import SemVer, parse_semver, satisfies_range
from ..marketplace.yml_schema import load_marketplace_yml
from ..utils.path_security import PathTraversalError, validate_path_segments
from ._helpers import _get_console

# Restore builtins shadowed by subcommand names
list = builtins.list


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _load_yml_or_exit(logger):
    """Load ``./marketplace.yml`` from CWD or exit with an appropriate code.

    Returns the parsed ``MarketplaceYml`` on success.
    Calls ``sys.exit(1)`` on ``FileNotFoundError`` and
    ``sys.exit(2)`` on ``MarketplaceYmlError`` (schema/parse errors).
    """
    yml_path = Path.cwd() / "marketplace.yml"
    if not yml_path.exists():
        logger.error(
            "No marketplace.yml found. Run 'apm marketplace init' to scaffold one.",
            symbol="error",
        )
        sys.exit(1)
    try:
        return load_marketplace_yml(yml_path)
    except MarketplaceYmlError as exc:
        logger.error(f"marketplace.yml schema error: {exc}", symbol="error")
        sys.exit(2)


def _is_interactive():
    """Return True if both stdin and stdout are attached to a TTY.

    Centralised helper so that commands needing interactive confirmation
    can share a single detection point.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


@click.group(help="Manage plugin marketplaces for discovery and governance")
def marketplace():
    """Register, browse, and search plugin marketplaces."""
    pass


# ---------------------------------------------------------------------------
# marketplace init
# ---------------------------------------------------------------------------


@marketplace.command(help="Scaffold a new marketplace.yml in the current directory")
@click.option("--force", is_flag=True, help="Overwrite existing marketplace.yml")
@click.option(
    "--no-gitignore-check",
    is_flag=True,
    help="Skip the .gitignore staleness check",
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def init(force, no_gitignore_check, verbose):
    """Create a richly-commented marketplace.yml scaffold."""
    from ..marketplace.init_template import render_marketplace_yml_template

    logger = CommandLogger("marketplace-init", verbose=verbose)
    yml_path = Path.cwd() / "marketplace.yml"

    # Guard: file already exists
    if yml_path.exists() and not force:
        logger.error(
            "marketplace.yml already exists. Use --force to overwrite.",
            symbol="error",
        )
        sys.exit(1)

    # Write template
    template_text = render_marketplace_yml_template()
    try:
        yml_path.write_text(template_text, encoding="utf-8")
    except OSError as exc:
        logger.error(f"Failed to write marketplace.yml: {exc}", symbol="error")
        sys.exit(1)

    logger.success("Created marketplace.yml", symbol="check")

    if verbose:
        logger.verbose_detail(f"    Path: {yml_path}")

    # .gitignore staleness check
    if not no_gitignore_check:
        _check_gitignore_for_marketplace_json(logger)

    # Next steps panel
    next_steps = [
        "Edit marketplace.yml to add your packages",
        "Run 'apm marketplace build' to generate marketplace.json",
        "Commit BOTH marketplace.yml and marketplace.json",
    ]

    try:
        from ..utils.console import _rich_panel

        _rich_panel(
            "\n".join(f"  {i}. {step}" for i, step in enumerate(next_steps, 1)),
            title=" Next Steps",
            style="cyan",
        )
    except (ImportError, NameError):
        logger.progress("Next steps:")
        for i, step in enumerate(next_steps, 1):
            click.echo(f"  {i}. {step}")


def _check_gitignore_for_marketplace_json(logger):
    """Warn if .gitignore contains a rule that would ignore marketplace.json."""
    gitignore_path = Path.cwd() / ".gitignore"
    if not gitignore_path.exists():
        return

    try:
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    patterns = {"marketplace.json", "**/marketplace.json", "/marketplace.json"}
    for line in lines:
        stripped = line.strip()
        # Skip blank and commented lines
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in patterns:
            logger.warning(
                "Your .gitignore ignores marketplace.json. "
                "Both marketplace.yml and marketplace.json must be tracked "
                "in git. Remove the .gitignore rule.",
                symbol="warning",
            )
            return


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
# marketplace build
# ---------------------------------------------------------------------------


@marketplace.command(help="Build marketplace.json from marketplace.yml")
@click.option("--dry-run", is_flag=True, help="Preview without writing marketplace.json")
@click.option("--offline", is_flag=True, help="Use cached refs only (no network)")
@click.option(
    "--include-prerelease", is_flag=True, help="Include prerelease versions"
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def build(dry_run, offline, include_prerelease, verbose):
    """Resolve packages and compile marketplace.json."""
    logger = CommandLogger("marketplace-build", verbose=verbose)
    yml_path = Path.cwd() / "marketplace.yml"

    # Load yml (exit 1 on missing, exit 2 on schema error)
    _load_yml_or_exit(logger)

    try:
        opts = BuildOptions(
            dry_run=dry_run,
            offline=offline,
            include_prerelease=include_prerelease,
        )
        builder = MarketplaceBuilder(yml_path, options=opts)
        report = builder.build()
    except MarketplaceYmlError as exc:
        logger.error(f"marketplace.yml schema error: {exc}", symbol="error")
        sys.exit(2)
    except BuildError as exc:
        _render_build_error(logger, exc)
        sys.exit(1)

    # Render results table
    _render_build_table(logger, report)

    if dry_run:
        logger.progress(
            "Dry run -- marketplace.json not written", symbol="info"
        )
    else:
        logger.success(
            f"Built marketplace.json ({len(report.resolved)} packages)",
            symbol="check",
        )


def _render_build_error(logger, exc):
    """Render a BuildError with actionable hints."""
    if isinstance(exc, GitLsRemoteError):
        logger.error(exc.summary_text, symbol="error")
        if exc.hint:
            logger.progress(f"Hint: {exc.hint}", symbol="info")
    elif isinstance(exc, NoMatchingVersionError):
        logger.error(str(exc), symbol="error")
        logger.progress(
            "Check that your version range matches published tags.",
            symbol="info",
        )
    elif isinstance(exc, RefNotFoundError):
        logger.error(str(exc), symbol="error")
        logger.progress(
            "Verify the ref is spelled correctly and the remote is reachable.",
            symbol="info",
        )
    elif isinstance(exc, HeadNotAllowedError):
        logger.error(str(exc), symbol="error")
    elif isinstance(exc, OfflineMissError):
        logger.error(str(exc), symbol="error")
        logger.progress(
            "Run a build online first to populate the cache.",
            symbol="info",
        )
    else:
        logger.error(f"Build failed: {exc}", symbol="error")


def _render_build_table(logger, report):
    """Render the resolved-packages table (Rich with colorama fallback)."""
    console = _get_console()
    if not console:
        # Colorama fallback
        for pkg in report.resolved:
            sha_short = pkg.sha[:8] if pkg.sha else "--"
            ref_kind = "tag" if not pkg.ref.startswith("refs/heads/") else "branch"
            logger.tree_item(
                f"  [+] {pkg.name}  {pkg.ref}  {sha_short}  ({ref_kind})"
            )
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Resolved Packages",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Status", style="green", no_wrap=True, width=6)
    table.add_column("Package", style="bold white", no_wrap=True)
    table.add_column("Version", style="cyan")
    table.add_column("Commit", style="dim")
    table.add_column("Ref Kind", style="white")

    for pkg in report.resolved:
        sha_short = pkg.sha[:8] if pkg.sha else "--"
        # Determine ref kind
        ref_kind = "tag"
        if pkg.ref and not parse_semver(pkg.ref.lstrip("vV")):
            ref_kind = "ref"
        table.add_row(Text("[+]"), pkg.name, pkg.ref, sha_short, ref_kind)

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# marketplace outdated
# ---------------------------------------------------------------------------


@marketplace.command(help="Show packages with available upgrades")
@click.option("--offline", is_flag=True, help="Use cached refs only (no network)")
@click.option(
    "--include-prerelease", is_flag=True, help="Include prerelease versions"
)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def outdated(offline, include_prerelease, verbose):
    """Compare installed versions against latest available tags."""
    logger = CommandLogger("marketplace-outdated", verbose=verbose)

    yml = _load_yml_or_exit(logger)

    # Load current marketplace.json for "Current" column
    current_versions = _load_current_versions()

    resolver = RefResolver(offline=offline)
    try:
        rows = []
        upgradable = 0
        for entry in yml.packages:
            # Entries with explicit ref (no range) are skipped
            if entry.ref is not None:
                rows.append(_OutdatedRow(
                    name=entry.name,
                    current=current_versions.get(entry.name, "--"),
                    range_spec="--",
                    latest_in_range="--",
                    latest_overall="--",
                    status="[i]",
                    note="Pinned to ref; skipped",
                ))
                continue

            version_range = entry.version or ""
            if not version_range:
                rows.append(_OutdatedRow(
                    name=entry.name,
                    current=current_versions.get(entry.name, "--"),
                    range_spec="--",
                    latest_in_range="--",
                    latest_overall="--",
                    status="[i]",
                    note="No version range",
                ))
                continue

            try:
                refs = resolver.list_remote_refs(entry.source)
            except (BuildError, Exception) as exc:
                rows.append(_OutdatedRow(
                    name=entry.name,
                    current=current_versions.get(entry.name, "--"),
                    range_spec=version_range,
                    latest_in_range="--",
                    latest_overall="--",
                    status="[x]",
                    note=str(exc)[:60],
                ))
                continue

            # Parse tags into semvers
            tag_versions = _extract_tag_versions(
                refs, entry, yml, include_prerelease
            )

            if not tag_versions:
                rows.append(_OutdatedRow(
                    name=entry.name,
                    current=current_versions.get(entry.name, "--"),
                    range_spec=version_range,
                    latest_in_range="--",
                    latest_overall="--",
                    status="[!]",
                    note="No matching tags found",
                ))
                continue

            # Find highest in-range and highest overall
            in_range = [
                (sv, tag) for sv, tag in tag_versions
                if satisfies_range(sv, version_range)
            ]
            latest_overall_sv, latest_overall_tag = max(
                tag_versions, key=lambda x: x[0]
            )
            latest_in_range_tag = "--"
            if in_range:
                _, latest_in_range_tag = max(in_range, key=lambda x: x[0])

            current = current_versions.get(entry.name, "--")

            # Determine status
            if current == latest_in_range_tag:
                status = "[+]"
            elif latest_in_range_tag != "--" and current != latest_in_range_tag:
                status = "[!]"
                upgradable += 1
            else:
                status = "[!]"
                upgradable += 1

            # Check if major upgrade available outside range
            if latest_overall_tag != latest_in_range_tag:
                status = "[*]"

            rows.append(_OutdatedRow(
                name=entry.name,
                current=current,
                range_spec=version_range,
                latest_in_range=latest_in_range_tag,
                latest_overall=latest_overall_tag,
                status=status,
                note="",
            ))

        _render_outdated_table(logger, rows)

        if verbose:
            logger.verbose_detail(f"    {upgradable} upgradable entries")

    finally:
        resolver.close()


class _OutdatedRow:
    """Simple container for outdated table row data."""

    __slots__ = (
        "name", "current", "range_spec", "latest_in_range",
        "latest_overall", "status", "note",
    )

    def __init__(self, name, current, range_spec, latest_in_range,
                 latest_overall, status, note):
        self.name = name
        self.current = current
        self.range_spec = range_spec
        self.latest_in_range = latest_in_range
        self.latest_overall = latest_overall
        self.status = status
        self.note = note


def _load_current_versions():
    """Load current ref versions from marketplace.json if present."""
    mkt_path = Path.cwd() / "marketplace.json"
    if not mkt_path.exists():
        return {}
    try:
        data = json.loads(mkt_path.read_text(encoding="utf-8"))
        result = {}
        for plugin in data.get("plugins", []):
            name = plugin.get("name", "")
            src = plugin.get("source", {})
            if isinstance(src, dict):
                result[name] = src.get("ref", "--")
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def _extract_tag_versions(refs, entry, yml, include_prerelease):
    """Extract (SemVer, tag_name) pairs from remote refs for a package entry."""
    from ..marketplace.tag_pattern import build_tag_regex

    pattern = entry.tag_pattern or yml.build.tag_pattern
    tag_rx = build_tag_regex(pattern)
    results = []
    for remote_ref in refs:
        if not remote_ref.name.startswith("refs/tags/"):
            continue
        tag_name = remote_ref.name[len("refs/tags/"):]
        m = tag_rx.match(tag_name)
        if not m:
            continue
        version_str = m.group("version")
        sv = parse_semver(version_str)
        if sv is None:
            continue
        if sv.is_prerelease and not (include_prerelease or entry.include_prerelease):
            continue
        results.append((sv, tag_name))
    return results


def _render_outdated_table(logger, rows):
    """Render the outdated-packages table."""
    console = _get_console()
    if not console:
        for row in rows:
            note = f"  ({row.note})" if row.note else ""
            logger.tree_item(
                f"  {row.status} {row.name}  current={row.current}  "
                f"latest-in-range={row.latest_in_range}  "
                f"latest={row.latest_overall}{note}"
            )
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Package Version Status",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Status", style="green", no_wrap=True, width=6)
    table.add_column("Package", style="bold white", no_wrap=True)
    table.add_column("Current", style="white")
    table.add_column("Range", style="dim")
    table.add_column("Latest in Range", style="cyan")
    table.add_column("Latest Overall", style="yellow")

    for row in rows:
        note = ""
        if row.note:
            note = f" ({row.note})"
        table.add_row(
            Text(row.status),
            row.name,
            row.current,
            row.range_spec,
            row.latest_in_range + note,
            row.latest_overall,
        )

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# marketplace check
# ---------------------------------------------------------------------------


@marketplace.command(help="Validate marketplace.yml entries are resolvable")
@click.option("--offline", is_flag=True, help="Schema + cached-ref checks only (no network)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def check(offline, verbose):
    """Validate marketplace.yml and check each entry is resolvable."""
    logger = CommandLogger("marketplace-check", verbose=verbose)

    yml = _load_yml_or_exit(logger)

    if offline:
        logger.progress(
            "Offline mode -- only schema and cached-ref checks",
            symbol="info",
        )

    resolver = RefResolver(offline=offline)
    results = []
    failure_count = 0

    try:
        for entry in yml.packages:
            try:
                # Attempt to resolve each entry
                refs = resolver.list_remote_refs(entry.source)

                # Check version/ref resolution
                ref_ok = False
                if entry.ref is not None:
                    # Check the explicit ref exists
                    for r in refs:
                        tag_name = r.name
                        if tag_name.startswith("refs/tags/"):
                            tag_name = tag_name[len("refs/tags/"):]
                        elif tag_name.startswith("refs/heads/"):
                            tag_name = tag_name[len("refs/heads/"):]
                        if tag_name == entry.ref or r.name == entry.ref:
                            ref_ok = True
                            break
                    if not ref_ok:
                        results.append(_CheckResult(
                            name=entry.name, reachable=True,
                            version_found=False, ref_ok=False,
                            error=f"Ref '{entry.ref}' not found",
                        ))
                        failure_count += 1
                        continue
                else:
                    # Version range -- check at least one tag satisfies
                    tag_versions = _extract_tag_versions(
                        refs, entry, yml, False
                    )
                    version_range = entry.version or ""
                    matching = [
                        (sv, tag) for sv, tag in tag_versions
                        if satisfies_range(sv, version_range)
                    ]
                    if matching:
                        ref_ok = True
                    else:
                        results.append(_CheckResult(
                            name=entry.name, reachable=True,
                            version_found=len(tag_versions) > 0,
                            ref_ok=False,
                            error=f"No tag matching '{version_range}'",
                        ))
                        failure_count += 1
                        continue

                results.append(_CheckResult(
                    name=entry.name, reachable=True,
                    version_found=True, ref_ok=True, error="",
                ))

            except OfflineMissError:
                results.append(_CheckResult(
                    name=entry.name, reachable=False,
                    version_found=False, ref_ok=False,
                    error="No cached refs (offline)",
                ))
                failure_count += 1
            except GitLsRemoteError as exc:
                results.append(_CheckResult(
                    name=entry.name, reachable=False,
                    version_found=False, ref_ok=False,
                    error=exc.summary_text[:60],
                ))
                failure_count += 1
            except Exception as exc:
                results.append(_CheckResult(
                    name=entry.name, reachable=False,
                    version_found=False, ref_ok=False,
                    error=str(exc)[:60],
                ))
                failure_count += 1

        _render_check_table(logger, results)

        total = len(results)
        if failure_count > 0:
            logger.error(
                f"{failure_count} entries have issues", symbol="error"
            )
            sys.exit(1)
        else:
            logger.success(
                f"All {total} entries OK", symbol="check"
            )

    finally:
        resolver.close()


class _CheckResult:
    """Container for per-entry check results."""

    __slots__ = ("name", "reachable", "version_found", "ref_ok", "error")

    def __init__(self, name, reachable, version_found, ref_ok, error):
        self.name = name
        self.reachable = reachable
        self.version_found = version_found
        self.ref_ok = ref_ok
        self.error = error


def _render_check_table(logger, results):
    """Render the check-results table."""
    console = _get_console()
    if not console:
        for r in results:
            icon = "[+]" if r.ref_ok else "[x]"
            detail = r.error if r.error else "OK"
            logger.tree_item(f"  {icon} {r.name}: {detail}")
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Entry Health Check",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Status", no_wrap=True, width=6)
    table.add_column("Package", style="bold white", no_wrap=True)
    table.add_column("Reachable", style="white", justify="centre")
    table.add_column("Version Found", style="white", justify="centre")
    table.add_column("Ref OK", style="white", justify="centre")
    table.add_column("Detail", style="dim")

    for r in results:
        reach = "[+]" if r.reachable else "[x]"
        ver = "[+]" if r.version_found else "[x]"
        ref = "[+]" if r.ref_ok else "[x]"
        detail = r.error if r.error else "OK"
        table.add_row(
            Text("[+]" if r.ref_ok else "[x]"),
            r.name,
            Text(reach),
            Text(ver),
            Text(ref),
            detail,
        )

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# marketplace doctor
# ---------------------------------------------------------------------------


@marketplace.command(help="Run environment diagnostics for marketplace builds")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def doctor(verbose):
    """Check git, network, auth, and marketplace.yml readiness."""
    logger = CommandLogger("marketplace-doctor", verbose=verbose)
    checks = []

    # Check 1: git on PATH
    git_ok = False
    git_detail = ""
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            git_ok = True
            git_detail = result.stdout.strip()
        else:
            git_detail = "git returned non-zero exit code"
    except FileNotFoundError:
        git_detail = "git not found on PATH"
    except subprocess.TimeoutExpired:
        git_detail = "git --version timed out"
    except Exception as exc:
        git_detail = str(exc)[:60]

    checks.append(_DoctorCheck(
        name="git",
        passed=git_ok,
        detail=git_detail,
    ))

    # Check 2: network reachability
    net_ok = False
    net_detail = ""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "https://github.com/git/git.git", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            net_ok = True
            net_detail = "github.com reachable"
        else:
            translated = translate_git_stderr(
                result.stderr,
                exit_code=result.returncode,
                operation="ls-remote",
                remote="github.com",
            )
            net_detail = translated.hint[:80]
    except subprocess.TimeoutExpired:
        net_detail = "Network check timed out (5s)"
    except FileNotFoundError:
        net_detail = "git not found; cannot test network"
    except Exception as exc:
        net_detail = str(exc)[:60]

    checks.append(_DoctorCheck(
        name="network",
        passed=net_ok,
        detail=net_detail,
    ))

    # Check 3: auth tokens
    has_token = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    auth_detail = "Token detected" if has_token else "No token; unauthenticated rate limits apply"
    checks.append(_DoctorCheck(
        name="auth",
        passed=True,  # informational; never fails
        detail=auth_detail,
        informational=True,
    ))

    # Check 4: marketplace.yml presence + parsability
    yml_path = Path.cwd() / "marketplace.yml"
    yml_found = yml_path.exists()
    yml_detail = ""
    yml_parsed = False
    if yml_found:
        try:
            load_marketplace_yml(yml_path)
            yml_parsed = True
            yml_detail = "marketplace.yml found and valid"
        except MarketplaceYmlError as exc:
            yml_detail = f"marketplace.yml has errors: {str(exc)[:60]}"
    else:
        yml_detail = "No marketplace.yml in current directory"

    checks.append(_DoctorCheck(
        name="marketplace.yml",
        passed=yml_parsed if yml_found else True,  # informational if absent
        detail=yml_detail,
        informational=True,
    ))

    _render_doctor_table(logger, checks)

    # Exit: 0 if checks 1-3 pass; check 4 is informational
    critical_checks = [c for c in checks if not c.informational]
    if any(not c.passed for c in critical_checks):
        sys.exit(1)


class _DoctorCheck:
    """Container for a single doctor check result."""

    __slots__ = ("name", "passed", "detail", "informational")

    def __init__(self, name, passed, detail, informational=False):
        self.name = name
        self.passed = passed
        self.detail = detail
        self.informational = informational


def _render_doctor_table(logger, checks):
    """Render the doctor results table."""
    console = _get_console()
    if not console:
        for c in checks:
            if c.informational:
                icon = "[i]"
            elif c.passed:
                icon = "[+]"
            else:
                icon = "[x]"
            logger.tree_item(f"  {icon} {c.name}: {c.detail}")
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Environment Diagnostics",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Check", style="bold white", no_wrap=True)
    table.add_column("Status", no_wrap=True, width=6)
    table.add_column("Detail", style="white")

    for c in checks:
        if c.informational:
            icon = "[i]"
        elif c.passed:
            icon = "[+]"
        else:
            icon = "[x]"
        table.add_row(c.name, Text(icon), c.detail)

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# marketplace publish
# ---------------------------------------------------------------------------


def _load_targets_file(path):
    """Load and validate a consumer-targets YAML file.

    Returns a list of ``ConsumerTarget`` instances.

    Raises ``SystemExit`` on validation failures.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return None, f"Invalid YAML in targets file: {exc}"
    except OSError as exc:
        return None, f"Cannot read targets file: {exc}"

    if not isinstance(raw, dict) or "targets" not in raw:
        return None, "Targets file must contain a 'targets' key."

    raw_targets = raw["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        return None, "Targets file must contain a non-empty 'targets' list."

    targets = []
    for idx, entry in enumerate(raw_targets):
        if not isinstance(entry, dict):
            return None, f"targets[{idx}] must be a mapping."

        repo = entry.get("repo")
        if not repo or not isinstance(repo, str):
            return None, f"targets[{idx}]: 'repo' is required (owner/name)."

        # Validate repo format: owner/name
        parts = repo.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None, f"targets[{idx}]: 'repo' must be 'owner/name', got '{repo}'."

        branch = entry.get("branch")
        if not branch or not isinstance(branch, str):
            return None, f"targets[{idx}]: 'branch' is required."

        path_in_repo = entry.get("path_in_repo", "apm.yml")
        if not isinstance(path_in_repo, str) or not path_in_repo.strip():
            return None, f"targets[{idx}]: 'path_in_repo' must be a non-empty string."

        # Path safety check
        try:
            validate_path_segments(
                path_in_repo,
                context=f"targets[{idx}].path_in_repo",
            )
        except PathTraversalError as exc:
            return None, str(exc)

        targets.append(ConsumerTarget(
            repo=repo.strip(),
            branch=branch.strip(),
            path_in_repo=path_in_repo.strip(),
        ))

    return targets, None


@marketplace.command(help="Publish marketplace updates to consumer repositories")
@click.option(
    "--targets",
    "targets_file",
    default=None,
    type=click.Path(exists=False),
    help="Path to consumer-targets YAML file (default: ./consumer-targets.yml)",
)
@click.option("--dry-run", is_flag=True, help="Preview without pushing or opening PRs")
@click.option("--no-pr", is_flag=True, help="Push branches but skip PR creation")
@click.option("--draft", is_flag=True, help="Create PRs as drafts")
@click.option("--allow-downgrade", is_flag=True, help="Allow version downgrades")
@click.option("--allow-ref-change", is_flag=True, help="Allow switching ref types")
@click.option(
    "--parallel",
    default=4,
    show_default=True,
    type=int,
    help="Maximum number of concurrent target updates",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def publish(
    targets_file,
    dry_run,
    no_pr,
    draft,
    allow_downgrade,
    allow_ref_change,
    parallel,
    yes,
    verbose,
):
    """Publish marketplace updates to consumer repositories."""
    logger = CommandLogger("marketplace-publish", verbose=verbose)

    # ------------------------------------------------------------------
    # 1. Pre-flight checks
    # ------------------------------------------------------------------

    # 1a. Load marketplace.yml
    yml = _load_yml_or_exit(logger)

    # 1b. Load marketplace.json
    mkt_json_path = Path.cwd() / "marketplace.json"
    if not mkt_json_path.exists():
        logger.error(
            "marketplace.json not found. Run 'apm marketplace build' first.",
            symbol="error",
        )
        sys.exit(1)

    # 1c. Load targets
    if targets_file:
        targets_path = Path(targets_file)
        if not targets_path.exists():
            logger.error(
                f"Targets file not found: {targets_file}",
                symbol="error",
            )
            sys.exit(1)
    else:
        targets_path = Path.cwd() / "consumer-targets.yml"
        if not targets_path.exists():
            logger.error(
                "No consumer-targets.yml found. "
                "Create one or pass --targets <path>.\n"
                "\n"
                "Example consumer-targets.yml:\n"
                "  targets:\n"
                "    - repo: acme-org/service-a\n"
                "      branch: main\n"
                "    - repo: acme-org/service-b\n"
                "      branch: develop",
                symbol="error",
            )
            sys.exit(1)

    targets, error = _load_targets_file(targets_path)
    if error:
        logger.error(error, symbol="error")
        sys.exit(1)

    # 1d. Check gh availability (unless --no-pr)
    if not no_pr:
        pr = PrIntegrator()
        available, hint = pr.check_available()
        if not available:
            logger.error(hint, symbol="error")
            sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Plan and confirm
    # ------------------------------------------------------------------

    publisher = MarketplacePublisher(Path.cwd())
    plan = publisher.plan(
        targets,
        allow_downgrade=allow_downgrade,
        allow_ref_change=allow_ref_change,
    )

    # Render publish plan
    _render_publish_plan(logger, plan)

    # Confirmation logic
    if not yes:
        if not _is_interactive():
            logger.error(
                "Non-interactive session: pass --yes to confirm the publish.",
                symbol="error",
            )
            sys.exit(1)
        answer = click.prompt(
            f"Confirm publish to {len(targets)} repositories? [y/N]",
            default="N",
            show_default=False,
        )
        if answer.strip().lower() != "y":
            logger.progress("Publish aborted by user.", symbol="info")
            return

    if dry_run:
        logger.progress(
            "Dry run: no branches will be pushed and no PRs will be opened.",
            symbol="info",
        )

    # ------------------------------------------------------------------
    # 3. Execute publish
    # ------------------------------------------------------------------

    results = publisher.execute(plan, dry_run=dry_run, parallel=parallel)

    # PR integration
    pr_results = []
    if not no_pr:
        if not locals().get("pr"):
            pr = PrIntegrator()

        for result in results:
            if dry_run:
                # In dry-run, preview what PR would do for UPDATED targets
                if result.outcome == PublishOutcome.UPDATED:
                    pr_result = pr.open_or_update(
                        plan,
                        result.target,
                        result,
                        no_pr=False,
                        draft=draft,
                        dry_run=True,
                    )
                    pr_results.append(pr_result)
                else:
                    pr_results.append(PrResult(
                        target=result.target,
                        state=PrState.SKIPPED,
                        pr_number=None,
                        pr_url=None,
                        message=f"No PR needed: {result.outcome.value}",
                    ))
            else:
                if result.outcome == PublishOutcome.UPDATED:
                    pr_result = pr.open_or_update(
                        plan,
                        result.target,
                        result,
                        no_pr=False,
                        draft=draft,
                        dry_run=False,
                    )
                    pr_results.append(pr_result)
                else:
                    pr_results.append(PrResult(
                        target=result.target,
                        state=PrState.SKIPPED,
                        pr_number=None,
                        pr_url=None,
                        message=f"No PR needed: {result.outcome.value}",
                    ))

    # ------------------------------------------------------------------
    # 4. Summary rendering
    # ------------------------------------------------------------------

    _render_publish_summary(logger, results, pr_results, no_pr, dry_run)

    # State file path -- use soft_wrap so the path is never split mid-word
    # in narrow terminals (Rich would otherwise break at hyphens).
    state_path = Path.cwd() / ".apm" / "publish-state.json"
    from rich.text import Text

    console = _get_console()
    console.print(
        Text(f"[i] State file: {state_path}", no_wrap=True),
        style="blue",
        highlight=False,
        soft_wrap=True,
    )

    # Exit code
    failed_count = sum(
        1 for r in results if r.outcome == PublishOutcome.FAILED
    )
    if failed_count > 0:
        sys.exit(1)


def _render_publish_plan(logger, plan):
    """Render the publish plan as a Rich panel + target table."""
    console = _get_console()

    plan_text = (
        f"Marketplace: {plan.marketplace_name}\n"
        f"New version: {plan.marketplace_version}\n"
        f"New ref:     {plan.new_ref}\n"
        f"Branch:      {plan.branch_name}\n"
        f"Targets:     {len(plan.targets)}"
    )

    if not console:
        logger.progress("Publish plan:", symbol="info")
        for line in plan_text.splitlines():
            click.echo(f"  {line}")
        click.echo()
        for t in plan.targets:
            logger.tree_item(
                f"  [*] {t.repo}  branch={t.branch}  path={t.path_in_repo}"
            )
        return

    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console.print()
    console.print(Panel(
        plan_text,
        title="Publish plan",
        border_style="cyan",
    ))

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Repo", style="bold white", no_wrap=True)
    table.add_column("Branch", style="cyan")
    table.add_column("Path", style="dim")
    table.add_column("Status", no_wrap=True, width=10)

    for t in plan.targets:
        table.add_row(t.repo, t.branch, t.path_in_repo, Text("[*]"))

    console.print(table)
    console.print()


def _render_publish_summary(logger, results, pr_results, no_pr, dry_run):
    """Render the final publish summary table."""
    console = _get_console()

    # Build lookup for PR results by repo
    pr_by_repo = {}
    for pr_r in pr_results:
        pr_by_repo[pr_r.target.repo] = pr_r

    updated_count = sum(
        1 for r in results if r.outcome == PublishOutcome.UPDATED
    )
    failed_count = sum(
        1 for r in results if r.outcome == PublishOutcome.FAILED
    )
    total = len(results)

    if not console:
        click.echo()
        for r in results:
            icon = _outcome_symbol(r.outcome)
            pr_info = ""
            if not no_pr:
                pr_r = pr_by_repo.get(r.target.repo)
                if pr_r:
                    pr_info = f"  PR: {pr_r.state.value}"
                    if pr_r.pr_number:
                        pr_info += f" #{pr_r.pr_number}"
            logger.tree_item(
                f"  {icon} {r.target.repo}: {r.outcome.value}{pr_info} -- {r.message}"
            )
        click.echo()
        _render_publish_footer(logger, updated_count, failed_count, total, dry_run)
        return

    from rich.table import Table
    from rich.text import Text

    table = Table(
        title="Publish Results",
        show_header=True,
        header_style="bold cyan",
        border_style="cyan",
    )
    table.add_column("Status", no_wrap=True, width=6)
    table.add_column("Repo", style="bold white", no_wrap=True)
    table.add_column("Outcome", style="white")

    if not no_pr:
        table.add_column("PR State", style="white")
        table.add_column("PR #", style="cyan", justify="right")
        table.add_column("PR URL", style="dim")

    table.add_column("Message", style="dim", ratio=1)

    for r in results:
        icon = _outcome_symbol(r.outcome)
        row = [Text(icon), r.target.repo, r.outcome.value]

        if not no_pr:
            pr_r = pr_by_repo.get(r.target.repo)
            if pr_r:
                row.append(pr_r.state.value)
                row.append(str(pr_r.pr_number) if pr_r.pr_number else "--")
                row.append(pr_r.pr_url or "--")
            else:
                row.extend(["--", "--", "--"])

        row.append(r.message)
        table.add_row(*row)

    console.print()
    console.print(table)
    console.print()

    _render_publish_footer(logger, updated_count, failed_count, total, dry_run)


def _outcome_symbol(outcome):
    """Map a ``PublishOutcome`` to a bracket symbol."""
    if outcome == PublishOutcome.UPDATED:
        return "[+]"
    elif outcome == PublishOutcome.FAILED:
        return "[x]"
    elif outcome in (
        PublishOutcome.SKIPPED_DOWNGRADE,
        PublishOutcome.SKIPPED_REF_CHANGE,
    ):
        return "[!]"
    elif outcome == PublishOutcome.NO_CHANGE:
        return "[*]"
    return "[*]"


def _render_publish_footer(logger, updated, failed, total, dry_run):
    """Render the footer success/warning line."""
    suffix = " (dry-run)" if dry_run else ""
    if failed == 0:
        logger.success(
            f"Published {updated}/{total} targets{suffix}",
            symbol="check",
        )
    else:
        logger.warning(
            f"Published {updated}/{total} targets, "
            f"{failed} failed{suffix}",
            symbol="warning",
        )


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

