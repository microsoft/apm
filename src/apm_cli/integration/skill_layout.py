"""Effective skill layout owner (#3773).

An install selects where skills land: the converged ``.agents/skills`` root
(default) or each client's own root (``--legacy-skill-paths`` /
``APM_LEGACY_SKILL_PATHS``). Nothing persists that choice except the deployed
paths the lockfile records, so every later flagless rebuild of recorded
deployments (audit drift replay, uninstall cleanup and survivor
re-integration) recovers the layout from those paths here.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from apm_cli.integration import targets as _targets

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.targets import TargetProfile


def _records_legacy_skill_layout(profile: TargetProfile, recorded: tuple[str, ...]) -> bool:
    """Return whether *recorded* places skills under the profile's per-client root."""
    mapping = profile.primitives.get("skills")
    if mapping is None or mapping.deploy_root is None:
        return False
    prefix = f"{profile.root_dir}/{mapping.subdir}/".replace("\\", "/")
    return any(path.startswith(prefix) for path in recorded)


def resolve_skill_layout(
    targets: Iterable[TargetProfile],
    *,
    legacy_skill_paths: bool = False,
    recorded_paths: Iterable[str] = (),
) -> list[TargetProfile]:
    """Return *targets* in their effective skill layout.

    Install passes its flag as *legacy_skill_paths*. Flagless rebuilds pass
    the lockfile's deployed paths as *recorded_paths* instead: a target whose
    recorded skills live under its per-client root keeps that layout, every
    other target keeps the converged root.
    """
    profiles = list(targets)
    if legacy_skill_paths:
        return _targets.apply_legacy_skill_paths(profiles)
    recorded = tuple(path.replace("\\", "/") for path in recorded_paths)
    if not recorded:
        return profiles
    return [
        _targets.apply_legacy_skill_paths([profile])[0]
        if _records_legacy_skill_layout(profile, recorded)
        else profile
        for profile in profiles
    ]


def recorded_deployed_paths(lockfile: LockFile | None) -> tuple[str, ...]:
    """Return every deployed path the lockfile records, for layout recovery."""
    if lockfile is None:
        return ()
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    return tuple(DeploymentLedgerCodec.legacy_deployed_file_claims(lockfile))


def resolve_reintegration_targets(
    project_root,
    *,
    user_scope: bool = False,
    explicit_target: str | list[str] | None = None,
    recorded_paths: Iterable[str] = (),
    create_config: bool = True,
) -> list[TargetProfile]:
    """Resolve targets for a flagless rebuild of recorded deployments."""
    return resolve_skill_layout(
        _targets.resolve_targets(
            project_root,
            user_scope=user_scope,
            explicit_target=explicit_target,
            create_config=create_config,
        ),
        recorded_paths=recorded_paths,
    )


__all__ = [
    "recorded_deployed_paths",
    "resolve_reintegration_targets",
    "resolve_skill_layout",
]
