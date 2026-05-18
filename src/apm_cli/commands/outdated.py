"""Check for outdated locked dependencies.

Compares locked dependency commit SHAs against remote tip SHAs.
For tag-pinned deps, also shows the latest available semver tag.
For marketplace-sourced deps, checks available versions in the marketplace.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass, field

import click

from ..core.command_logger import CommandLogger
from ._outdated_runner import _check_deps_with_progress, _CheckRunContext

logger = logging.getLogger(__name__)

TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+")


@dataclass(frozen=True)
class OutdatedRow:
    """One row of ``apm outdated`` output."""

    package: str
    current: str
    latest: str
    status: str
    extra_tags: list[str] = field(default_factory=list)
    source: str = ""


def _is_tag_ref(ref: str) -> bool:
    """Return True when *ref* looks like a semver tag (v1.2.3 or 1.2.3)."""
    return bool(TAG_RE.match(ref)) if ref else False


def _strip_v(ref: str) -> str:
    """Strip leading 'v' prefix from a version string."""
    return ref[1:] if ref and ref.startswith("v") else (ref or "")


def _find_remote_tip(ref_name, remote_refs):
    """Find the tip SHA for a branch ref from remote refs.

    If *ref_name* is empty/None, falls back to common default branch
    names (main, master).
    Returns the commit SHA string or None if not found.
    """
    from ..models.dependency.types import GitReferenceType

    if not remote_refs:
        return None

    branch_refs = {
        r.name: r.commit_sha for r in remote_refs if r.ref_type == GitReferenceType.BRANCH
    }

    if ref_name:
        return branch_refs.get(ref_name)

    # No ref specified -- find the default branch
    for default in ("main", "master"):
        if default in branch_refs:
            return branch_refs[default]

    # Last resort: first branch in list
    if branch_refs:
        return next(iter(branch_refs.values()))

    return None


def _get_marketplace_checker():
    try:
        from ..marketplace.client import fetch_or_cache
        from ..marketplace.errors import MarketplaceError
        from ..marketplace.registry import get_marketplace_by_name
    except ImportError:
        return None
    return fetch_or_cache, MarketplaceError, get_marketplace_by_name


def _load_marketplace_plugin(dep):
    checker = _get_marketplace_checker()
    if checker is None or not dep.discovered_via or not dep.marketplace_plugin_name:
        return None
    fetch_or_cache, marketplace_error, get_marketplace_by_name = checker
    try:
        source_obj = get_marketplace_by_name(dep.discovered_via)
        manifest = fetch_or_cache(source_obj)
    except marketplace_error as exc:
        logger.warning(
            "Marketplace '%s' unavailable; falling back to git check for '%s' (%s)",
            dep.discovered_via,
            dep.marketplace_plugin_name,
            exc,
        )
        return None
    return manifest.find_plugin(dep.marketplace_plugin_name)


def _short_ref(ref: str) -> str:
    return ref[:12] if len(ref) > 12 else ref


def _build_marketplace_row(dep, plugin, installed_ref: str, marketplace_ref: str) -> OutdatedRow:
    latest_display = _short_ref(marketplace_ref)
    if plugin.version:
        latest_display = f"{plugin.version} ({latest_display})"
    return OutdatedRow(
        package=f"{dep.marketplace_plugin_name}@{dep.discovered_via}",
        current=_short_ref(installed_ref),
        latest=latest_display,
        status="outdated" if installed_ref != marketplace_ref else "up-to-date",
        source=f"marketplace: {dep.discovered_via}",
    )


def _check_marketplace_ref(dep, verbose):
    """Check a marketplace-sourced dep against its marketplace entry."""
    plugin = _load_marketplace_plugin(dep)
    if plugin is None or not isinstance(plugin.source, dict):
        return None
    marketplace_ref = plugin.source.get("ref", "")
    installed_ref = dep.resolved_ref or dep.resolved_commit or ""
    if not marketplace_ref or not installed_ref:
        return None
    return _build_marketplace_row(dep, plugin, installed_ref, marketplace_ref)


def _check_one_dep(dep, downloader, verbose):
    """Check a single dependency against remote refs.

    Returns an ``OutdatedRow`` instance.

    This function is safe to call from a thread pool.
    """
    # Try marketplace-based check first for marketplace-sourced deps
    marketplace_result = _check_marketplace_ref(dep, verbose)
    if marketplace_result is not None:
        return marketplace_result

    from ..models.dependency.reference import DependencyReference
    from ..models.dependency.types import GitReferenceType
    from ..utils.version_checker import is_newer_version

    current_ref = dep.resolved_ref or ""
    locked_sha = dep.resolved_commit or ""
    package_name = dep.get_unique_key()

    # Build a DependencyReference to query remote refs
    try:
        # Use parse() to correctly handle all host types (GitHub, ADO, etc.)
        full_url = f"{dep.host}/{dep.repo_url}" if dep.host else dep.repo_url
        dep_ref = DependencyReference.parse(full_url)
    except Exception:
        return OutdatedRow(
            package=package_name, current=current_ref or "(none)", latest="-", status="unknown"
        )

    # Fetch remote refs
    try:
        remote_refs = downloader.list_remote_refs(dep_ref)
    except Exception:
        return OutdatedRow(
            package=package_name, current=current_ref or "(none)", latest="-", status="unknown"
        )

    is_tag = _is_tag_ref(current_ref)

    if is_tag:
        tag_refs = [r for r in remote_refs if r.ref_type == GitReferenceType.TAG]
        if not tag_refs:
            return OutdatedRow(
                package=package_name,
                current=current_ref,
                latest="-",
                status="unknown",
                source="git tags",
            )

        latest_tag = tag_refs[0].name
        current_ver = _strip_v(current_ref)
        latest_ver = _strip_v(latest_tag)

        if is_newer_version(current_ver, latest_ver):
            extra = [r.name for r in tag_refs[:10]] if verbose else []
            return OutdatedRow(
                package=package_name,
                current=current_ref,
                latest=latest_tag,
                status="outdated",
                extra_tags=extra,
                source="git tags",
            )
        else:
            return OutdatedRow(
                package=package_name,
                current=current_ref,
                latest=latest_tag,
                status="up-to-date",
                source="git tags",
            )
    else:
        remote_tip_sha = _find_remote_tip(current_ref, remote_refs)

        if not remote_tip_sha:
            return OutdatedRow(
                package=package_name,
                current=current_ref or "(none)",
                latest="-",
                status="unknown",
                source="git branch",
            )

        display_ref = current_ref or "(default)"
        if locked_sha and locked_sha != remote_tip_sha:
            latest_display = remote_tip_sha[:8]
            return OutdatedRow(
                package=package_name,
                current=display_ref,
                latest=latest_display,
                status="outdated",
                source="git branch",
            )
        else:
            return OutdatedRow(
                package=package_name,
                current=display_ref,
                latest=remote_tip_sha[:8],
                status="up-to-date",
                source="git branch",
            )


def _load_outdated_lockfile(global_: bool, logger: CommandLogger):
    from ..core.scope import InstallScope, get_apm_dir
    from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed

    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    project_root = get_apm_dir(scope)
    migrate_lockfile_if_needed(project_root)
    lockfile = LockFile.read(get_lockfile_path(project_root))
    if lockfile is not None:
        return lockfile
    scope_hint = "~/.apm/" if global_ else "current directory"
    logger.error(f"No lockfile found in {scope_hint}")
    sys.exit(1)


def _build_outdated_downloader():
    from ..core.auth import AuthResolver
    from ..deps.github_downloader import GitHubPackageDownloader

    return GitHubPackageDownloader(auth_resolver=AuthResolver())


def _collect_checkable_dependencies(lockfile, logger: CommandLogger):
    checkable = []
    for key, dep in lockfile.dependencies.items():
        if dep.source == "local":
            logger.verbose_detail(f"Skipping local dep: {key}")
            continue
        if dep.registry_prefix:
            logger.verbose_detail(f"Skipping Artifactory dep: {key}")
            continue
        checkable.append(dep)
    return checkable


def _render_outdated_rows(rows: list[OutdatedRow], verbose: bool) -> None:
    try:
        from rich.table import Table

        from ._helpers import _get_console

        console = _get_console()
        if console is None:
            raise ImportError("Rich console not available")
        table = Table(title="Dependency Status", show_header=True, header_style="bold cyan")
        table.add_column("Package", style="white", min_width=20)
        table.add_column("Current", style="white", min_width=10)
        table.add_column("Latest", style="white", min_width=10)
        table.add_column("Status", min_width=12)
        table.add_column("Source", style="dim", min_width=14)
        status_styles = {"up-to-date": "green", "outdated": "yellow", "unknown": "dim"}
        for row in rows:
            style = status_styles.get(row.status, "white")
            table.add_row(
                row.package,
                row.current,
                row.latest,
                f"[{style}]{row.status}[/{style}]",
                row.source,
            )
            if verbose and row.extra_tags:
                table.add_row("", "", f"[dim]tags: {', '.join(row.extra_tags)}[/dim]", "", "")
        console.print(table)
    except (ImportError, Exception):
        click.echo(f"{'Package':<24}{'Current':<13}{'Latest':<13}{'Status':<15}{'Source'}")
        click.echo("-" * 82)
        for row in rows:
            click.echo(
                f"{row.package:<24}{row.current:<13}{row.latest:<13}{row.status:<15}{row.source}"
            )
            if verbose and row.extra_tags:
                click.echo(f"{'':24}tags: {', '.join(row.extra_tags)}")


def _summarise_outdated_rows(rows: list[OutdatedRow], logger: CommandLogger) -> None:
    outdated_count = sum(1 for row in rows if row.status == "outdated")
    has_unknown = any(row.status == "unknown" for row in rows)
    if outdated_count:
        logger.warning(
            f"{outdated_count} outdated {'dependency' if outdated_count == 1 else 'dependencies'} found"
        )
    elif has_unknown:
        logger.progress("Some dependencies could not be checked (branch/commit refs)")


@click.command(name="outdated", help="Show outdated locked dependencies")
@click.option(
    "--global",
    "-g",
    "global_",
    is_flag=True,
    default=False,
    help="Check user-scope dependencies (~/.apm/)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Show additional info (e.g., available tags for outdated deps)",
)
@click.option(
    "--parallel-checks",
    "-j",
    type=int,
    default=4,
    help="Max concurrent remote checks (default: 4, 0 = sequential)",
)
def outdated(global_, verbose, parallel_checks):
    """Show outdated locked dependencies

    Compares each locked dependency against the remote to detect staleness.
    Tag-pinned deps use semver comparison; branch-pinned deps compare commit SHAs.

    \b
    Examples:
        apm outdated             # Check project deps
        apm outdated --global    # Check user-scope deps
        apm outdated --verbose   # Show available tags
        apm outdated -j 8        # Use 8 parallel checks
    """
    logger = CommandLogger("outdated", verbose=verbose)
    lockfile = _load_outdated_lockfile(global_, logger)
    if not lockfile.dependencies:
        logger.success("No locked dependencies to check")
        return
    checkable = _collect_checkable_dependencies(lockfile, logger)
    if not checkable:
        logger.success("No remote dependencies to check")
        return
    rows = _check_deps_with_progress(
        checkable,
        parallel_checks,
        _CheckRunContext(
            downloader=_build_outdated_downloader(),
            verbose=verbose,
            logger_obj=logger,
            check_fn=_check_one_dep,
        ),
    )
    if not rows:
        logger.success("No remote dependencies to check")
        return
    if not any(row.status in {"outdated", "unknown"} for row in rows):
        logger.success("All dependencies are up-to-date")
        return
    _render_outdated_rows(rows, verbose)
    _summarise_outdated_rows(rows, logger)
