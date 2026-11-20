"""Re-check local-bundle hooks against current executable trust (#4027).

A local bundle's hooks were admitted by one exact content-bound approval key,
recorded with their ownership marker (see
:func:`~apm_cli.integration.hook_ownership.local_bundle_hook_source_marker`).
Every install, update, prune and ``apm audit --ci`` asks this module whether
that key is still granted under the current project, user and org layers.
Hooks whose key is no longer granted, or whose marker records no key, are not
admitted: install retires their registration through the hook integrator's
local-bundle retirement owner, and audit reports them first.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REASON_UNRECORDED = "no approval record"
REASON_NOT_GRANTED = "approval no longer granted"


@dataclass(frozen=True)
class UnadmittedBundleHooks:
    """One deployed local bundle whose hooks the current trust does not admit."""

    slug: str
    reason: str


def _merged_hook_documents(project_root: Path, targets: Any):
    """Yield ``(config, sidecar, container)`` for every merged hook target."""
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR, _MERGE_HOOK_TARGETS
    from apm_cli.integration.hook_ownership import read_hook_object

    for target in targets or ():
        config = _MERGE_HOOK_TARGETS.get(getattr(target, "name", None))
        if config is None:
            continue
        json_path = Path(project_root) / target.root_dir / config.config_filename
        sidecar_path = json_path.parent / _APM_HOOKS_SIDECAR
        try:
            yield (
                read_hook_object(json_path),
                read_hook_object(sidecar_path),
                config.event_container_key,
            )
        except (OSError, ValueError, TypeError):
            # Unreadable ownership state cannot prove an approval.
            yield None, None, config.event_container_key


def deployed_bundle_hook_keys(project_root: Path, targets: Any) -> dict[str, set[str | None]]:
    """Return each deployed local bundle slug and the approval keys its hooks record."""
    from apm_cli.integration.hook_ownership import (
        parse_local_bundle_hook_source,
        reinject_apm_source_from_sidecar,
    )

    found: dict[str, set[str | None]] = {}
    for data, sidecar, container in _merged_hook_documents(project_root, targets):
        if data is None:
            continue
        hooks = data.get(container)
        if not isinstance(hooks, dict):
            continue
        if sidecar:
            reinject_apm_source_from_sidecar(hooks, sidecar)
        for entries in hooks.values():
            for entry in entries if isinstance(entries, list) else ():
                parsed = (
                    parse_local_bundle_hook_source(entry.get("_apm_source"))
                    if isinstance(entry, dict)
                    else None
                )
                if parsed is not None:
                    found.setdefault(parsed[0], set()).add(parsed[1])
    return found


def unadmitted_bundle_hooks(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]] | None],
) -> list[UnadmittedBundleHooks]:
    """Return deployed local-bundle hooks the current trust does not admit.

    *allow_executables* returns the effective deny-wins map, and is asked
    only when a local bundle has deployed hooks; ``None`` means the
    executables gate is off, which admits every hook as installs do.
    """
    deployed = deployed_bundle_hook_keys(project_root, targets)
    if not deployed:
        return []
    allow_map = allow_executables()
    if allow_map is None:
        return []
    from apm_cli.security.executables import EXEC_TYPE_HOOKS, is_package_approved

    result: list[UnadmittedBundleHooks] = []
    for slug, keys in sorted(deployed.items()):
        if None in keys:
            result.append(UnadmittedBundleHooks(slug, REASON_UNRECORDED))
        elif not all(is_package_approved(allow_map, key, EXEC_TYPE_HOOKS) for key in keys):
            result.append(UnadmittedBundleHooks(slug, REASON_NOT_GRANTED))
    return result


def retire_unadmitted_bundle_hooks(
    project_root: Path,
    targets: Any,
    allow_executables: Callable[[], dict[str, dict[str, bool]] | None],
    *,
    logger: Any = None,
    dry_run: bool = False,
) -> list[UnadmittedBundleHooks]:
    """Retire the registration of every local-bundle hook trust no longer admits."""
    unadmitted = unadmitted_bundle_hooks(project_root, targets, allow_executables)
    if not unadmitted:
        return []
    from apm_cli.integration.hook_integrator import HookIntegrator

    hook_targets = [target for target in targets if target.supports("hooks")]
    for item in unadmitted:
        if not dry_run:
            HookIntegrator().retire_local_bundle_hooks(item.slug, project_root, hook_targets)
        if logger is not None:
            verb = "Would retire" if dry_run else "Retired"
            logger.warning(
                f"{verb} hooks from local bundle '{item.slug}' ({item.reason}). "
                "To keep them, approve the bundle and rerun 'apm install <bundle>'."
            )
    return unadmitted


def scope_bundle_hook_inputs(scope: Any, apm_package: Any, *, no_policy: bool, logger: Any):
    """Return ``(deploy_root, targets, allow_provider)`` for one command scope."""
    from apm_cli.core.scope import get_deploy_root, is_user_scope
    from apm_cli.integration.targets import resolve_targets
    from apm_cli.security.executables import resolve_project_exec_allow_map

    root = get_deploy_root(scope)
    user_scope = is_user_scope(scope)
    explicit = list(getattr(apm_package, "canonical_targets", None) or ()) or None
    targets = resolve_targets(root, user_scope=user_scope, explicit_target=explicit)

    def allow_provider() -> dict[str, dict[str, bool]] | None:
        return resolve_project_exec_allow_map(
            root, apm_package, user_scope=user_scope, no_policy=no_policy, logger=logger
        )

    return root, targets, allow_provider


def reconcile_scope_bundle_hooks(
    scope: Any,
    apm_package: Any,
    *,
    no_policy: bool = False,
    logger: Any = None,
    dry_run: bool = False,
) -> list[UnadmittedBundleHooks]:
    """Re-check one scope's local-bundle hooks; install, update and prune call this."""
    root, targets, allow_provider = scope_bundle_hook_inputs(
        scope, apm_package, no_policy=no_policy, logger=logger
    )
    return retire_unadmitted_bundle_hooks(
        root, targets, allow_provider, logger=logger, dry_run=dry_run
    )
