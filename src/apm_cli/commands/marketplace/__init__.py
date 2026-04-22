"""Marketplace CLI package.

This package keeps the click group wiring, shared rendering helpers, and
small compatibility commands that are still exposed from
``apm_cli.commands.marketplace``.
"""

from __future__ import annotations

import builtins
import json
import subprocess
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

import click
import yaml

from ...core.command_logger import CommandLogger
from ...marketplace.builder import (
    BuildOptions,
    BuildReport,
    MarketplaceBuilder,
    ResolvedPackage,
)
from ...marketplace.errors import (
    BuildError,
    GitLsRemoteError,
    HeadNotAllowedError,
    MarketplaceYmlError,
    NoMatchingVersionError,
    OfflineMissError,
    RefNotFoundError,
)
from ...marketplace.git_stderr import translate_git_stderr
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
from ...marketplace.yml_schema import MarketplaceYml, PackageEntry, load_marketplace_yml
from ...utils.path_security import PathTraversalError, validate_path_segments
from .._helpers import _get_console, _is_interactive

# Restore builtins shadowed by command names.
list = builtins.list


def _load_yml_or_exit(logger: CommandLogger) -> MarketplaceYml:
    """Load ``./marketplace.yml`` from CWD or exit with an appropriate code."""
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


def _check_gitignore_for_marketplace_json(logger: CommandLogger) -> None:
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


@click.group(help="Manage plugin marketplaces for discovery and governance")
def marketplace() -> None:
    """Register, browse, and search plugin marketplaces."""
    pass


@marketplace.command(help="Register a plugin marketplace")
@click.argument("repo", required=True)
@click.option("--name", "-n", default=None, help="Display name (defaults to repo name)")
@click.option("--branch", "-b", default="main", show_default=True, help="Branch to use")
@click.option("--host", default=None, help="Git host FQDN (default: github.com)")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def add(
    repo: str,
    name: str | None,
    branch: str,
    host: str | None,
    verbose: bool,
) -> None:
    """Register a marketplace from OWNER/REPO or HOST/OWNER/REPO."""
    logger = CommandLogger("marketplace-add", verbose=verbose)
    try:
        import re

        from ...marketplace.client import _auto_detect_path, fetch_marketplace
        from ...marketplace.models import MarketplaceSource
        from ...marketplace.registry import add_marketplace
        from ...utils.github_host import default_host, is_valid_fqdn

        if "/" not in repo:
            logger.error(
                f"Invalid format: '{repo}'. Use 'OWNER/REPO' "
                f"(e.g., 'acme-org/plugin-marketplace')"
            )
            sys.exit(1)

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

        source = MarketplaceSource(
            name=display_name,
            owner=owner,
            repo=repo_name,
            branch=branch,
            host=resolved_host,
            path=detected_path,
        )

        manifest = fetch_marketplace(source, force_refresh=True)
        plugin_count = len(manifest.plugins)

        add_marketplace(source)

        logger.success(
            f"Marketplace '{display_name}' registered ({plugin_count} plugins)",
            symbol="check",
        )
        if manifest.description:
            logger.verbose_detail(f"    {manifest.description}")

    except Exception as exc:
        logger.error(f"Failed to register marketplace: {exc}")
        sys.exit(1)


@marketplace.command(name="list", help="List registered marketplaces")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def list_cmd(verbose: bool) -> None:
    """Show all registered marketplaces."""
    logger = CommandLogger("marketplace-list", verbose=verbose)
    try:
        from ...marketplace.registry import get_registered_marketplaces

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
            logger.progress(f"{len(sources)} marketplace(s) registered:", symbol="info")
            for source in sources:
                click.echo(f"  {source.name}  ({source.owner}/{source.repo})")
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

        for source in sources:
            table.add_row(
                source.name,
                f"{source.owner}/{source.repo}",
                source.branch,
                source.path,
            )

        console.print()
        console.print(table)
        console.print("\n[dim]Use 'apm marketplace browse <name>' to see plugins[/dim]")

    except Exception as exc:
        logger.error(f"Failed to list marketplaces: {exc}")
        sys.exit(1)


@marketplace.command(help="Browse plugins in a marketplace")
@click.argument("name", required=True)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def browse(name: str, verbose: bool) -> None:
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
            logger.success(
                f"{len(manifest.plugins)} plugin(s) in '{name}':",
                symbol="check",
            )
            for plugin_item in manifest.plugins:
                desc = f" -- {plugin_item.description}" if plugin_item.description else ""
                click.echo(f"  {plugin_item.name}{desc}")
            click.echo(f"\n  Install: apm install <plugin-name>@{name}")
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

        for plugin_item in manifest.plugins:
            desc = plugin_item.description or "--"
            ver = plugin_item.version or "--"
            table.add_row(plugin_item.name, desc, ver, f"{plugin_item.name}@{name}")

        console.print()
        console.print(table)
        console.print(f"\n[dim]Install a plugin: apm install <plugin-name>@{name}[/dim]")

    except Exception as exc:
        logger.error(f"Failed to browse marketplace: {exc}")
        sys.exit(1)


