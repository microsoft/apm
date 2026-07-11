"""In-memory uninstall reconciliation for one atomic lockfile write."""

from __future__ import annotations

from pathlib import Path


def reconcile_uninstall_deployment_state(
    lockfile,
    *,
    deploy_root: Path,
    all_deployed_files: set[str],
    surviving_deployed_files: dict[str, set[str]],
) -> bool:
    """Reconcile survivor ownership and hashes without persisting."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.core.deployment_state import (
        DeploymentIntent,
        DeploymentReconciler,
        MaterializationResult,
        MaterializationStatus,
        NativePayloadValidation,
    )
    from apm_cli.install.phases.lockfile import compute_deployed_hashes
    from apm_cli.integration.targets import KNOWN_TARGETS
    from apm_cli.utils.diagnostics import DiagnosticCollector

    previous_ledger = DeploymentLedgerCodec.from_lockfile(lockfile)
    records_by_value = {}
    for record in previous_ledger.records.values():
        records_by_value.setdefault(record.locator.value, []).append(record)
    materializations: list[MaterializationResult] = []
    for dep_key, deployed_files in surviving_deployed_files.items():
        transferred = sorted(all_deployed_files.intersection(deployed_files))
        hashes = compute_deployed_hashes(transferred, deploy_root)
        for path in transferred:
            for prior in records_by_value.get(path, ()):
                materializations.append(
                    MaterializationResult(
                        locator=prior.locator,
                        owners=frozenset({dep_key}),
                        status=MaterializationStatus.UNCHANGED,
                        content_hash=hashes.get(path),
                        validation=NativePayloadValidation(
                            valid=True,
                            contract="uninstall-survivor",
                        ),
                    )
                )

    reconciled = DeploymentReconciler(
        deploy_root,
        KNOWN_TARGETS,
        diagnostics=DiagnosticCollector(),
    ).reconcile(
        previous_ledger,
        materializations,
        DeploymentIntent(
            active_targets=frozenset(),
            declared_targets=None,
            desired_owners=frozenset({".", *lockfile.dependencies}),
            authoritative_targets=False,
        ),
    )
    if reconciled.changed:
        DeploymentLedgerCodec.apply_to_lockfile(reconciled.ledger, lockfile)
    return reconciled.changed


def lockfile_has_persisted_state(lockfile) -> bool:
    """Return whether uninstall should keep rather than remove the lockfile."""
    return bool(
        lockfile.dependencies
        or lockfile.mcp_servers
        or lockfile.lsp_servers
        or lockfile.local_deployed_files
        or lockfile.deployment_ledger.records
    )
