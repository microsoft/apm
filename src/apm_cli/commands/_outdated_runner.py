"""Parallel and progress-aware runner for ``apm outdated`` dependency checks.

Extracted from ``outdated.py`` to keep that module under the 500-line limit.
All three functions accept a *check_fn* callable so they remain decoupled from
the ``_check_one_dep`` implementation and carry no top-level import back into
``outdated.py`` (avoiding a circular-import).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CheckRunContext:
    """Shared execution context for outdated checks."""

    downloader: object
    verbose: bool
    logger_obj: object
    check_fn: object


def _check_deps_with_progress(checkable, parallel_checks, run_ctx: _CheckRunContext):
    """Check all deps with Rich progress bar and optional parallelism.

    Parameters
    ----------
    checkable:
        List of ``LockedDependency`` objects to check.
    downloader:
        ``GitHubPackageDownloader`` instance (or compatible mock).
    verbose:
        Whether verbose output was requested.
    parallel_checks:
        Maximum number of concurrent remote checks (0 = sequential).
    logger_obj:
        A ``CommandLogger`` (or compatible) instance for plain-text progress.
    check_fn:
        Callable ``(dep, downloader, verbose) -> OutdatedRow`` — typically
        ``_check_one_dep`` from ``outdated.py``.
    """
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
                rows = _check_parallel(checkable, parallel_checks, progress, run_ctx)
            else:
                task_id = progress.add_task(
                    f"Checking {total} dependencies",
                    total=total,
                )
                for dep in checkable:
                    short = dep.get_unique_key().split("/")[-1]
                    progress.update(task_id, description=f"Checking {short}")
                    result = run_ctx.check_fn(dep, run_ctx.downloader, run_ctx.verbose)
                    rows.append(result)
                    progress.advance(task_id)
    except ImportError:
        # No Rich -- plain text feedback
        run_ctx.logger_obj.progress(f"Checking {total} dependencies...")
        if parallel_checks > 0 and total > 1:
            rows = _check_parallel_plain(checkable, parallel_checks, run_ctx)
        else:
            for dep in checkable:
                rows.append(run_ctx.check_fn(dep, run_ctx.downloader, run_ctx.verbose))

    return rows


def _check_parallel(checkable, max_workers, progress, run_ctx: _CheckRunContext):
    """Run checks in parallel with Rich progress display.

    Parameters
    ----------
    check_fn:
        Callable ``(dep, downloader, verbose) -> OutdatedRow``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Lazy import avoids a circular dependency at module-load time.
    from .outdated import OutdatedRow

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
            fut = executor.submit(run_ctx.check_fn, dep, run_ctx.downloader, run_ctx.verbose)
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


def _check_parallel_plain(checkable, max_workers, run_ctx: _CheckRunContext):
    """Run checks in parallel without Rich (plain fallback).

    Parameters
    ----------
    check_fn:
        Callable ``(dep, downloader, verbose) -> OutdatedRow``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Lazy import avoids a circular dependency at module-load time.
    from .outdated import OutdatedRow

    max_workers = min(max_workers, len(checkable))
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_ctx.check_fn, dep, run_ctx.downloader, run_ctx.verbose): dep
            for dep in checkable
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
