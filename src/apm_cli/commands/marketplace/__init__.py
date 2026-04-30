"""Marketplace CLI package.

This package keeps click group wiring, shared helpers, and compatibility
exports for the marketplace command surface.
"""

from __future__ import annotations

import builtins
import json
import os  # noqa: F401
import re
import sys
import traceback
from pathlib import Path

import click
import yaml

from ...core.command_logger import CommandLogger
from ...marketplace.builder import BuildOptions, BuildReport, MarketplaceBuilder, ResolvedPackage
from ...marketplace.errors import (
    BuildError,
    GitLsRemoteError,
    HeadNotAllowedError,
    MarketplaceNotFoundError,
    MarketplaceYmlError,
    NoMatchingVersionError,
    OfflineMissError,
    RefNotFoundError,
)
from ...marketplace.git_stderr import translate_git_stderr
from ...marketplace.migration import (
    ConfigSource,
    detect_config_source,
    load_marketplace_config,
    migrate_marketplace_yml,
)
from ...marketplace.pr_integration import PrIntegrator, PrResult, PrState
from ...marketplace.publisher import (
    ConsumerTarget,
    MarketplacePublisher,
    PublishOutcome,
    PublishPlan,
    TargetResult,
)
from ...marketplace.ref_resolver import RefResolver, RemoteRef
from ...marketplace.semver import SemVer, parse_semver, satisfies_range
from ...marketplace.yml_schema import load_marketplace_yml
from ...utils.console import _rich_info, _rich_warning  # noqa: F401
from ...utils.path_security import PathTraversalError, validate_path_segments
from .._helpers import _get_console, _is_interactive

# Restore builtins shadowed by subcommand names
list = builtins.list


# Marketplace alias must satisfy this pattern so it can appear on the right of
# ``@`` in ``apm install <plugin>@<marketplace>`` syntax.
_ALIAS_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


def _is_valid_alias(value: str) -> bool:
    """Return True when ``value`` is a legal marketplace alias."""
    return bool(value) and _ALIAS_PATTERN.match(value) is not None


# ---------------------------------------------------------------------------
# Custom group for organised --help output
# ---------------------------------------------------------------------------


class MarketplaceGroup(click.Group):
    """Custom group that organises commands by audience."""

    _consumer_commands = [  # noqa: RUF012
        "add",
        "list",
        "browse",
        "update",
        "remove",
        "validate",
    ]
    _authoring_commands = [  # noqa: RUF012
        "init",
        "check",
        "outdated",
        "doctor",
        "publish",
        "package",
        "migrate",
    ]

    def get_command(self, ctx, cmd_name):
        # The 'build' subcommand was removed in favour of the unified
        # 'apm pack' entrypoint. Surface a hard error with a migration
        # hint rather than silently aliasing.
        if cmd_name == "build":
            raise click.UsageError(
                "'apm marketplace build' was removed. Use 'apm pack' instead.\n"
                "marketplace.json is now produced by 'apm pack' when "
                "apm.yml has a 'marketplace:' block."
            )
        return super().get_command(ctx, cmd_name)

    def format_commands(self, ctx, formatter):
        sections = [
            ("Consumer commands", self._consumer_commands),
            ("Authoring commands", self._authoring_commands),
        ]

        for section_name, cmd_names in sections:
            commands = []
            for name in cmd_names:
                cmd = self.get_command(ctx, name)
                if cmd is None:
                    continue
                help_text = cmd.get_short_help_str(limit=150)
                commands.append((name, help_text))
            if commands:
                with formatter.section(section_name):
                    formatter.write_dl(commands)


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


def _load_config_or_exit(logger):
    """Load the marketplace config from CWD (apm.yml or marketplace.yml).

    Returns ``(project_root, config)``. Exits with code 1 when no config
    is found or both files coexist; exits with code 2 on validation errors.
    Emits a deprecation warning when the legacy file is in use.
    """
    project_root = Path.cwd()
    try:
        config = load_marketplace_config(
            project_root,
            warn_callback=lambda msg: logger.warning(msg, symbol="warning"),
        )
    except MarketplaceYmlError as exc:
        msg = str(exc)
        if msg.startswith("No marketplace config"):
            logger.error(msg, symbol="error")
            sys.exit(1)
        if msg.startswith("Both apm.yml"):
            logger.error(msg, symbol="error")
            sys.exit(1)
        logger.error(f"marketplace config error: {exc}", symbol="error")
        sys.exit(2)
    return project_root, config


