"""Lifecycle-level checks for the canonical deployment owner."""

from pathlib import Path

from apm_cli.core.deployment_state import (
    DeploymentIntent,
    DeploymentLedger,
    DeploymentLocator,
    DeploymentReconciler,
    LocatorKind,
    MaterializationResult,
    MaterializationStatus,
    NativePayloadValidation,
)
from apm_cli.core.target_catalog import TARGET_CAPABILITIES
from apm_cli.integration.targets import TargetProfile
from apm_cli.policy.ci_checks import _check_content_integrity
from apm_cli.utils.diagnostics import DiagnosticCollector


def test_install_update_compile_uninstall_share_one_owner(tmp_path: Path) -> None:
    target = TargetProfile(
        capability=TARGET_CAPABILITIES["copilot"],
        root_dir=".github",
        primitives={},
    )
    reconciler = DeploymentReconciler(
        tmp_path,
        {"copilot": target},
        diagnostics=DiagnosticCollector(),
    )
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value=".github/agents/demo.agent.md",
        runtime=None,
        scope="project",
    )
    intent = DeploymentIntent(
        active_targets=frozenset({"copilot"}),
        declared_targets=frozenset({"copilot"}),
        desired_owners=frozenset({"owner/package"}),
        authoritative_targets=True,
    )

    ledger = DeploymentLedger(records={})
    for status in (
        MaterializationStatus.WRITTEN,
        MaterializationStatus.UNCHANGED,
        MaterializationStatus.WRITTEN,
    ):
        result = MaterializationResult(
            locator=locator,
            owners=frozenset({"owner/package"}),
            status=status,
            content_hash="sha256:demo",
            validation=NativePayloadValidation(valid=True, contract="file"),
        )
        ledger = reconciler.reconcile(ledger, [result], intent).ledger

    uninstall = reconciler.reconcile(
        ledger,
        [
            MaterializationResult(
                locator=locator,
                owners=frozenset({"owner/package"}),
                status=MaterializationStatus.REMOVED,
                content_hash=None,
                validation=NativePayloadValidation(valid=True, contract="file"),
            )
        ],
        DeploymentIntent(
            active_targets=frozenset({"copilot"}),
            declared_targets=frozenset({"copilot"}),
            desired_owners=frozenset(),
            authoritative_targets=True,
        ),
    )

    assert uninstall.ledger.records == {}
    assert uninstall.removed == (locator,)


def test_content_integrity_fails_when_ownership_row_is_absent(
    tmp_path: Path,
) -> None:
    from apm_cli.deps.lockfile import LockFile

    path = tmp_path / ".github" / "agents" / "demo.agent.md"
    path.parent.mkdir(parents=True)
    path.write_text("demo", encoding="utf-8")
    lockfile = LockFile(
        local_deployed_files=[".github/agents/demo.agent.md"],
        local_deployed_file_hashes={".github/agents/demo.agent.md": "sha256:missing"},
    )
    lockfile._deployments_present = True

    result = _check_content_integrity(tmp_path, lockfile)

    assert result.passed is False
    assert result.details == ["missing-ownership: .github/agents/demo.agent.md"]
