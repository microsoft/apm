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


def _named_kinds(entry: object) -> bool:
    """Whether a hook retirement entry ``(why, kinds)`` names any kind."""
    return isinstance(entry, tuple) and len(entry) == 2 and bool(entry[1])


def retire_disallowed_native_survivors(
    lockfile: Any,
    retirements: Any,
    removed_keys: set[str],
    hooks: Mapping[str, object],
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
        # Only a hook retirement that named kinds was already reported (#4567).
        if not _named_kinds(hooks.get(key)) and key not in denied:
            logger.warning(f"{key} native plugin is no longer allowed ({why}); removing it")
    for key in denied | set(hooks) | set(native):
        lockfile.dependencies[key].native_plugin = None
    return set(native)


class NativeGateCheckError(RuntimeError):
    """The gate retirement check for locked native plugins could not run."""


def retire_gated_native_plugins(
    lockfile: Any,
    modules_dir: Path,
    manifest_path: Path,
    deploy_root: Path,
    logger: Any,
    *,
    dry_run: bool = False,
) -> frozenset[str]:
    """Drop the record of each native plugin the current gates no longer admit (prune).

    ``apm prune`` skips install's gate, so without this a revoked or denied
    plugin would stay declared, and a missing root would be redeployed from its
    locked bytes. The verdict is the gate retirement owner's, as for uninstall;
    the native reconcile then removes the root and its declarations. Returns
    the keys whose record was (or, with *dry_run*, would be) dropped, so the
    caller persists the lock. With *dry_run* the lock is left untouched and
    each root retirement is reported as a dry-run notice; the caller previews
    the settings with :func:`preview_native_settings`. A failure to evaluate the gate
    raises :class:`NativeGateCheckError`: prune must not keep a plugin the gate
    may no longer admit without saying so (#4754).
    """
    from apm_cli.install.exec_gate import gate_retirements
    from apm_cli.models.apm_package import APMPackage

    if lockfile is None or not any(d.native_plugin for d in lockfile.dependencies.values()):
        return frozenset()
    try:
        apm_package = APMPackage.from_apm_yml(manifest_path)
        retirements = gate_retirements(
            lockfile,
            modules_dir,
            apm_package,
            deploy_root,
            user_scope=False,
            migrate_user_legacy=not dry_run,
        )
    except Exception as exc:
        raise NativeGateCheckError(
            "Could not check locked native plugins against the current approvals "
            f"and policy: {type(exc).__name__}: {exc}"
        ) from exc
    if retirements is None:
        return frozenset()
    if dry_run:
        return _preview_gated_native_roots(lockfile, retirements, deploy_root, logger)
    before = {k for k, d in lockfile.dependencies.items() if d.native_plugin is not None}
    hooks = dict(retirements.hook_packages)
    retire_disallowed_native_survivors(lockfile, retirements, set(), hooks, logger)
    retired = before - {k for k, d in lockfile.dependencies.items() if d.native_plugin is not None}
    denied = set(retirements.denied_packages)
    for key in sorted(retired & (set(hooks) | denied)):
        # The survivor helper already named this one with its reason.
        if key in retirements.native_packages and not _named_kinds(hooks.get(key)):
            if key not in denied:
                continue
        logger.warning(f"{key} native plugin is no longer allowed; removing it")
    return frozenset(retired)


def _preview_gated_native_roots(
    lockfile: Any, retirements: Any, deploy_root: Path, logger: Any
) -> frozenset[str]:
    """Report the native roots a real prune would retire for the gate."""
    from apm_cli.claude_plugins.registrar import native_ownership, preview_project_root_retirement
    from apm_cli.native_plugins.record import PLUGINS_RELATIVE

    owned = native_ownership(lockfile)
    reasons = {
        **retirements.native_packages,
        **retirements.hook_packages,
        **retirements.denied_packages,
    }
    retiring = sorted(set(reasons) & set(owned))
    for key in retiring:
        record = owned[key].record
        root = f"{PLUGINS_RELATIVE}/{record.name}"
        root_removed = preview_project_root_retirement(record, deploy_root)
        if root_removed:
            action = f"remove {root}"
        elif root_removed is False:
            action = f"keep {root} (changed since deploy; remove it manually)"
        else:
            action = f"{root} is not deployed"
        logger.dry_run_notice(f"retire native plugin {record.name} ({reasons[key]}): {action}")
    return frozenset(retiring)


def preview_native_settings(
    lockfile: Any, removed_keys: set[str], modules_dir: Path, deploy_root: Path, logger: Any
) -> None:
    """Report the settings files the prune's native reconcile would change.

    *removed_keys* are every dependency row or native record the planned
    prune drops (orphans and gate retirements), so the preview plans the same
    lock the real reconcile sees.
    """
    import copy

    from apm_cli.claude_plugins.registrar import (
        native_ownership,
        preview_project_settings_changes,
    )

    if lockfile is None or not any(d.native_plugin for d in lockfile.dependencies.values()):
        return
    following = copy.deepcopy(lockfile)
    for key in removed_keys:
        following.dependencies.pop(key, None)
    try:
        settings = preview_project_settings_changes(
            modules_dir=modules_dir,
            previous=native_ownership(lockfile),
            lockfile=following,
            deploy_root=deploy_root,
        )
    except Exception as exc:
        logger.warning(f"Cannot preview native plugin settings changes: {exc}")
        return
    if settings:
        logger.dry_run_notice(f"update native plugin declarations in {', '.join(settings)}")
