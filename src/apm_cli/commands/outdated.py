"""Check for outdated locked dependencies.

Compares locked dependency commit SHAs against remote tip SHAs.
For tag-pinned deps, also shows the latest available semver tag.
For marketplace-sourced deps, checks available versions in the marketplace.
"""

import logging
import re
import sys
from dataclasses import dataclass, field
from typing import List  # noqa: F401, UP035

import click

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


def _check_marketplace_ref(dep, verbose):
    """Check a marketplace-sourced dep against its marketplace entry.

    Compares the installed ref (resolved_ref or resolved_commit) against
    the marketplace entry's current source ref. Returns a result tuple
    ``(package, current, latest, status, extra, source)`` or ``None``
    when the check cannot be performed (caller should fall through to
    the git-based check).
    """
    if not dep.discovered_via or not dep.marketplace_plugin_name:
        return None

    try:
        from ..marketplace.client import fetch_or_cache
        from ..marketplace.errors import MarketplaceError
        from ..marketplace.registry import get_marketplace_by_name
    except ImportError:
        return None

    source_label = f"marketplace: {dep.discovered_via}"

    try:
        source_obj = get_marketplace_by_name(dep.discovered_via)
    except MarketplaceError:
        logger.warning(
            "Marketplace '%s' not found; falling back to git check for '%s'",
            dep.discovered_via,
            dep.marketplace_plugin_name,
        )
        return None

    try:
        manifest = fetch_or_cache(source_obj)
    except MarketplaceError:
        logger.warning(
            "Failed to fetch marketplace '%s'; falling back to git check for '%s'",
            dep.discovered_via,
            dep.marketplace_plugin_name,
        )
        return None

    plugin = manifest.find_plugin(dep.marketplace_plugin_name)
    if not plugin:
        return None

    # Determine marketplace entry's current ref
    mkt_ref = None
    mkt_version = plugin.version or ""
    if isinstance(plugin.source, dict):
        mkt_ref = plugin.source.get("ref", "")
    else:
        # String sources are relative paths, not refs -- skip
        return None

    if not mkt_ref:
        return None

    # Determine installed ref
    installed_ref = dep.resolved_ref or dep.resolved_commit or ""
    if not installed_ref:
        return None

    package_name = f"{dep.marketplace_plugin_name}@{dep.discovered_via}"
    current_display = installed_ref[:12] if len(installed_ref) > 12 else installed_ref
    latest_display = mkt_ref[:12] if len(mkt_ref) > 12 else mkt_ref
    if mkt_version:
        latest_display = f"{mkt_version} ({latest_display})"

    if installed_ref != mkt_ref:
        return OutdatedRow(
            package=package_name,
            current=current_display,
            latest=latest_display,
            status="outdated",
            source=source_label,
        )

    return OutdatedRow(
        package=package_name,
        current=current_display,
        latest=latest_display,
        status="up-to-date",
        source=source_label,
    )


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


