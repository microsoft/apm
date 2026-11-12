"""Prepare resolved source inputs for the canonical install plan."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext
    from apm_cli.install.plan import UpdatePlan


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
