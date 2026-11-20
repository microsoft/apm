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
            Path(lockfile_path).write_bytes(lock_bytes)
        hint = "apm install --global" if is_user_scope(scope) else "apm install"
        raise ClaudeNativePluginError(
            "Package removal may already be complete, but native plugin declarations did not "
            f"converge. {exc} Reconcile remaining declarations with '{hint}' and the "
            "original --target selection; do not re-add the removed package."
        ) from exc