@click.command(name="outdated")
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
    from ..core.command_logger import CommandLogger
    from ..core.scope import InstallScope, get_apm_dir
    from ..deps.lockfile import LockFile, get_lockfile_path, migrate_lockfile_if_needed

    logger = CommandLogger("outdated", verbose=verbose)

    # Resolve scope and lockfile path
    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    project_root = get_apm_dir(scope)

    migrate_lockfile_if_needed(project_root)
    lockfile_path = get_lockfile_path(project_root)
    lockfile = LockFile.read(lockfile_path)

    if lockfile is None:
        scope_hint = "~/.apm/" if global_ else "current directory"
        logger.error(f"No lockfile found in {scope_hint}")
        sys.exit(1)

    if not lockfile.dependencies:
        logger.success("No locked dependencies to check")
        return

    # Lazy-init downloader only when we have deps to check
    from ..core.auth import AuthResolver
    from ..deps.github_downloader import GitHubPackageDownloader

    auth_resolver = AuthResolver()
    downloader = GitHubPackageDownloader(auth_resolver=auth_resolver)

    # Filter to checkable deps (skip local + Artifactory)
    checkable = []
    for key, dep in lockfile.dependencies.items():
        if dep.source == "local":
            logger.verbose_detail(f"Skipping local dep: {key}")
            continue
        if dep.registry_prefix:
            logger.verbose_detail(f"Skipping Artifactory dep: {key}")
            continue
        checkable.append(dep)

    if not checkable:
        logger.success("No remote dependencies to check")
        return

    # Check deps with progress feedback and optional parallelism
    rows = _check_deps_with_progress(checkable, downloader, verbose, parallel_checks, logger)

    if not rows:
        logger.success("No remote dependencies to check")
        return

    # Check if everything is up-to-date
    has_outdated = any(row.status == "outdated" for row in rows)
    has_unknown = any(row.status == "unknown" for row in rows)

    if not has_outdated and not has_unknown:
        logger.success("All dependencies are up-to-date")
        return

    # Render the table
    try:
        from rich.table import Table

        from ._helpers import _get_console

        console = _get_console()
        if console is None:
            raise ImportError("Rich console not available")

        table = Table(
            title="Dependency Status",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Package", style="white", min_width=20)
        table.add_column("Current", style="white", min_width=10)
        table.add_column("Latest", style="white", min_width=10)
        table.add_column("Status", min_width=12)
        table.add_column("Source", style="dim", min_width=14)

        status_styles = {
            "up-to-date": "green",
            "outdated": "yellow",
            "unknown": "dim",
        }

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
                tags_str = ", ".join(row.extra_tags)
                table.add_row("", "", f"[dim]tags: {tags_str}[/dim]", "", "")

        console.print(table)

    except (ImportError, Exception):
        # Fallback: plain text output
        click.echo(f"{'Package':<24}{'Current':<13}{'Latest':<13}{'Status':<15}{'Source'}")
        click.echo("-" * 82)
        for row in rows:
            click.echo(
                f"{row.package:<24}{row.current:<13}{row.latest:<13}{row.status:<15}{row.source}"
            )
            if verbose and row.extra_tags:
                click.echo(f"{'':24}tags: {', '.join(row.extra_tags)}")

    # Summary
    outdated_count = sum(1 for row in rows if row.status == "outdated")
    if outdated_count:
        logger.warning(
            f"{outdated_count} outdated "
            f"{'dependency' if outdated_count == 1 else 'dependencies'} found"
        )
    elif has_unknown:
        logger.progress("Some dependencies could not be checked (branch/commit refs)")


def _check_deps_with_progress(checkable, downloader, verbose, parallel_checks, logger):
    """Check all deps with Rich progress bar and optional parallelism."""
    rows = []
    total = len(checkable)

    try:
        from rich.progress import (
            BarColumn,
            Progress,
            SpinnerColumn,
            TaskProgressColumn,
            TextColumn,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}[/cyan]"),
            BarColumn(),
            TaskProgressColumn(),
            transient=True,
        ) as progress:
            if parallel_checks > 0 and total > 1:
                rows = _check_parallel(
                    checkable,
                    downloader,
                    verbose,
                    parallel_checks,
                    progress,
                    logger,
                )
            else:
                task_id = progress.add_task(
                    f"Checking {total} dependencies",
                    total=total,
                )
                for dep in checkable:
                    short = dep.get_unique_key().split("/")[-1]
                    progress.update(task_id, description=f"Checking {short}")
                    result = _check_one_dep(dep, downloader, verbose)
                    rows.append(result)
                    progress.advance(task_id)
    except ImportError:
        # No Rich -- plain text feedback
        logger.progress(f"Checking {total} dependencies...")
        if parallel_checks > 0 and total > 1:
            rows = _check_parallel_plain(
                checkable,
                downloader,
                verbose,
                parallel_checks,
            )
        else:
            for dep in checkable:
                rows.append(_check_one_dep(dep, downloader, verbose))

    return rows


def _check_parallel(checkable, downloader, verbose, max_workers, progress, logger):
    """Run checks in parallel with Rich progress display."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(checkable)
    max_workers = min(max_workers, total)
    overall_id = progress.add_task(
        f"Checking {total} dependencies",
        total=total,
    )

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for dep in checkable:
            short = dep.get_unique_key().split("/")[-1]
            task_id = progress.add_task(f"Checking {short}", total=None)
            fut = executor.submit(_check_one_dep, dep, downloader, verbose)
            futures[fut] = (dep, task_id)

        for fut in as_completed(futures):
            dep, task_id = futures[fut]
            try:
                result = fut.result()
            except Exception:
                pkg = dep.get_unique_key()
                result = OutdatedRow(package=pkg, current="(none)", latest="-", status="unknown")
            results[dep.get_unique_key()] = result
            progress.update(task_id, visible=False)
            progress.advance(overall_id)

    # Preserve original order
    return [results[dep.get_unique_key()] for dep in checkable if dep.get_unique_key() in results]


def _check_parallel_plain(checkable, downloader, verbose, max_workers):
    """Run checks in parallel without Rich (plain fallback)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    max_workers = min(max_workers, len(checkable))
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_check_one_dep, dep, downloader, verbose): dep for dep in checkable
        }
        for fut in as_completed(futures):
            dep = futures[fut]
            try:
                result = fut.result()
            except Exception:
                pkg = dep.get_unique_key()
                result = OutdatedRow(package=pkg, current="(none)", latest="-", status="unknown")
            results[dep.get_unique_key()] = result

    return [results[dep.get_unique_key()] for dep in checkable if dep.get_unique_key() in results]
