"""Audit intent uses current declarations without reinterpreting install defaults."""

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.core.target_detection import resolve_audit_target_decision
from apm_cli.deps.lockfile import LockFile

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("manifest", "recorded", "expected", "source"),
    [
        (["copilot"], ("grok-cloud",), ("copilot",), "apm.yml"),
        (None, ("grok-cloud",), ("grok-cloud",), "deployment ledger"),
        (None, ("claude", "copilot"), ("claude", "copilot"), "deployment ledger"),
        (None, (), None, "auto-detect"),
    ],
)
def test_audit_target_intent(
    manifest: list[str] | None,
    recorded: tuple[str, ...],
    expected: tuple[str, ...] | None,
    source: str,
) -> None:
    decision = resolve_audit_target_decision(manifest_target=manifest, recorded_targets=recorded)
    assert decision.canonical_targets == expected
    assert decision.source == source


@pytest.mark.parametrize(
    ("records", "scope", "expected"),
    [
        (None, "project", ()),
        ([], "project", ()),
        ([("grok-cloud", "user")], "project", ()),
        ([("unknown", "project")], "project", ()),
        ([("grok-cloud", "user")], "user", ("grok-cloud",)),
        (
            [("grok-cloud", "project"), ("copilot", "project"), ("grok-cloud", "project")],
            "project",
            ("copilot", "grok-cloud"),
        ),
    ],
)
def test_recorded_target_names(
    records: list[tuple[str, str]] | None, scope: str, expected: tuple[str, ...]
) -> None:
    lock = LockFile(_deployments_present=True)
    if records is not None:
        ledger_records = {}
        for index, (target, record_scope) in enumerate(records):
            locator = DeploymentLocator(
                kind=LocatorKind.PROJECT_RELATIVE,
                target=target,
                value=f".grok/skills/example-{index}/SKILL.md",
                runtime=None,
                scope=record_scope,
            )
            ledger_records[locator.key] = DeploymentRecord(
                locator=locator, owners=("example",), active_owner="example", content_hash=None
            )
        lock.deployment_ledger = DeploymentLedger(records=ledger_records)
    assert (
        DeploymentLedgerCodec.recorded_target_names(
            lock, scope=scope, supported_targets=("copilot", "grok-cloud")
        )
        == expected
    )


def test_synthesized_legacy_targets_are_not_recorded_intent() -> None:
    lock = LockFile.from_yaml(
        "lockfile_version: '1'\n"
        "local_deployed_files:\n"
        "  - .claude/skills/example/SKILL.md\n"
        "  - .agents/skills/example/SKILL.md\n"
    )
    assert not lock._deployments_present
    assert {row.locator.target for row in lock.deployment_ledger.records.values()} == {
        "claude",
        "legacy",
    }
    assert (
        DeploymentLedgerCodec.recorded_target_names(
            lock, scope="project", supported_targets=("claude", "copilot")
        )
        == ()
    )
