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


def run(ctx: InstallContext) -> None:
    """Execute the parallel download phase.

    On return ``ctx.pre_download_results`` and ``ctx.pre_downloaded_keys``
    are populated.
    """
    # Module-attribute access for late-patchability (same pattern as
    # resolve.py).  detect_ref_change / build_download_ref live in
    # apm_cli.drift and tests import them from there, so direct import
    # is safe here -- no test patches at apm_cli.commands.install.X.
    from apm_cli.drift import (
        LOCKED_COPY_CONTENT_HASH,
        build_download_ref,
        detect_ref_change,
        locked_copy_proof,
    )
    from apm_cli.install.transaction import stage_existing_copy

    deps_to_install = ctx.deps_to_install
    existing_lockfile = ctx.existing_lockfile
    update_refs = ctx.update_refs
    parallel_downloads = ctx.parallel_downloads
    apm_modules_dir = ctx.apm_modules_dir
    if apm_modules_dir is None:
        raise RuntimeError("Resolution must set apm_modules_dir before download")
    downloader = ctx.downloader
    callback_downloaded = ctx.callback_downloaded
    callback_failures = ctx.callback_failures

    # Phase 4 (#171): Parallel package downloads using ThreadPoolExecutor
    # Pre-download all non-cached packages in parallel for wall-clock speedup.
    # Results are stored and consumed by the sequential integration loop below.
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import as_completed as _futures_completed

    _pre_download_results = {}  # dep_key -> PackageInfo
    _need_download = []
    for _pd_ref in deps_to_install:
        _pd_key = _pd_ref.get_unique_key()
        _pd_path = _pd_ref.get_install_path(apm_modules_dir)
        # Skip local packages -- they are copied, not downloaded
        if _pd_ref.is_local:
            continue
        # Skip deps that already failed during BFS resolution (#1111 C2).
        # Without this, registry failures fall through to the git downloader
        # and hang on a non-existent github.com/{owner}/{repo} clone (~180s).
        if _pd_key in callback_failures:
            continue
        # Registry-sourced deps are fetched in resolve.py's callback; the git
        # downloader must never handle them here.
        if getattr(_pd_ref, "source", None) == "registry":
            continue
        # Skip if already downloaded during BFS resolution
        if _pd_key in callback_downloaded:
            continue
        # Detect if manifest ref changed from what's recorded in the lockfile.
        # detect_ref_change() handles all transitions including None->ref.
        _pd_locked_chk = existing_lockfile.get_dependency(_pd_key) if existing_lockfile else None
        _pd_ref_changed = detect_ref_change(_pd_ref, _pd_locked_chk, update_refs=update_refs)
        # Skip when the copy on disk already holds the locked content, as
        # decided by the canonical copy-validity owner (#3884).
        # Normal mode: only when the ref hasn't changed in the manifest.
        # Update mode: defer to the sequential loop which resolves the
        # remote ref and compares -- if unchanged, the download is skipped
        # entirely; if changed, it falls back to sequential download.
        # Content-hash-only rows have no commit anchor, so update mode never
        # reuses them here.
        _pd_has_commit = bool(
            _pd_locked_chk
            and _pd_locked_chk.resolved_commit
            and _pd_locked_chk.resolved_commit != "cached"
        )
        _pd_reuse_allowed = (
            (update_refs or not _pd_ref_changed)
            if _pd_has_commit
            else bool(_pd_locked_chk and _pd_locked_chk.content_hash)
            and not update_refs
            and not _pd_ref_changed
        )
        if _pd_path.exists() and _pd_reuse_allowed:
            _pd_proof = locked_copy_proof(_pd_locked_chk, _pd_path)
            if _pd_proof == LOCKED_COPY_CONTENT_HASH:
                ctx.content_hash_verified_deps.add(_pd_key)
            if _pd_proof is not None:
                continue
            _report_cache_mismatch(ctx, _pd_key, _pd_locked_chk, _pd_path)
        # Build download ref (use locked commit for reproducibility).
        # build_download_ref() uses the manifest ref when ref_changed is True.
        _pd_dlref = build_download_ref(
            _pd_ref, existing_lockfile, update_refs=update_refs, ref_changed=_pd_ref_changed
        )
        if parallel_downloads > 0:
            stage_existing_copy(ctx, _pd_path, _pd_locked_chk)
        _need_download.append((_pd_ref, _pd_path, _pd_dlref))

    if _need_download and parallel_downloads > 0:
        _max_workers = min(parallel_downloads, len(_need_download))
        with ThreadPoolExecutor(max_workers=_max_workers) as _executor:
            _futures = {}
            for _pd_ref, _pd_path, _pd_dlref in _need_download:
                _pd_disp = str(_pd_ref) if _pd_ref.is_virtual else _pd_ref.repo_url
                _pd_short = _pd_disp.split("/")[-1] if "/" in _pd_disp else _pd_disp
                _pd_key = _pd_ref.get_unique_key()
                if ctx.tui is not None:
                    ctx.tui.task_started(_pd_key, f"fetch {_pd_short}")
                _pd_fut = _executor.submit(
                    downloader.download_package,
                    _pd_dlref,
                    _pd_path,
                    progress_task_id=None,
                    progress_obj=None,
                )
                _futures[_pd_fut] = (_pd_ref, _pd_disp, _pd_key)
            for _pd_fut in _futures_completed(_futures):
                _pd_ref, _pd_disp, _pd_key = _futures[_pd_fut]
                try:
                    _pd_info = _pd_fut.result()
                    _pre_download_results[_pd_key] = _pd_info
                    if ctx.tui is not None:
                        ctx.tui.task_completed(_pd_key)
                except Exception:
                    if ctx.tui is not None:
                        ctx.tui.task_failed(_pd_key)
                    # Silent: sequential loop below will retry and report errors

    ctx.pre_download_results = _pre_download_results
    ctx.pre_downloaded_keys = builtins.set(_pre_download_results.keys())


def _report_cache_mismatch(ctx: InstallContext, dep_key: str, locked_dep, path) -> None:
    """Name an apm_modules copy whose bytes contradict the lock before re-downloading."""
    from apm_cli.install.source_helpers import (
        locked_content_mismatch,
        locked_content_mismatch_line,
        observed_package_hash,
    )

    if not (locked_dep and locked_dep.content_hash and path.is_dir()):
        return
    if not locked_content_mismatch(dep_key, locked_dep, path, lockfile=ctx.existing_lockfile):
        return
    message = (
        locked_content_mismatch_line(
            dep_key, locked_dep.content_hash, observed_package_hash(locked_dep, path)
        )
        + " Re-downloading from the locked source."
    )
    logger = getattr(ctx, "logger", None)
    if ctx.diagnostics is not None:
        ctx.diagnostics.warn(message, package=dep_key)
    if logger is not None:
        logger.progress(message)
    # Only a notice that reached a sink counts as reported; otherwise the
    # integrate phase keeps its fallback report (#4351).
    reported = getattr(ctx, "cache_mismatch_reported", None)
    if isinstance(reported, set) and (ctx.diagnostics is not None or logger is not None):
        reported.add(dep_key)
