"""Files at a withheld package's hook path that the lockfile does not record.

When the executables gate withholds a package's hooks (a deny, or no
approval), a file at one of that package's own Copilot hook destinations
that ``apm.lock.yaml`` does not record cannot be proven APM's, so it is never
deleted. ``apm install`` keeps and names it
(:func:`announce_withheld_hook_files`) and ``apm audit --ci`` fails on it
(:func:`locked_withheld_hook_files`, the ``hook-approval-pins`` check). Both
judge the package with the install gate's own verdict
(:func:`~apm_cli.install.exec_gate.current_exec_decision`). Read-only.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


class HookPathsUnknown(Exception):
    """A withheld package's hook paths cannot be computed, so none can be cleared."""


def withheld_hook_files(
    package_info: Any, project_root: Path, targets: Iterable[Any], *, recorded: Any
) -> list[str]:
    """Unrecorded files at the Copilot hook paths *package_info*'s own sources deploy to.

    Sources come from the read-only shared selector, so a package whose
    hook routing install would refuse is still inspected. Raises
    :class:`HookPathsUnknown` when the paths cannot be computed: callers
    fail closed rather than report nothing.
    """
    from apm_cli.integration.copilot_hook_paths import copilot_hook_destinations
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.utils.paths import portable_relpath

    integrator = HookIntegrator()
    found: set[str] = set()
    for target in targets or ():
        if getattr(target, "name", None) != "copilot":
            continue
        try:
            destinations = copilot_hook_destinations(integrator, package_info, project_root, target)
        except (OSError, ValueError) as exc:
            raise HookPathsUnknown(str(exc)) from exc
        for path in destinations:
            rel = portable_relpath(path, project_root)
            if rel not in recorded and (path.is_file() or path.is_symlink()):
                found.add(rel)
    return sorted(found)


def locked_withheld_hook_files(
    lockfile: Any,
    modules_dir: Path,
    trust_ctx: Callable[[], Any],
    project_root: Path,
    targets: Iterable[Any],
) -> list[tuple[str, str, str]]:
    """``(path, lock key, reason)`` per unrecorded file at a withheld package's hook path.

    A package whose hook paths cannot be computed yields one row with an
    empty path and the reason, so the audit fails closed.

    Each package is checked only on the targets install would deploy it to
    (:func:`~apm_cli.install.target_filter.resolve_effective_package_targets`
    with its persisted dependency selection). A package not materialized
    under *modules_dir* is left to the checks that report it.
    """
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.install.drift import _build_package_info
    from apm_cli.install.exec_gate import ExecPolicyUnavailable, current_exec_decision
    from apm_cli.install.package_resolution import effective_dependency_selection
    from apm_cli.install.target_filter import resolve_effective_package_targets
    from apm_cli.security.executables import (
        EXEC_TYPE_HOOKS,
        TRUST_ABSENT,
        TRUST_DENIED,
        TRUST_DEPLOYED,
    )

    if lockfile is None:
        return []
    active = list(targets or ())
    recorded = set(DeploymentLedgerCodec.legacy_deployed_file_claims(lockfile))
    found: list[tuple[str, str, str]] = []
    for key, dep in sorted((getattr(lockfile, "dependencies", None) or {}).items()):
        if key == "." or dep.exec_status == TRUST_ABSENT:
            continue
        dep_ref = dep.to_dependency_ref()
        install_path = dep_ref.get_install_path(modules_dir)
        if not install_path.is_dir():
            continue
        try:
            package_info = _build_package_info(dep, install_path)
            status, allowed = current_exec_decision(trust_ctx, package_info, dep.get_unique_key())
        except (ExecPolicyUnavailable, OSError, ValueError):
            continue
        if status in {None, TRUST_DEPLOYED} or EXEC_TYPE_HOOKS in allowed:
            continue
        package_targets = resolve_effective_package_targets(
            active,
            effective_dependency_selection(dep_ref).target_subset,
            package_info,
            None,
            dep_ref.get_identity(),
        ).targets
        reason = "denied" if status == TRUST_DENIED else "not approved"
        try:
            paths = withheld_hook_files(
                package_info, project_root, package_targets, recorded=recorded
            )
        except HookPathsUnknown as exc:
            found.append(("", key, f"hooks {reason}; its hook paths cannot be checked: {exc}"))
            continue
        found.extend((path, key, f"hooks {reason}") for path in paths)
    return found


def announce_withheld_hook_files(
    package_name: str,
    package_info: Any,
    project_root: Path,
    targets: Iterable[Any],
    managed_files: Any,
    logger: Any | None,
) -> None:
    """Warn, naming the path, for each unrecorded file at a withheld package's hook path.

    *targets* are the package's effective install targets. Nothing is deleted.
    """
    if logger is None:
        return
    from apm_cli.core.remediation import WITHHELD_HOOK_FILE_UNRECORDED, kept_file_message

    package = getattr(package_info, "package", None)
    label = getattr(package, "name", None) or package_name or "unknown"
    try:
        paths = withheld_hook_files(
            package_info, project_root, targets, recorded=set(managed_files or ())
        )
    except HookPathsUnknown as exc:
        logger.warning(
            f"{label}: cannot check its hook paths for files apm.lock.yaml does not record "
            f"({exc}); 'apm audit --ci' fails until they can be checked.",
            symbol="warning",
        )
        return
    for rel in paths:
        logger.warning(
            kept_file_message(
                rel,
                f"it sits at {label}'s hook path, the executables gate withholds {label}'s "
                "hooks, and apm.lock.yaml does not record the file",
                WITHHELD_HOOK_FILE_UNRECORDED,
            ),
            symbol="warning",
        )
