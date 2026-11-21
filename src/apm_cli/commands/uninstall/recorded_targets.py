"""Targets uninstall reconciles after apm.yml changes its targets (#4636)."""

from __future__ import annotations

from typing import Any


def lock_recorded_targets(targets: list[Any], lockfile: Any) -> tuple[list[Any], list[str]]:
    """Split *targets* into the ones apm.lock.yaml records and the new ones.

    Uninstall reconciles only targets the project was installed for, so every
    file it writes is recorded (I1). A lock written with declared targets
    records them in ``manifest_targets``; otherwise a target counts as
    recorded when the lock records a file it governs. A target apm.yml
    declares since then is left to ``apm install``.
    """
    from apm_cli.install.target_governance import install_governance, is_governed_by_install

    if lockfile is None:
        return list(targets), []
    if lockfile.manifest_targets:
        recorded_names = set(lockfile.manifest_targets)
        kept = [target for target in targets if target.name in recorded_names]
    else:
        paths = [
            *(path for dep in lockfile.dependencies.values() for path in dep.deployed_files),
            *lockfile.local_deployed_files,
        ]
        if not paths:
            return list(targets), []
        kept = [
            target
            for target in targets
            if any(is_governed_by_install(path, *install_governance([target])) for path in paths)
        ]
    kept_names = {target.name for target in kept}
    return kept, [target.name for target in targets if target.name not in kept_names]


def reconciled_targets(targets: list[Any], lockfile: Any, logger: Any) -> list[Any]:
    """Return the lock-recorded *targets*, naming once the ones left to ``apm install``."""
    from apm_cli.core.remediation import UNINSTALL_NEW_TARGET_UNDEPLOYED

    kept, new = lock_recorded_targets(targets, lockfile)
    if new:
        shown = ", ".join(new)
        logger.progress(
            f"Not deployed to {shown}: this project was not installed for it yet. "
            + UNINSTALL_NEW_TARGET_UNDEPLOYED.bind(targets=shown).sentence()
        )
    return kept