def _warn_duplicate_names(logger, yml):
    """Emit a warning for each duplicate package name in *yml*."""
    seen: dict[str, int] = {}
    for idx, entry in enumerate(yml.packages):
        lower = entry.name.lower()
        if lower in seen:
            logger.warning(
                f"Duplicate package name '{entry.name}' "
                f"(packages[{seen[lower]}] and packages[{idx}]). "
                f"Consumers will see duplicate entries in browse.",
                symbol="warning",
            )
        else:
            seen[lower] = idx


def _find_duplicate_names(yml):
    """Return a diagnostic string if *yml* contains duplicate package names."""
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for idx, entry in enumerate(yml.packages):
        lower = entry.name.lower()
        if lower in seen:
            duplicates.append(f"'{entry.name}' (packages[{seen[lower]}] and packages[{idx}])")
        else:
            seen[lower] = idx
    if duplicates:
        return f"Duplicate names: {', '.join(duplicates)}"
    return ""


@click.group(cls=MarketplaceGroup, help="Manage marketplaces for discovery and governance")
@click.pass_context
def marketplace(ctx):
    """Register, browse, and search marketplaces."""


from .plugin import package  # noqa: E402

marketplace.add_command(package)


def _check_gitignore_for_marketplace_json(logger):
    """Warn if .gitignore contains a rule that would ignore marketplace.json."""
    gitignore_path = Path.cwd() / ".gitignore"
    if not gitignore_path.exists():
        return

    try:
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    patterns = {"marketplace.json", "**/marketplace.json", "/marketplace.json", "*.json"}
    for line in lines:
        stripped = line.strip()
        # Skip blank and commented lines
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in patterns:
            logger.warning(
                "Your .gitignore ignores marketplace.json. "
                "Both apm.yml and the generated marketplace.json must be "
                "tracked in git. Remove the .gitignore rule.",
                symbol="warning",
            )
            return


