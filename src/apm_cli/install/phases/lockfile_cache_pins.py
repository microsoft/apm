"""Cache pin marker sync for the lockfile phase.

Writes the ``.apm-pin`` markers (:mod:`apm_cli.install.cache_pin`) that
let a later ``apm audit`` drift replay trust cached remote dependencies.
Every function is best-effort: a marker write failure never undoes
lockfile assembly or aborts the install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext


def sync_cache_pin_markers(ctx: InstallContext, lockfile: LockFile) -> None:
    """Write ``.apm-pin`` markers for every cached remote dep.

    Idempotent and best-effort: a missing or unwritable cache
    directory is silently skipped at the marker-helper level and
    will surface during the next ``apm audit`` drift replay.
    Wrapped in a broad except because lockfile assembly success
    must not be undone by a marker write failure.
    """
    try:
        from apm_cli.install.cache_pin import sync_markers_for_lockfile

        apm_modules_dir = ctx.apm_modules_dir
        if apm_modules_dir is None:
            return
        written = sync_markers_for_lockfile(lockfile, ctx.project_root, apm_modules_dir)
        if ctx.logger and written:
            ctx.logger.verbose_detail(f"Wrote {written} cache pin marker(s) for drift replay")
    except Exception as exc:
        if ctx.logger:
            ctx.logger.verbose_detail(f"Cache pin marker sync skipped: {exc}")


def sync_cache_pin_markers_from_existing(ctx: InstallContext) -> None:
    """Self-heal markers from the pre-existing in-memory lockfile.

    Runs at the very start of ``build_and_save`` so that remote deps
    recorded in the on-disk lockfile get their ``.apm-pin`` markers
    even when the imminent build prunes them (orphan removal, #1926):
    the pruned dep's cache remains under ``apm_modules/`` and the
    supply-chain contract (#1137) requires every cached remote dep to
    be marked. Warnings are suppressed here -- the primary post-build
    sync over the freshly-built lockfile surfaces unpinned deps, so a
    second warning pass would be noise. Best-effort: a marker write
    failure must never abort the install.
    """
    existing = ctx.existing_lockfile
    if existing is None:
        return
    try:
        from apm_cli.install.cache_pin import sync_markers_for_lockfile

        apm_modules_dir = ctx.apm_modules_dir
        if apm_modules_dir is None:
            return
        written = sync_markers_for_lockfile(
            existing,
            ctx.project_root,
            apm_modules_dir,
            warn_unpinned=False,
        )
        if ctx.logger and written:
            ctx.logger.verbose_detail(
                f"Self-healed {written} cache pin marker(s) from existing lockfile"
            )
    except Exception as exc:
        if ctx.logger:
            ctx.logger.verbose_detail(f"Cache pin marker self-heal (existing) skipped: {exc}")


def sync_cache_pin_markers_from_disk(ctx: InstallContext) -> None:
    """Self-heal markers from the on-disk lockfile when no install ran.

    This handles the upgrade path: user installed an older APM,
    runs the new APM with no manifest changes, expects the next
    ``apm audit`` to find every remote dep correctly marked.
    """
    try:
        from apm_cli.deps.lockfile import LockFile as _LF
        from apm_cli.deps.lockfile import get_lockfile_path

        lockfile_path = get_lockfile_path(ctx.apm_dir)
        if not lockfile_path.exists():
            return
        lockfile = _LF.load_or_create(lockfile_path)
        sync_cache_pin_markers(ctx, lockfile)
    except Exception as exc:
        if ctx.logger:
            ctx.logger.verbose_detail(f"Cache pin marker self-heal skipped: {exc}")
