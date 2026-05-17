"""Marketplace CLI package."""

from __future__ import annotations

import builtins
import json
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
_TRUSTED_MARKETPLACE_HOST_KINDS = ("github", "ghe_cloud", "ghes", "gitlab")
from ._add_helpers import (
    _marketplace_add_unsupported_host_error,
    _parse_marketplace_repo,
)
from ._build_render import _render_build_error, _render_build_table
from ._check import (
    _CheckResult,
    _find_duplicate_names,
    _render_check_table,
    _warn_duplicate_names,
)
from ._doctor import _DoctorCheck, _render_doctor_table
from ._io import (
    _check_gitignore_for_marketplace_json,
    _load_config_or_exit,
    _load_yml_or_exit,
)
from ._outdated import (
    _extract_tag_versions,
    _load_current_versions,
    _OutdatedRow,
    _render_outdated_table,
)
from ._publish_helpers import (
    _load_targets_file,
    _outcome_symbol,
    _render_publish_footer,
    _render_publish_plan,
    _render_publish_summary,
)


def _is_valid_alias(value: str) -> bool:
    """Return True when ``value`` is a legal marketplace alias."""
    return bool(value) and _ALIAS_PATTERN.match(value) is not None


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


@click.group(cls=MarketplaceGroup, help="Manage marketplaces for discovery and governance")
@click.pass_context
def marketplace(ctx):
    """Register, browse, and search marketplaces."""


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
        from ...utils.github_host import default_host, is_valid_fqdn

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
        if host is not None:
            normalized_host = host.strip().lower()
            if not is_valid_fqdn(normalized_host):
                logger.error(
                    f"Invalid host: '{host}'. Expected a valid host FQDN "
                    f"(for example, 'github.com').",
                    symbol="error",
                )
                sys.exit(1)
            resolved_host = normalized_host
        elif embedded_host is not None:
            resolved_host = embedded_host
        else:
            resolved_host = default_host()

        # Trusted-host gate. Routes through AuthResolver.classify_host so the
        # registration-time guard and the fetch-time guard in client.py share a
        # single classification implementation.
        from ...core.auth import AuthResolver

        host_info = AuthResolver.classify_host(resolved_host)
        if host_info.kind not in _TRUSTED_MARKETPLACE_HOST_KINDS:
            import shlex as _shlex

            quoted_repo = _shlex.quote(repo)
            quoted_host = _shlex.quote(resolved_host)
            logger.error(
                _marketplace_add_unsupported_host_error(
                    resolved_host, quoted_repo, quoted_host, host_info.kind
                )
            )
            sys.exit(1)

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
                    f"Falling back to repo name.",
                    symbol="warning",
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


from .plugin import package  # noqa: E402

marketplace.add_command(package)

from .check import check  # noqa: E402
from .doctor import doctor  # noqa: E402
from .init import init  # noqa: E402
from .migrate import migrate  # noqa: E402
from .outdated import outdated  # noqa: E402
from .publish import publish  # noqa: E402
from .validate import validate  # noqa: E402

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