@marketplace.command(help="Refresh marketplace cache")
@click.argument("name", required=False, default=None)
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def update(name: str | None, verbose: bool) -> None:
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
            for source in sources:
                try:
                    clear_marketplace_cache(source.name, host=source.host)
                    manifest = fetch_marketplace(source, force_refresh=True)
                    logger.tree_item(f"  {source.name} ({len(manifest.plugins)} plugins)")
                except Exception as exc:
                    logger.warning(f"  {source.name}: {exc}")
            logger.success("Marketplace cache refreshed", symbol="check")

    except Exception as exc:
        logger.error(f"Failed to update marketplace: {exc}")
        sys.exit(1)


@marketplace.command(help="Remove a registered marketplace")
@click.argument("name", required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def remove(name: str, yes: bool, verbose: bool) -> None:
    """Unregister a marketplace."""
    logger = CommandLogger("marketplace-remove", verbose=verbose)
    try:
        from ...marketplace.client import clear_marketplace_cache
        from ...marketplace.registry import get_marketplace_by_name, remove_marketplace

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

    except Exception as exc:
        logger.error(f"Failed to remove marketplace: {exc}")
        sys.exit(1)


def _render_build_error(logger: CommandLogger, exc: BuildError) -> None:
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


def _render_build_table(logger: CommandLogger, report: BuildReport) -> None:
    """Render the resolved-packages table (Rich with colorama fallback)."""
    console = _get_console()
    if not console:
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
        ref_kind = "tag"
        if pkg.ref and not parse_semver(pkg.ref.lstrip("vV")):
            ref_kind = "ref"
        table.add_row(Text("[+]"), pkg.name, pkg.ref, sha_short, ref_kind)

    console.print()
    console.print(table)


class _OutdatedRow:
    """Simple container for outdated table row data."""

    name: str
    current: str
    range_spec: str
    latest_in_range: str
    latest_overall: str
    status: str
    note: str

    __slots__ = (
        "name",
        "current",
        "range_spec",
        "latest_in_range",
        "latest_overall",
        "status",
        "note",
    )

    def __init__(
        self,
        name: str,
        current: str,
        range_spec: str,
        latest_in_range: str,
        latest_overall: str,
        status: str,
        note: str,
    ) -> None:
        """Store one rendered row for the ``marketplace outdated`` output."""
        self.name = name
        self.current = current
        self.range_spec = range_spec
        self.latest_in_range = latest_in_range
        self.latest_overall = latest_overall
        self.status = status
        self.note = note


def _load_current_versions() -> dict[str, str]:
    """Load current ref versions from marketplace.json if present."""
    mkt_path = Path.cwd() / "marketplace.json"
    if not mkt_path.exists():
        return {}
    try:
        data = json.loads(mkt_path.read_text(encoding="utf-8"))
        result: dict[str, str] = {}
        for plugin_item in data.get("plugins", []):
            name = plugin_item.get("name", "")
            src = plugin_item.get("source", {})
            if isinstance(src, dict):
                result[name] = src.get("ref", "--")
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def _extract_tag_versions(
    refs: Sequence[RemoteRef],
    entry: PackageEntry,
    yml: MarketplaceYml,
    include_prerelease: bool,
) -> list[tuple[SemVer, str]]:
    """Extract (SemVer, tag_name) pairs from remote refs for a package entry."""
    from ...marketplace.tag_pattern import build_tag_regex

    pattern = entry.tag_pattern or yml.build.tag_pattern
    tag_rx = build_tag_regex(pattern)
    results: list[tuple[SemVer, str]] = []
    for remote_ref in refs:
        if not remote_ref.name.startswith("refs/tags/"):
            continue
        tag_name = remote_ref.name[len("refs/tags/") :]
        match = tag_rx.match(tag_name)
        if not match:
            continue
        version_str = match.group("version")
        semver_value = parse_semver(version_str)
        if semver_value is None:
            continue
        if semver_value.is_prerelease and not (
            include_prerelease or entry.include_prerelease
        ):
            continue
        results.append((semver_value, tag_name))
    return results


def _render_outdated_table(
    logger: CommandLogger,
    rows: Sequence[_OutdatedRow],
) -> None:
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

    name: str
    reachable: bool
    version_found: bool
    ref_ok: bool
    error: str

    __slots__ = ("name", "reachable", "version_found", "ref_ok", "error")

    def __init__(
        self,
        name: str,
        reachable: bool,
        version_found: bool,
        ref_ok: bool,
        error: str,
    ) -> None:
        """Store one health-check result for the ``marketplace check`` table."""
        self.name = name
        self.reachable = reachable
        self.version_found = version_found
        self.ref_ok = ref_ok
        self.error = error


def _render_check_table(
    logger: CommandLogger,
    results: Sequence[_CheckResult],
) -> None:
    """Render the check-results table."""
    console = _get_console()
    if not console:
        for result in results:
            icon = "[+]" if result.ref_ok else "[x]"
            detail = result.error if result.error else "OK"
            logger.tree_item(f"  {icon} {result.name}: {detail}")
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

    for result in results:
        reach = "[+]" if result.reachable else "[x]"
        ver = "[+]" if result.version_found else "[x]"
        ref = "[+]" if result.ref_ok else "[x]"
        detail = result.error if result.error else "OK"
        table.add_row(
            Text("[+]" if result.ref_ok else "[x]"),
            result.name,
            Text(reach),
            Text(ver),
            Text(ref),
            detail,
        )

    console.print()
    console.print(table)


class _DoctorCheck:
    """Container for a single doctor check result."""

    name: str
    passed: bool
    detail: str
    informational: bool

    __slots__ = ("name", "passed", "detail", "informational")

    def __init__(
        self,
        name: str,
        passed: bool,
        detail: str,
        informational: bool = False,
    ) -> None:
        """Store one diagnostic row for the ``marketplace doctor`` output."""
        self.name = name
        self.passed = passed
        self.detail = detail
        self.informational = informational


def _render_doctor_table(
    logger: CommandLogger,
    checks: Sequence[_DoctorCheck],
) -> None:
    """Render the doctor results table."""
    console = _get_console()
    if not console:
        for check in checks:
            if check.informational:
                icon = "[i]"
            elif check.passed:
                icon = "[+]"
            else:
                icon = "[x]"
            logger.tree_item(f"  {icon} {check.name}: {check.detail}")
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

    for check in checks:
        if check.informational:
            icon = "[i]"
        elif check.passed:
            icon = "[+]"
        else:
            icon = "[x]"
        table.add_row(check.name, Text(icon), check.detail)

    console.print()
    console.print(table)


def _load_targets_file(
    path: Path,
) -> tuple[list[ConsumerTarget] | None, str | None]:
    """Load and validate a consumer-targets YAML file."""
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

    targets: list[ConsumerTarget] = []
    for idx, entry in enumerate(raw_targets):
        if not isinstance(entry, dict):
            return None, f"targets[{idx}] must be a mapping."

        repo = entry.get("repo")
        if not repo or not isinstance(repo, str):
            return None, f"targets[{idx}]: 'repo' is required (owner/name)."

        parts = repo.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None, f"targets[{idx}]: 'repo' must be 'owner/name', got '{repo}'."

        branch = entry.get("branch")
        if not branch or not isinstance(branch, str):
            return None, f"targets[{idx}]: 'branch' is required."

        path_in_repo = entry.get("path_in_repo", "apm.yml")
        if not isinstance(path_in_repo, str) or not path_in_repo.strip():
            return None, f"targets[{idx}]: 'path_in_repo' must be a non-empty string."

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


def _render_publish_plan(logger: CommandLogger, plan: PublishPlan) -> None:
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
        for target in plan.targets:
            logger.tree_item(
                f"  [*] {target.repo}  branch={target.branch}  path={target.path_in_repo}"
            )
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

    for target in plan.targets:
        table.add_row(target.repo, target.branch, target.path_in_repo, Text("[*]"))

    console.print(table)
    console.print()


def _render_publish_summary(
    logger: CommandLogger,
    results: Sequence[TargetResult],
    pr_results: Sequence[PrResult],
    no_pr: bool,
    dry_run: bool,
) -> None:
    """Render the final publish summary table."""
    console = _get_console()

    pr_by_repo: dict[str, PrResult] = {}
    for pr_result in pr_results:
        pr_by_repo[pr_result.target.repo] = pr_result

    updated_count = sum(
        1 for result in results if result.outcome == PublishOutcome.UPDATED
    )
    failed_count = sum(
        1 for result in results if result.outcome == PublishOutcome.FAILED
    )
    total = len(results)

    if not console:
        click.echo()
        for result in results:
            icon = _outcome_symbol(result.outcome)
            pr_info = ""
            if not no_pr:
                pr_result = pr_by_repo.get(result.target.repo)
                if pr_result:
                    pr_info = f"  PR: {pr_result.state.value}"
                    if pr_result.pr_number:
                        pr_info += f" #{pr_result.pr_number}"
            logger.tree_item(
                f"  {icon} {result.target.repo}: {result.outcome.value}{pr_info} -- {result.message}"
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

    for result in results:
        icon = _outcome_symbol(result.outcome)
        row = [Text(icon), result.target.repo, result.outcome.value]

        if not no_pr:
            pr_result = pr_by_repo.get(result.target.repo)
            if pr_result:
                row.append(pr_result.state.value)
                row.append(str(pr_result.pr_number) if pr_result.pr_number else "--")
                row.append(pr_result.pr_url or "--")
            else:
                row.extend(["--", "--", "--"])

        row.append(result.message)
        table.add_row(*row)

    console.print()
    console.print(table)
    console.print()

    _render_publish_footer(logger, updated_count, failed_count, total, dry_run)


def _outcome_symbol(outcome: PublishOutcome) -> str:
    """Map a ``PublishOutcome`` to a bracket symbol."""
    if outcome == PublishOutcome.UPDATED:
        return "[+]"
    if outcome == PublishOutcome.FAILED:
        return "[x]"
    if outcome in (
        PublishOutcome.SKIPPED_DOWNGRADE,
        PublishOutcome.SKIPPED_REF_CHANGE,
    ):
        return "[!]"
    if outcome == PublishOutcome.NO_CHANGE:
        return "[*]"
    return "[*]"


def _render_publish_footer(
    logger: CommandLogger,
    updated: int,
    failed: int,
    total: int,
    dry_run: bool,
) -> None:
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


@click.command(
    name="search",
    help="Search plugins in a marketplace (QUERY@MARKETPLACE)",
)
@click.argument("expression", required=True)
@click.option("--limit", default=20, show_default=True, help="Max results to show")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def search(expression: str, limit: int, verbose: bool) -> None:
    """Search for plugins in a specific marketplace."""
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
        except Exception:
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
            logger.success(f"Found {len(results)} plugin(s):", symbol="check")
            for plugin_item in results:
                desc = f" -- {plugin_item.description}" if plugin_item.description else ""
                click.echo(f"  {plugin_item.name}@{marketplace_name}{desc}")
            click.echo(f"\n  Install: apm install <plugin-name>@{marketplace_name}")
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

        for plugin_item in results:
            desc = plugin_item.description or "--"
            if len(desc) > 60:
                desc = desc[:57] + "..."
            table.add_row(plugin_item.name, desc, f"{plugin_item.name}@{marketplace_name}")

        console.print()
        console.print(table)
        console.print(
            f"\n[dim]Install: apm install <plugin-name>@{marketplace_name}[/dim]"
        )

    except SystemExit:
        raise
    except Exception as exc:
        logger.error(f"Search failed: {exc}")
        if verbose:
            click.echo(traceback.format_exc(), err=True)
        sys.exit(1)


from .plugin import plugin  # noqa: E402

marketplace.add_command(plugin)

from .build import build  # noqa: E402
from .check import check  # noqa: E402
from .doctor import doctor  # noqa: E402
from .init import init  # noqa: E402
from .outdated import outdated  # noqa: E402
from .publish import publish  # noqa: E402
from .validate import validate  # noqa: E402

__all__ = [
    "marketplace",
    "plugin",
    "init",
    "add",
    "list_cmd",
    "browse",
    "update",
    "remove",
    "validate",
    "build",
    "outdated",
    "check",
    "doctor",
    "publish",
    "search",
    "_load_yml_or_exit",
    "_check_gitignore_for_marketplace_json",
    "_render_build_error",
    "_render_build_table",
    "_OutdatedRow",
    "_load_current_versions",
    "_extract_tag_versions",
    "_render_outdated_table",
    "_CheckResult",
    "_render_check_table",
    "_DoctorCheck",
    "_render_doctor_table",
    "_load_targets_file",
    "_render_publish_plan",
    "_render_publish_summary",
    "_outcome_symbol",
    "_render_publish_footer",
    "BuildOptions",
    "BuildReport",
    "MarketplaceBuilder",
    "ResolvedPackage",
    "BuildError",
    "GitLsRemoteError",
    "HeadNotAllowedError",
    "MarketplaceYmlError",
    "NoMatchingVersionError",
    "OfflineMissError",
    "RefNotFoundError",
    "translate_git_stderr",
    "PrIntegrator",
    "PrResult",
    "PrState",
    "ConsumerTarget",
    "MarketplacePublisher",
    "PublishOutcome",
    "PublishPlan",
    "TargetResult",
    "RefResolver",
    "RemoteRef",
    "SemVer",
    "parse_semver",
    "satisfies_range",
    "load_marketplace_yml",
    "PathTraversalError",
    "validate_path_segments",
    "_get_console",
    "_is_interactive",
    "subprocess",
]
