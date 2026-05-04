"""CLI commands for cache management (apm cache info|clean|prune)."""

from __future__ import annotations

import click


@click.group(help="Manage the local package cache")
def cache() -> None:
    """Cache management commands."""


@cache.command(help="Show cache location and size statistics")
def info() -> None:
    """Display cache statistics: location, size, entry counts."""
    from ..cache.paths import get_cache_root
    from ..utils.console import _rich_echo, _rich_info

    try:
        root = get_cache_root()
    except (ValueError, OSError) as exc:
        from ..utils.console import _rich_error

        _rich_error(f"Cannot resolve cache root: {exc}", symbol="error")
        raise SystemExit(1) from exc

    _rich_info(f"Cache root: {root}", symbol="info")

    # Git cache stats
    from ..cache.git_cache import GitCache

    git_cache = GitCache(root)
    git_stats = git_cache.get_cache_stats()

    # HTTP cache stats
    from ..cache.http_cache import HttpCache

    http_cache = HttpCache(root)
    http_stats = http_cache.get_stats()

    total_bytes = git_stats["total_size_bytes"] + http_stats["total_size_bytes"]

    click.echo()
    _rich_echo(f"  Git repositories (db):    {git_stats['db_count']}", symbol="list")
    _rich_echo(f"  Git checkouts:            {git_stats['checkout_count']}", symbol="list")
    _rich_echo(f"  HTTP cache entries:       {http_stats['entry_count']}", symbol="list")
    click.echo()
    _rich_echo(f"  Total size:               {_format_size(total_bytes)}", symbol="list")
    _rich_echo(
        f"    Git:                    {_format_size(git_stats['total_size_bytes'])}", symbol="list"
    )
    _rich_echo(
        f"    HTTP:                   {_format_size(http_stats['total_size_bytes'])}", symbol="list"
    )


@cache.command(help="Remove all cached content")
@click.option("--force", "-f", is_flag=True, help="Skip confirmation prompt")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def clean(force: bool, yes: bool) -> None:
    """Remove all cache content (git repos, checkouts, HTTP responses)."""
    from ..cache.paths import get_cache_root
    from ..utils.console import _rich_info, _rich_success

    try:
        root = get_cache_root()
    except (ValueError, OSError) as exc:
        from ..utils.console import _rich_error

        _rich_error(f"Cannot resolve cache root: {exc}", symbol="error")
        raise SystemExit(1) from exc

    if not force and not yes:
        confirmed = click.confirm(f"Remove all cache content in {root}?", default=False)
        if not confirmed:
            _rich_info("Aborted.", symbol="info")
            return

    _rich_info("Cleaning cache...", symbol="gear")

    from ..cache.git_cache import GitCache
    from ..cache.http_cache import HttpCache

    git_cache = GitCache(root)
    git_cache.clean_all()

    http_cache = HttpCache(root)
    http_cache.clean_all()

    _rich_success("Cache cleaned.", symbol="check")


@cache.command(help="Remove cache entries older than N days")
@click.option(
    "--days",
    type=int,
    default=30,
    show_default=True,
    help="Remove entries not accessed within this many days",
)
def prune(days: int) -> None:
    """Remove stale cache entries based on last access time.

    Note: pruning uses mtime as the access indicator. Entries currently
    referenced by project lockfiles are NOT exempt -- freshness is
    determined solely by filesystem timestamps.
    """
    from ..cache.git_cache import GitCache
    from ..cache.paths import get_cache_root
    from ..utils.console import _rich_info, _rich_success

    try:
        root = get_cache_root()
    except (ValueError, OSError) as exc:
        from ..utils.console import _rich_error

        _rich_error(f"Cannot resolve cache root: {exc}", symbol="error")
        raise SystemExit(1) from exc

    _rich_info(f"Pruning entries older than {days} days...", symbol="gear")

    git_cache = GitCache(root)
    pruned = git_cache.prune(max_age_days=days)

    _rich_success(f"Pruned {pruned} checkout(s).", symbol="check")


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
