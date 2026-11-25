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

    integrator = HookIntegrator()
    found: set[str] = set()
    for target in targets or ():
        if getattr(target, "name", None) != "copilot":
            continue
        try:
            _require_readable_hook_dirs(Path(package_info.install_path))
            destinations = copilot_hook_destinations(integrator, package_info, project_root, target)
        except (OSError, ValueError) as exc:
            raise HookPathsUnknown(str(exc)) from exc
        for path in destinations:
            # Lexical, never resolved: a symlink is named as the link itself.
            rel = path.relative_to(project_root).as_posix()
            if rel in recorded or not present_hook_path(path, rel):
                continue
            _require_parent_inside(path, project_root, rel)
            found.add(rel)
    return sorted(found)


def present_hook_path(path: Path, rel: str) -> bool:
    """Whether a file or link sits at *path*; an unreadable location is unchecked.

    ``lstat`` never follows the leaf; any failure other than a missing path
    raises :class:`HookPathsUnknown` instead of reading as absent.
    """
    import stat

    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise HookPathsUnknown(f"{rel} cannot be inspected: {exc}") from exc
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def _require_readable_hook_dirs(package_root: Path) -> None:
    """Raise ``OSError`` for a hook source directory that exists but cannot be listed.

    A glob over an unreadable directory finds nothing, which would read as
    "no hooks" and skip the package.
    """
    import os

    for hooks_dir in (package_root / ".apm" / "hooks", package_root / "hooks"):
        if hooks_dir.is_dir():
            with os.scandir(hooks_dir) as entries:
                next(entries, None)


def _require_parent_inside(path: Path, project_root: Path, rel: str) -> None:
    """Raise :class:`HookPathsUnknown` when *path*'s directory leaves the project.

    Only the parent chain is resolved, never the leaf, so a symlinked hook
    file is still named as itself; a symlinked hooks directory pointing
    elsewhere is never named for deletion.
    """
    try:
        inside = path.parent.resolve().is_relative_to(project_root.resolve())
    except (OSError, RuntimeError):
        inside = False
    if not inside:
        raise HookPathsUnknown(
            f"the directory of {rel} resolves outside the project; inspect it manually"
        )


def locked_withheld_hook_files(
    lockfile: Any,
    modules_dir: Path,
    trust_ctx: Callable[[], Any],
    project_root: Path,
    targets: Iterable[Any],
    *,
    user_scope: bool = False,
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
    from apm_cli.install.withheld_merged_hooks import ENTRY_REASON, withheld_merged_hook_entries
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
            _require_readable_hook_dirs(install_path)
            package_info = _build_package_info(dep, install_path)
            status, allowed = current_exec_decision(trust_ctx, package_info, dep.get_unique_key())
        except (ExecPolicyUnavailable, OSError, ValueError) as exc:
            subset = effective_dependency_selection(dep_ref).target_subset
            if any(
                target.name in _scanned_target_names() and (not subset or target.name in subset)
                for target in active
            ):
                found.append(("", key, f"its executables decision cannot be read: {exc}"))
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
            entries = withheld_merged_hook_entries(
                package_info,
                project_root,
                package_targets,
                user_scope=user_scope,
                # The lock records the hooks as deployed, as locked_hooks_retired reads it.
                receipted=dep.exec_status in {None, TRUST_DEPLOYED}
                or EXEC_TYPE_HOOKS in tuple(dep.exec_allowed_types or ()),
            )
        except HookPathsUnknown as exc:
            found.append(("", key, f"hooks {reason}; its hook paths cannot be checked: {exc}"))
            continue
        found.extend((path, key, f"hooks {reason}") for path in paths)
        found.extend((entry, key, f"hooks {reason}; {ENTRY_REASON}") for entry in entries)
    return found


def _scanned_target_names() -> frozenset[str]:
    """Targets whose hook destinations the scan inspects: Copilot files and merged configs."""
    from apm_cli.integration.hook_integrator import _MERGE_HOOK_TARGETS

    return frozenset({"copilot", *_MERGE_HOOK_TARGETS})


def announce_withheld_hook_files(
    package_name: str,
    package_info: Any,
    project_root: Path,
    targets: Iterable[Any],
    managed_files: Any,
    logger: Any | None,
    *,
    user_scope: bool = False,
) -> None:
    """Warn, naming the path, for each unrecorded file at a withheld package's hook path.

    *targets* are the package's effective install targets. Nothing is deleted.
    """
    if logger is None:
        return
    from apm_cli.core.remediation import (
        WITHHELD_HOOK_ENTRY_UNRECORDED,
        WITHHELD_HOOK_FILE_UNRECORDED,
        WITHHELD_HOOK_PATHS_UNCHECKED,
        kept_file_message,
    )
    from apm_cli.install.withheld_merged_hooks import withheld_merged_hook_entries

    package = getattr(package_info, "package", None)
    label = getattr(package, "name", None) or package_name or "unknown"
    try:
        paths = withheld_hook_files(
            package_info, project_root, targets, recorded=set(managed_files or ())
        )
        entries = withheld_merged_hook_entries(
            package_info, project_root, targets, user_scope=user_scope
        )
    except HookPathsUnknown as exc:
        logger.warning(
            f"{label}: cannot check its hook paths for files apm.lock.yaml does not record "
            f"({exc}). {WITHHELD_HOOK_PATHS_UNCHECKED.sentence()}",
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
    for entry in entries:
        logger.warning(
            kept_file_message(
                f"hook entry {entry}",
                f"it matches {label}'s hook, the executables gate withholds {label}'s "
                "hooks, and no ownership record attributes it to another source",
                WITHHELD_HOOK_ENTRY_UNRECORDED,
            ),
            symbol="warning",
        )
