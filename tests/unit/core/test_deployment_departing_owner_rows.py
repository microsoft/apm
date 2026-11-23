"""Uninstall reconciliation of rows a departing owner proved (#4703)."""

from dataclasses import replace
from pathlib import Path

import pytest

from apm_cli.core.deployment_state import (
    DeploymentIntent,
    DeploymentLedger,
    DeploymentLocator,
    DeploymentReconciler,
    DeploymentRecord,
    LocatorKind,
    MaterializationResult,
    MaterializationStatus,
    NativePayloadValidation,
)
from apm_cli.core.target_catalog import TARGET_CAPABILITIES
from apm_cli.integration.targets import TargetProfile
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-deployment-provenance-state")]


def _target(name: str, root: str, managed: Path | None = None) -> TargetProfile:
    capability = TARGET_CAPABILITIES.get(name)
    if capability is None:
        capability = replace(
            TARGET_CAPABILITIES["copilot"],
            name=name,
            aliases=(),
            runtimes=(),
        )
    return TargetProfile(
        capability=capability,
        root_dir=str(managed or root),
        primitives={},
        resolved_deploy_root=managed,
    )


def _locator(
    value: str = ".github/agents/demo.agent.md",
    *,
    target: str = "copilot",
    runtime: str | None = None,
) -> DeploymentLocator:
    return DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target=target,
        value=value,
        runtime=runtime,
        scope="project",
    )


def _materialization(
    owner: str,
    locator: DeploymentLocator,
    *,
    status: MaterializationStatus = MaterializationStatus.WRITTEN,
    valid: bool = True,
    content_hash: str | None = "sha256:new",
) -> MaterializationResult:
    return MaterializationResult(
        locator=locator,
        owners=frozenset({owner}),
        status=status,
        content_hash=content_hash,
        validation=NativePayloadValidation(valid=valid, contract="native-v1"),
    )


def _record(locator: DeploymentLocator, owners=("old",), active="old") -> DeploymentRecord:
    return DeploymentRecord(
        locator=locator,
        owners=owners,
        active_owner=active,
        content_hash="sha256:old",
    )


def _reconciler(tmp_path: Path) -> DeploymentReconciler:
    profiles = {
        "copilot": _target("copilot", ".github"),
        "claude": _target("claude", ".claude"),
        "cursor": _target("cursor", ".cursor"),
    }
    return DeploymentReconciler(
        tmp_path,
        profiles,
        diagnostics=DiagnosticCollector(),
    )


def _intent(
    *,
    active=frozenset({"copilot"}),
    declared=None,
    owners=None,
    authoritative=True,
) -> DeploymentIntent:
    return DeploymentIntent(
        active_targets=active,
        declared_targets=declared,
        desired_owners=owners,
        authoritative_targets=authoritative,
    )


def test_departing_owner_proof_keeps_the_row_with_remaining_owners(tmp_path: Path) -> None:
    """A departing owner's proof neither fails nor erases a bundle's claim (#4703)."""
    locator = _locator(".github/instructions/f-doc.instructions.md")
    prior = DeploymentLedger(
        records={
            locator.key: _record(
                locator, owners=("removed", ".", "local-bundle"), active="local-bundle"
            )
        }
    )
    reconciler = _reconciler(tmp_path)

    reconciled = reconciler.reconcile(
        prior,
        [_materialization("legacy", locator, status=MaterializationStatus.UNCHANGED)],
        _intent(owners=frozenset({".", "local-bundle"})),
    )

    record = reconciled.ledger.records[locator.key]
    assert record.owners == (".", "local-bundle")
    assert record.active_owner == "local-bundle"
    assert reconciled.failed == ()
    assert reconciler.diagnostics.error_count == 0


def test_departing_owner_proof_drops_a_row_no_owner_still_holds(tmp_path: Path) -> None:
    """With no remaining owner, a departing proof removes the row without failing (#4703)."""
    locator = _locator(".github/instructions/f-doc.instructions.md")
    prior = DeploymentLedger(
        records={locator.key: _record(locator, owners=("removed",), active="removed")}
    )
    reconciler = _reconciler(tmp_path)

    reconciled = reconciler.reconcile(
        prior,
        [_materialization("removed", locator, status=MaterializationStatus.UNCHANGED)],
        _intent(owners=frozenset({"."})),
    )

    assert locator.key not in reconciled.ledger.records
    assert reconciled.failed == ()
    assert reconciler.diagnostics.error_count == 0


def test_departing_proof_superseding_a_generic_row_is_consumed_once(tmp_path: Path) -> None:
    """A concrete departing proof that a generic row took is not re-processed (#4703)."""
    concrete = _locator(".github/instructions/f-doc.instructions.md")
    generic = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="legacy-generic",
        value=concrete.value,
        runtime=None,
        scope="project",
    )
    prior = DeploymentLedger(
        records={generic.key: _record(generic, owners=("removed",), active="removed")}
    )
    reconciler = _reconciler(tmp_path)

    reconciled = reconciler.reconcile(
        prior,
        [_materialization("removed", concrete, status=MaterializationStatus.UNCHANGED)],
        _intent(owners=frozenset({"."})),
    )

    assert generic.key not in reconciled.ledger.records
    assert concrete.key not in reconciled.ledger.records
    assert reconciled.failed == ()
    assert reconciler.diagnostics.error_count == 0


def test_generic_row_with_several_concrete_proofs_keeps_every_target_record(
    tmp_path: Path,
) -> None:
    """A live generic row superseded on two targets keeps both concrete rows (#4703)."""
    value = ".github/instructions/shared.instructions.md"
    copilot = _locator(value)
    claude = _locator(value, target="claude")
    generic = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="legacy-generic",
        value=value,
        runtime=None,
        scope="project",
    )
    prior = DeploymentLedger(
        records={generic.key: _record(generic, owners=("kept",), active="kept")}
    )
    reconciler = _reconciler(tmp_path)

    reconciled = reconciler.reconcile(
        prior,
        [_materialization("kept", copilot), _materialization("kept", claude)],
        _intent(active=frozenset({"copilot", "claude"}), owners=frozenset({"kept"})),
    )

    assert copilot.key in reconciled.ledger.records
    assert claude.key in reconciled.ledger.records
    assert reconciled.failed == ()
    assert reconciler.diagnostics.error_count == 0


def test_ownerless_proof_still_reports_missing_ownership(tmp_path: Path) -> None:
    """An ownerless materialization is not a departing owner's proof (#4703)."""
    locator = _locator(".github/instructions/f-doc.instructions.md")
    prior = DeploymentLedger(
        records={locator.key: _record(locator, owners=("kept",), active="kept")}
    )
    ownerless = MaterializationResult(
        locator=locator,
        owners=frozenset(),
        status=MaterializationStatus.WRITTEN,
        content_hash="sha256:new",
        validation=NativePayloadValidation(valid=True, contract="native-v1"),
    )
    reconciler = _reconciler(tmp_path)

    reconciled = reconciler.reconcile(prior, [ownerless], _intent(owners=frozenset({"kept"})))

    assert reconciled.failed == (locator,)
    assert reconciler.diagnostics.error_count == 1
