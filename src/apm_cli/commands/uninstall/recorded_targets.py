"""Targets uninstall reconciles after apm.yml changes its targets (#4636)."""

from __future__ import annotations

from typing import Any


def lock_recorded_targets(targets: list[Any], lockfile: Any) -> tuple[list[Any], list[str]]:
    """Split *targets* into the ones apm.lock.yaml holds deployments for and the new ones.

    Uninstall reconciles only targets the project was installed for, so every
    file it writes is recorded (I1). The evidence is the target each recorded
    deployment row names, never ``manifest_targets``: any changed lock write
    refreshes that from the current apm.yml. A legacy row on a root several
    targets share (``.agents/``) counts only for the one candidate governing
    it. A target apm.yml declares since then is left to ``apm install``.
    """
    from apm_cli.core.deployment_ledger import UNATTRIBUTED_TARGET, DeploymentLedgerCodec
    from apm_cli.install.target_governance import install_governance, is_governed_by_install

    if lockfile is None:
        return list(targets), []
    files = [
        record.locator
        for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values()
        if record.locator.runtime is None
    ]
    if not files:
        return list(targets), []
    named = {locator.target for locator in files}
    unattributed = [locator.value for locator in files if locator.target == UNATTRIBUTED_TARGET]
    governance = {target.name: install_governance([target]) for target in targets}

    def governing(path: str) -> list[str]:
        return [name for name, rule in governance.items() if is_governed_by_install(path, *rule)]

    exclusive = {names[0] for path in unattributed if len(names := governing(path)) == 1}
    kept = [target for target in targets if target.name in named | exclusive]
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
