"""Parallel package pre-download phase.

Reads ``ctx.deps_to_install``, ``ctx.existing_lockfile``,
``ctx.update_refs``, ``ctx.parallel_downloads``, ``ctx.apm_modules_dir``,
``ctx.downloader``, and ``ctx.callback_downloaded``; populates
``ctx.pre_download_results`` (dep_key -> PackageInfo) and
``ctx.pre_downloaded_keys`` (set of dep_keys that were pre-downloaded).

This is Phase 4 (#171) of the install pipeline.  Packages that were already
fetched during BFS resolution (callback_downloaded), local packages, and
those whose lockfile SHA matches the on-disk HEAD are skipped.  Remaining
packages are fetched in parallel via :class:`ThreadPoolExecutor` with a Rich
progress UI.  Failures are silently swallowed -- the sequential integration
loop is the source of truth for error reporting.
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def _build_download_queue(ctx: InstallContext) -> list[tuple[object, object, object]]:
    from apm_cli.drift import build_download_ref, detect_ref_change

    need_download: list[tuple[object, object, object]] = []
    for dep_ref in ctx.deps_to_install:
        dep_key = dep_ref.get_unique_key()
        dep_path = (
            (ctx.apm_modules_dir / dep_ref.alias)
            if dep_ref.alias
            else dep_ref.get_install_path(ctx.apm_modules_dir)
        )
        if dep_ref.is_local or dep_key in ctx.callback_downloaded:
            continue
        dep_locked_chk = (
            ctx.existing_lockfile.get_dependency(dep_key) if ctx.existing_lockfile else None
        )
        ref_changed = detect_ref_change(dep_ref, dep_locked_chk, update_refs=ctx.update_refs)
        if (
            dep_path.exists()
            and dep_locked_chk
            and dep_locked_chk.resolved_commit
            and dep_locked_chk.resolved_commit != "cached"
            and (ctx.update_refs or not ref_changed)
        ):
            try:
                from git import Repo as git_repo

                if git_repo(dep_path).head.commit.hexsha == dep_locked_chk.resolved_commit:
                    continue
            except Exception:
                if dep_locked_chk.content_hash and dep_path.is_dir():
                    from apm_cli.utils.content_hash import verify_package_hash as verify_hash

                    if verify_hash(dep_path, dep_locked_chk.content_hash):
                        continue
        need_download.append(
            (
                dep_ref,
                dep_path,
                build_download_ref(
                    dep_ref,
                    ctx.existing_lockfile,
                    update_refs=ctx.update_refs,
                    ref_changed=ref_changed,
                ),
            )
        )
    return need_download


def _run_parallel_downloads(
    ctx: InstallContext,
    need_download: list[tuple[object, object, object]],
) -> dict[str, object]:
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import as_completed as futures_completed

    pre_download_results: dict[str, object] = {}
    if not need_download or ctx.parallel_downloads <= 0:
        return pre_download_results

    max_workers = min(ctx.parallel_downloads, len(need_download))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for dep_ref, dep_path, dep_dlref in need_download:
            dep_display = str(dep_ref) if dep_ref.is_virtual else dep_ref.repo_url
            dep_short = dep_display.split("/")[-1] if "/" in dep_display else dep_display
            dep_key = dep_ref.get_unique_key()
            if ctx.tui is not None:
                ctx.tui.task_started(dep_key, f"fetch {dep_short}")
            dep_future = executor.submit(
                ctx.downloader.download_package,
                dep_dlref,
                dep_path,
                progress_task_id=None,
                progress_obj=None,
            )
            futures[dep_future] = dep_key
        for dep_future in futures_completed(futures):
            dep_key = futures[dep_future]
            try:
                pre_download_results[dep_key] = dep_future.result()
                if ctx.tui is not None:
                    ctx.tui.task_completed(dep_key)
            except Exception:
                if ctx.tui is not None:
                    ctx.tui.task_failed(dep_key)
    return pre_download_results


def run(ctx: InstallContext) -> None:
    """Execute the parallel download phase.

    On return ``ctx.pre_download_results`` and ``ctx.pre_downloaded_keys``
    are populated.
    """
    pre_download_results = _run_parallel_downloads(ctx, _build_download_queue(ctx))
    ctx.pre_download_results = pre_download_results
    ctx.pre_downloaded_keys = builtins.set(pre_download_results.keys())
