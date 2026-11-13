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
    (#3532). An update exists to rewrite the lockfile: even an unchanged
    dependency plan can change the deployment ledger (for example new
    targets), so the refusal does not depend on the plan. Returns ``False``
    when the caller declined the plan.
    """
    if plan_callback is not None:
        plan = build_resolved_update_plan(ctx, lockfile)
        if not plan_callback(plan):
            return False
    if not (ctx.update_refs and ctx.apm_dir) or ctx.frozen or ctx.dry_run or ctx.lockfile_only:
        return True
    from apm_cli.deps.lockfile import get_lockfile_path, refuse_lockfile_before_mutation

    refuse_lockfile_before_mutation(lockfile, get_lockfile_path(ctx.apm_dir), would_mutate=True)
    return True


def build_resolved_update_plan(ctx: InstallContext, lockfile: LockFile | None) -> UpdatePlan:
    """Supply validated, staged local replacement hashes to the pure plan owner."""
    from apm_cli.install.plan import build_update_plan
    from apm_cli.utils.content_hash import compute_package_hash

    for dependency in ctx.deps_to_install:
        if dependency.replacement is not None and dependency.is_local:
            ctx.package_hashes[dependency.get_unique_key()] = compute_package_hash(
                dependency.get_install_path(ctx.apm_modules_dir)
            )
    return build_update_plan(
        lockfile,
        ctx.deps_to_install,
        complete_resolved_dep_keys=ctx.update_plan_complete_dep_keys if ctx.only_packages else None,
        resolved_content_hashes=ctx.package_hashes,
    )
