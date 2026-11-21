"""Native plugin convergence after a lifecycle command removed locked state.

Uninstall and prune call this one helper so removal follows the same owner as
install (:func:`apm_cli.claude_plugins.registrar.reconcile_native_plugins`):
ownership comes from the lock before and after the command, and a failure
restores the previous lock so a retry still owns what it must retire.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def reconcile_after_uninstall(
    *,
    deploy_root: Path,
    modules_dir: Path,
    scope: Any,
    lockfile: Any,
    logger: Any,
    ownership: Mapping[str, Any] | None,
    lock_bytes: bytes | None,
    lockfile_path: Path | None,
    dry_run: bool = False,
) -> None:
    """Converge deployed roots and harness settings onto the surviving lock."""
    from apm_cli.claude_plugins.admission import ClaudeNativePluginError
    from apm_cli.claude_plugins.registrar import reconcile_native_plugins
    from apm_cli.core.scope import is_user_scope

    try:
        reconcile_native_plugins(
            modules_dir=modules_dir,
            scope=scope,
            previous=ownership,
            lockfile=lockfile,
            deploy_root=deploy_root,
            logger=logger,
            dry_run=dry_run,
        )
    except ClaudeNativePluginError as exc:
        if lock_bytes and lockfile_path is not None:
            # One rollback unit: keep the removed plugins' ownership for the retry.
            from apm_cli.deps.lockfile_write_authority import restore_lockfile_bytes

            restore_lockfile_bytes(Path(lockfile_path), lock_bytes)
        hint = "apm install --global" if is_user_scope(scope) else "apm install"
        raise ClaudeNativePluginError(
            "Package removal may already be complete, but native plugin declarations did not "
            f"converge. {exc} Reconcile remaining declarations with '{hint}' and the "
            "original --target selection; do not re-add the removed package."
        ) from exc


def retire_disallowed_native_survivors(
    lockfile: Any,
    retirements: Any,
    removed_keys: set[str],
    hooks: Mapping[str, str],
    logger: Any,
) -> set[str]:
    """Drop the lock record of each surviving native plugin a gate no longer admits.

    A plugin loads as one unit (#3921): a denied package, retired hooks or a
    whole-plugin verdict install would now park (a revoked or changed pin, a
    missing MCP grant, a blocking org MCP policy) retires all of it. Without
    its record, the native reconcile removes the root and the declarations.
    Returns the keys retired for the native verdict, which the caller rebuilds
    under install's gate.
    """
    removed = set(removed_keys)
    denied = set(retirements.denied_packages) - removed
    native = {k: why for k, why in retirements.native_packages.items() if k not in removed}
    for key, why in sorted(native.items()):
        if key not in hooks and key not in denied:
            logger.warning(f"{key} native plugin is no longer allowed ({why}); removing it")
    for key in denied | set(hooks) | set(native):
        lockfile.dependencies[key].native_plugin = None
    return set(native)
