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


def run(ctx: "InstallContext") -> None:
    """Execute the parallel download phase.

    On return ``ctx.pre_download_results`` and ``ctx.pre_downloaded_keys``
    are populated.
    """
    # Module-attribute access for late-patchability (same pattern as
    # resolve.py).  detect_ref_change / build_download_ref live in
    # apm_cli.drift and tests import them from there, so direct import
    # is safe here -- no test patches at apm_cli.commands.install.X.
    from apm_cli.drift import build_download_ref, detect_ref_change

    deps_to_install = ctx.deps_to_install
    existing_lockfile = ctx.existing_lockfile
    update_refs = ctx.update_refs
    parallel_downloads = ctx.parallel_downloads
    apm_modules_dir = ctx.apm_modules_dir
    downloader = ctx.downloader
    callback_downloaded = ctx.callback_downloaded

    # Phase 4 (#171): Parallel package downloads using ThreadPoolExecutor
    # Pre-download all non-cached packages in parallel for wall-clock speedup.
    # Results are stored and consumed by the sequential integration loop below.
    from concurrent.futures import ThreadPoolExecutor, as_completed as _futures_completed

    _pre_download_results = {}   # dep_key -> PackageInfo
    _need_download = []
    for _pd_ref in deps_to_install:
        _pd_key = _pd_ref.get_unique_key()
        _pd_path = (apm_modules_dir / _pd_ref.alias) if _pd_ref.alias else _pd_ref.get_install_path(apm_modules_dir)
        # Skip local packages -- they are copied, not downloaded
        if _pd_ref.is_local:
            continue
        # Skip if already downloaded during BFS resolution
        if _pd_key in callback_downloaded:
            continue
        # Detect if manifest ref changed from what's recorded in the lockfile.
        # detect_ref_change() handles all transitions including None->ref.
        _pd_locked_chk = (
            existing_lockfile.get_dependency(_pd_key)
            if existing_lockfile
            else None
        )
        _pd_ref_changed = detect_ref_change(
            _pd_ref, _pd_locked_chk, update_refs=update_refs
        )
        # Skip if lockfile SHA matches local HEAD.
        # Normal mode: only when the ref hasn't changed in the manifest.
        # Update mode: defer to the sequential loop which resolves the
        # remote ref and compares -- if unchanged, the download is skipped
        # entirely; if changed, it falls back to sequential download.
        if (_pd_path.exists() and _pd_locked_chk
                and _pd_locked_chk.resolved_commit
                and _pd_locked_chk.resolved_commit != "cached"
                and (update_refs or not _pd_ref_changed)):
            try:
                from git import Repo as _PDGitRepo
                if _PDGitRepo(_pd_path).head.commit.hexsha == _pd_locked_chk.resolved_commit:
                    continue
            except Exception:
                # Git check failed (e.g. .git removed after download).
                # Fall back to content-hash verification so correctly
                # installed packages are not re-downloaded every run (#763).
                if _pd_locked_chk.content_hash and _pd_path.is_dir():
                    from apm_cli.utils.content_hash import verify_package_hash as _pd_verify_hash
                    if _pd_verify_hash(_pd_path, _pd_locked_chk.content_hash):
                        continue
        # Build download ref (use locked commit for reproducibility).
        # build_download_ref() uses the manifest ref when ref_changed is True.
        _pd_dlref = build_download_ref(
            _pd_ref, existing_lockfile, update_refs=update_refs, ref_changed=_pd_ref_changed
        )
        _need_download.append((_pd_ref, _pd_path, _pd_dlref))

    if _need_download and parallel_downloads > 0:
        from rich.progress import (
            Progress,
            SpinnerColumn,
            TextColumn,
            BarColumn,
            TaskProgressColumn,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[cyan]{task.description}[/cyan]"),
            BarColumn(),
            TaskProgressColumn(),
            transient=True,
        ) as _dl_progress:
            _max_workers = min(parallel_downloads, len(_need_download))
            with ThreadPoolExecutor(max_workers=_max_workers) as _executor:
                _futures = {}
                for _pd_ref, _pd_path, _pd_dlref in _need_download:
                    _pd_disp = str(_pd_ref) if _pd_ref.is_virtual else _pd_ref.repo_url
                    _pd_short = _pd_disp.split("/")[-1] if "/" in _pd_disp else _pd_disp
                    _pd_tid = _dl_progress.add_task(description=f"Fetching {_pd_short}", total=None)
                    _pd_fut = _executor.submit(
                        downloader.download_package, _pd_dlref, _pd_path,
                        progress_task_id=_pd_tid, progress_obj=_dl_progress,
                    )
                    _futures[_pd_fut] = (_pd_ref, _pd_tid, _pd_disp)
                for _pd_fut in _futures_completed(_futures):
                    _pd_ref, _pd_tid, _pd_disp = _futures[_pd_fut]
                    _pd_key = _pd_ref.get_unique_key()
                    try:
                        _pd_info = _pd_fut.result()
                        _pre_download_results[_pd_key] = _pd_info
                        _dl_progress.update(_pd_tid, visible=False)
                        _dl_progress.refresh()
                    except Exception:
                        _dl_progress.remove_task(_pd_tid)
                        # Silent: sequential loop below will retry and report errors

    ctx.pre_download_results = _pre_download_results
    ctx.pre_downloaded_keys = builtins.set(_pre_download_results.keys())
