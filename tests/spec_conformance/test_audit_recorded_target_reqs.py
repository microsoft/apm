"""Recorded target replay preserves the existing governed drift contract."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import resolve_audit_targets
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_audit_primitive_contract import project as audit_project
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged

pytestmark = pytest.mark.component
__all__ = ["audit_project"]


@pytest.mark.req("req-pl-014")
@pytest.mark.parametrize("fail_on_drift", (False, True))
def test_recorded_grok_target_preserves_governed_drift(
    audit_project: Path, monkeypatch: pytest.MonkeyPatch, fail_on_drift: bool
) -> None:
    """Unrelated discovery must not create drift or suppress actual drift."""
    (audit_project / "apm.yml").write_text(
        "name: audit-contract\nversion: 1.0.0\n", encoding="ascii"
    )
    source = audit_project / ".apm/skills/example/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nname: example\ndescription: Example.\n---\nOriginal.\n", encoding="ascii"
    )
    workflow = audit_project / ".github/workflows/unrelated.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: unrelated\n", encoding="ascii")
    runner = CliRunner()
    for args in (
        ["experimental", "enable", "grok-cloud"],
        ["install", "--target", "grok-cloud", "--no-policy", "--parallel-downloads", "0"],
    ):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
    deployed = audit_project / ".grok/skills/example/SKILL.md"
    assert deployed.read_bytes() == source.read_bytes()
    monkeypatch.setattr("apm_cli.commands.audit._resolve_fail_on_drift", lambda _: fail_on_drift)
    for damaged in (False, True):
        if damaged:
            deployed.write_text("Modified managed content.\n", encoding="ascii")
        before = ArtifactSnapshot.capture(audit_project)
        result = runner.invoke(cli, ["audit"])
        assert result.exit_code == int(damaged and fail_on_drift), result.output
        if damaged:
            assert "Drift detected: 1 file(s)" in result.output
            assert ".grok/skills/example/SKILL.md" in result.output
        else:
            assert "No drift detected" in result.output
        assert not (audit_project / ".agents/skills/example/SKILL.md").exists()
        assert_unchanged(before, ArtifactSnapshot.capture(audit_project))


@pytest.mark.req("req-pl-014")
def test_legacy_mixed_targets_preserve_discovery_and_drift(audit_project: Path) -> None:
    """Path-derived legacy identities must not narrow replay or untracked scanning."""
    (audit_project / "apm.yml").write_text(
        "name: audit-contract\nversion: 1.0.0\n", encoding="ascii"
    )
    source = audit_project / ".apm/skills/example/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\nname: example\ndescription: Example.\n---\nOriginal.\n", encoding="ascii"
    )
    runner = CliRunner()
    installed = runner.invoke(
        cli,
        ["install", "--target", "claude,copilot", "--no-policy", "--parallel-downloads", "0"],
    )
    assert installed.exit_code == 0, installed.output
    for root in (".claude", ".agents"):
        assert (
            audit_project / root / "skills/example/SKILL.md"
        ).read_bytes() == source.read_bytes()
    lock_path = audit_project / "apm.lock.yaml"
    legacy_data = load_yaml(lock_path)
    legacy_data.pop("deployments")
    dump_yaml(legacy_data, lock_path)
    manual_path = ".github/agents/manual.agent.md"
    manual = audit_project / manual_path
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text("# Manual agent\nReview changes.\n", encoding="ascii")
    lock = LockFile.read(lock_path)
    assert lock is not None and not lock._deployments_present
    assert {row.locator.target for row in lock.deployment_ledger.records.values()} == {
        "claude",
        "legacy",
    }
    assert {target.name for target in resolve_audit_targets(audit_project, lockfile=lock)} == {
        "claude",
        "copilot",
    }
    for damaged in (False, True):
        if damaged:
            (audit_project / ".agents/skills/example/SKILL.md").unlink()
        before = ArtifactSnapshot.capture(audit_project)
        result = runner.invoke(
            cli, ["audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"]
        )
        assert result.exit_code == int(damaged), result.output
        payload = json.loads(result.stdout)
        integrity = next(c for c in payload["checks"] if c["name"] == "content-integrity")
        assert any(
            row["file"] == manual_path and row["target"] == "copilot" and not row["tracked"]
            for row in integrity["primitive_coverage"]
        )
        assert bool(payload["drift"]["drift"]) is damaged
        if damaged:
            assert any(
                row["path"] == ".agents/skills/example/SKILL.md"
                for row in payload["drift"]["drift"]
            )
        assert_unchanged(before, ArtifactSnapshot.capture(audit_project))
