"""Carry forward the lock row of a native plugin a narrowed run never evaluated."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from apm_cli.native_plugins.scopes import carried_native_row, record_harnesses

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile


def carry_unevaluated_native_plugins(ctx: Any, lockfile: LockFile, existing: LockFile) -> None:
    """Keep the previous row of a native plugin this run's targets never reached.

    Per dependency (``carried_native_row``): ``--target copilot`` never
    evaluates a ``targets: [claude]`` plugin, so its previous row carries
    forward whole and integration deployed nothing for it this run; its
    deployed root, declarations and loose files stay as the row records
    (req-na-005's "keep the previous entry"). When its source changed, a
    default-visible note says the update waits.
    """
    run_targets = {target.name for target in ctx.targets or ()}
    carried = {}
    for dep_key, locked in lockfile.dependencies.items():
        if locked.native_plugin is not None:
            continue
        prior = carried_native_row(existing, dep_key, locked.target_subset, run_targets)
        if prior is None:
            continue
        carried[dep_key] = copy.deepcopy(prior)
        if (prior.resolved_commit, prior.content_hash) == (
            locked.resolved_commit,
            locked.content_hash,
        ):
            continue
        harnesses = record_harnesses(prior.package_type, prior.native_plugin.marketplace_path)
        if locked.target_subset:
            harnesses &= frozenset(locked.target_subset)
        message = (
            f"Kept the previous version of native plugin '{prior.native_plugin.name}': "
            f"this run's targets do not include {', '.join(sorted(harnesses))}, so its "
            "update was not applied. Run 'apm install' without --target to update it."
        )
        diagnostics = getattr(ctx, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.warn(message, package=dep_key)
        elif getattr(ctx, "logger", None) is not None:
            ctx.logger.warning(message)
    lockfile.dependencies.update(carried)
