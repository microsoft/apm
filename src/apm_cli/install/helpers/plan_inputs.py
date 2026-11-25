"""Prepare resolved source inputs for the canonical install plan."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext
    from apm_cli.install.plan import UpdatePlan


def confirm_update_plan(
    ctx: InstallContext,
    lockfile: LockFile | None,
    plan_callback: Callable[[UpdatePlan], bool] | None,
) -> bool:
    """Gate an update before any download, deploy or cleanup side effect.

    Runs the caller's plan confirmation (``apm update``), then refuses an
    unwritable (symlinked) lockfile while the transaction is still a no-op
    (#3532, #4168). An update exists to rewrite the lockfile: even an
    unchanged dependency plan can change the deployment ledger (for example
    new targets), so an update refuses regardless of its plan. An install
    refuses when it adds or removes a dependency; an unchanged install still
    reads through it. Returns ``False`` when the caller
    declined the plan. A dependency of the selected graph, direct or
    transitive, that failed to resolve (a deleted local path) fails the update
    here, as install does, rather than being reported as current (#5463).
    """
    if plan_callback is not None:
        from apm_cli.install.outcome import (
            raise_selected_dependency_failure,
            selected_dependency_failed,
        )

        if selected_dependency_failed(ctx):
            raise_selected_dependency_failure(ctx)
        plan = build_resolved_update_plan(ctx, lockfile)
        if not plan_callback(plan):
            return False
    from apm_cli.install.service import InstallService

    if (
        not ctx.apm_dir
        or InstallService.frozen_disposition(ctx).replay
        or ctx.dry_run
        or ctx.lockfile_only
    ):
        return True
    from apm_cli.deps.lockfile import get_lockfile_path, refuse_lockfile_before_mutation

    if lockfile is None:
        return True
    refuse_lockfile_before_mutation(
        lockfile,
        get_lockfile_path(ctx.apm_dir),
        would_mutate=writes_lockfile(ctx, lockfile, updating=plan_callback is not None),
    )
    return True


def writes_lockfile(ctx: InstallContext, lockfile: LockFile, *, updating: bool) -> bool:
    """Whether this install rewrites *lockfile* (#4168).

    An update rewrites it (even an unchanged plan can change the deployment
    ledger). An install without ``--update`` re-resolves locked dependencies
    to the same rows; adding or removing a dependency changes the lockfile.
    """
    if updating or ctx.update_refs:
        return True
    return any(
        entry.action in {"add", "remove"}
        for entry in build_resolved_update_plan(ctx, lockfile).entries
    )


def build_resolved_update_plan(ctx: InstallContext, lockfile: LockFile | None) -> UpdatePlan:
    """Supply validated, staged local package hashes to the pure plan owner.

    An update hashes each local copy as resolve made it, before validation
    normalizes it (the image install locks), so an edited local package is
    a planned change and an unchanged one is not (#5463).
    """
    from apm_cli.install.plan import build_update_plan
    from apm_cli.install.source_helpers import package_digests, record_package_digests

    for dependency in ctx.deps_to_install:
        if not dependency.is_local:
            continue
        key = dependency.get_unique_key()
        digests = ctx.local_copy_digests.get(key)
        if digests is None and dependency.replacement is not None:
            digests = package_digests(ctx, dependency.get_install_path(ctx.apm_modules_dir))
        if digests is not None:
            record_package_digests(ctx, key, digests)
    return build_update_plan(
        lockfile,
        ctx.deps_to_install,
        complete_resolved_dep_keys=ctx.update_plan_complete_dep_keys if ctx.only_packages else None,
        resolved_content_hashes=ctx.package_hashes,
    )