@marketplace.command(help="Register a marketplace")
@click.argument("repo", required=True)
@click.option("--name", "-n", default=None, help="Display name (defaults to repo name)")
@click.option("--branch", "-b", default="main", show_default=True, help="Branch to use")
@click.option("--host", default=None, help="Git host FQDN (default: github.com)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(repo, name, branch, host, verbose):
    """Register a marketplace from OWNER/REPO or HOST/OWNER/REPO."""
    logger = CommandLogger("marketplace-add", verbose=verbose)
    try:
        from ...marketplace.client import _auto_detect_path, fetch_marketplace
        from ...marketplace.models import MarketplaceSource
        from ...marketplace.registry import add_marketplace

        # Parse OWNER/REPO or HOST/OWNER/REPO
        if "/" not in repo:
            logger.error(
                f"Invalid format: '{repo}'. Use 'OWNER/REPO' (e.g., 'acme-org/plugin-marketplace')"
            )
            sys.exit(1)

        from ...utils.github_host import default_host, is_valid_fqdn

        parts = repo.split("/")
        if len(parts) == 3 and parts[0] and parts[1] and parts[2]:
            if not is_valid_fqdn(parts[0]):
                logger.error(
                    f"Invalid host: '{parts[0]}'. Use 'OWNER/REPO' or 'HOST/OWNER/REPO' format."
                )
                sys.exit(1)
            if host and host != parts[0]:
                logger.error(f"Conflicting host: --host '{host}' vs '{parts[0]}' in argument.")
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

        # Hard-fail if the user-supplied --name flag is malformed; the
        # manifest's name is validated softly below (publisher mistakes
        # shouldn't break a successful add).
        if name is not None and not _is_valid_alias(name):
            logger.error(
                f"Invalid marketplace name: '{name}'. "
                f"Names must only contain letters, digits, '.', '_', and '-' "
                f"(required for 'apm install plugin@marketplace' syntax)."
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
                f".claude-plugin/marketplace.json"
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
        # Track which tier won so we can report it in verbose mode and emit a
        # warning when a publisher-declared name had to be rejected.
        manifest_name = (manifest.name or "").strip()
        if name is not None:
            display_name = name
            alias_source = "--name flag"
        elif manifest_name and _is_valid_alias(manifest_name):
            display_name = manifest_name
            alias_source = f"manifest.name ('{manifest_name}')"
        else:
            display_name = repo_name
            if manifest_name and not _is_valid_alias(manifest_name):
                logger.warning(
                    f"Manifest declares name '{manifest_name}' which is not a "
                    f"valid alias (must match [a-zA-Z0-9._-]+). "
                    f"Falling back to repo name."
                )
                alias_source = f"repo name (manifest.name '{manifest_name}' invalid)"
            else:
                alias_source = "repo name (manifest.name missing)"

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
            logger.tree_item(f"  [+] {pkg.name}  {pkg.ref}  {sha_short}  ({ref_kind})")
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


class _OutdatedRow:
    """Simple container for outdated table row data."""

    __slots__ = (
        "current",
        "latest_in_range",
        "latest_overall",
        "name",
        "note",
        "range_spec",
        "status",
    )

    def __init__(self, name, current, range_spec, latest_in_range, latest_overall, status, note):
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
    from ...marketplace.tag_pattern import build_tag_regex

    pattern = entry.tag_pattern or yml.build.tag_pattern
    tag_rx = build_tag_regex(pattern)
    results = []
    for remote_ref in refs:
        if not remote_ref.name.startswith("refs/tags/"):
            continue
        tag_name = remote_ref.name[len("refs/tags/") :]
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


class _CheckResult:
    """Container for per-entry check results."""

    __slots__ = ("error", "name", "reachable", "ref_ok", "version_found")

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
    table.add_column("Reachable", style="white", justify="center")
    table.add_column("Version Found", style="white", justify="center")
    table.add_column("Ref OK", style="white", justify="center")
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


class _DoctorCheck:
    """Container for a single doctor check result."""

    __slots__ = ("detail", "informational", "name", "passed")

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

        targets.append(
            ConsumerTarget(
                repo=repo.strip(),
                branch=branch.strip(),
                path_in_repo=path_in_repo.strip(),
            )
        )

    return targets, None


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
            logger.tree_item(f"  {line}")
        click.echo()
        for t in plan.targets:
            logger.tree_item(f"  [*] {t.repo}  branch={t.branch}  path={t.path_in_repo}")
        return

    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console.print()
    console.print(
        Panel(
            plan_text,
            title="Publish plan",
            border_style="cyan",
        )
    )

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

    updated_count = sum(1 for r in results if r.outcome == PublishOutcome.UPDATED)
    failed_count = sum(1 for r in results if r.outcome == PublishOutcome.FAILED)
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
            logger.tree_item(f"  {icon} {r.target.repo}: {r.outcome.value}{pr_info} -- {r.message}")
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
            f"Published {updated}/{total} targets, {failed} failed{suffix}",
            symbol="warning",
        )


@click.command(
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
        if not console:
            # Colorama fallback
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

    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"Search failed: {e}")
        logger.verbose_detail(traceback.format_exc())
        sys.exit(1)


from .check import check  # noqa: E402
from .doctor import doctor  # noqa: E402
from .init import init  # noqa: E402
from .migrate import migrate  # noqa: E402
from .outdated import outdated  # noqa: E402
from .publish import publish  # noqa: E402
from .validate import validate  # noqa: E402

# Public surface: the click group + per-command callables. Domain types are
# re-exported from canonical sources for backward compatibility with tests
# and external consumers that patch via this package path. Submodules import
# their domain types from the canonical sources directly, not from here.
__all__ = [
    "BuildError",
    "BuildOptions",
    "BuildReport",
    "ConfigSource",
    "ConsumerTarget",
    "GitLsRemoteError",
    "HeadNotAllowedError",
    "MarketplaceBuilder",
    "MarketplaceGroup",
    "MarketplaceNotFoundError",
    "MarketplacePublisher",
    "MarketplaceYmlError",
    "NoMatchingVersionError",
    "OfflineMissError",
    "PathTraversalError",
    "PrIntegrator",
    "PrResult",
    "PrState",
    "PublishOutcome",
    "PublishPlan",
    "RefNotFoundError",
    "RefResolver",
    "RemoteRef",
    "ResolvedPackage",
    "SemVer",
    "TargetResult",
    "add",
    "browse",
    "check",
    "detect_config_source",
    "doctor",
    "init",
    "list_cmd",
    "load_marketplace_config",
    "load_marketplace_yml",
    "marketplace",
    "migrate",
    "migrate_marketplace_yml",
    "outdated",
    "package",
    "parse_semver",
    "publish",
    "remove",
    "satisfies_range",
    "search",
    "translate_git_stderr",
    "update",
    "validate",
    "validate_path_segments",
]
