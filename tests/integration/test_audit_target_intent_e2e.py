"""Installed-CLI proofs that audit replays current configured target intent."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_SKILL_NAME = "audit-target-intent"
_SKILL_PATH = f".grok/skills/{_SKILL_NAME}/SKILL.md"
_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")


@dataclass(frozen=True)
class _Scenario:
    isolated: IsolatedApmEnvironment
    project: Path
    skill: Path
    runner: ApmLifecycleRunner

    def run(self, *args: str, expected_exit: int = 0) -> CommandResult:
        """Run the real CLI with the scenario's isolated environment."""
        result = self.runner.run(
            args,
            scenario_id="audit-target-intent",
            cwd=self.project,
            env=self.isolated.subprocess_env(),
        )
        assert result.returncode == expected_exit, (
            f"{result.command}\n{result.stdout}\n{result.stderr}"
        )
        return result

    def audit(self, *, expected_exit: int = 0) -> dict:
        """Audit without changing the project or the user's configuration."""
        before_project = ArtifactSnapshot.capture(self.project)
        before_config = ArtifactSnapshot.capture(self.isolated.config_root)
        result = self.run(*_AUDIT_ARGS, expected_exit=expected_exit)
        assert_unchanged(before_project, ArtifactSnapshot.capture(self.project))
        assert_unchanged(before_config, ArtifactSnapshot.capture(self.isolated.config_root))
        return json.loads(result.stdout)


def _install_grok(
    tmp_path: Path,
    apm_binary_path: Path,
) -> _Scenario:
    """Install a local skill without any manifest target declaration."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    sources = LocalPackageFactory(isolated.package_root)
    package = sources.create("audit-source")
    skill = sources.add_skill(
        package,
        _SKILL_NAME,
        f"---\nname: {_SKILL_NAME}\ndescription: Audit target fixture\n---\n"
        "# Source-derived clean skill\n",
    )
    project = LocalPackageFactory(isolated.work_root).create(
        "audit-consumer",
        dependencies=({"path": str(package.root)},),
    )
    sentinel = project.root / ".github/workflows/unrelated.yml"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("name: unrelated\n", encoding="utf-8")
    scenario = _Scenario(
        isolated,
        project.root,
        skill,
        ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=180),
    )
    scenario.run("experimental", "enable", "grok-cloud")
    scenario.run("config", "set", "target", "grok-cloud")
    scenario.run("install", "--target", "grok-cloud", "--no-policy", "--parallel-downloads", "0")

    deployed = project.root / _SKILL_PATH
    assert deployed.read_bytes() == skill.read_bytes()
    assert not (project.root / ".agents/skills" / _SKILL_NAME).exists()
    lock = LockFile.read(project.root / "apm.lock.yaml")
    assert lock is not None
    assert {record.locator.target for record in lock.deployment_ledger.records.values()} == {
        "grok-cloud"
    }
    records = tuple(
        record
        for record in lock.deployment_ledger.records.values()
        if record.locator.value == _SKILL_PATH
    )
    assert len(records) == 1
    record = records[0]
    assert record.locator.target == "grok-cloud"
    assert record.locator.value == _SKILL_PATH
    assert record.owners == (record.active_owner,)
    assert record.active_owner in lock.dependencies
    assert record.content_hash == f"sha256:{hashlib.sha256(skill.read_bytes()).hexdigest()}"
    return scenario


@pytest.mark.parametrize("cold_cache", [False, True], ids=["warm", "cold"])
def test_clean_configured_grok_cloud_audit(
    tmp_path: Path,
    apm_binary_path: Path,
    cold_cache: bool,
) -> None:
    """An unrelated CI directory cannot retarget a clean cloud installation."""
    scenario = _install_grok(tmp_path, apm_binary_path)
    if cold_cache:
        shutil.rmtree(scenario.project / "apm_modules")
    payload = scenario.audit()
    assert payload["passed"] is True
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["drift"]["passed"] is True
    assert checks["deployment-ledger-owners"]["passed"] is True
    assert checks["content-integrity"]["passed"] is True


@pytest.mark.parametrize(
    ("mutation", "failed_check", "drift_kind"),
    [
        ("content", "content-integrity", "modified"),
        ("forged-hash", "drift", "modified"),
        ("missing-claim", "drift", "unrecorded"),
        ("invalid-owner", "deployment-ledger-owners", None),
        ("missing-file", "deployed-files-present", "unintegrated"),
        ("removed-source", "drift", "orphaned"),
        ("disabled", "target-resolution", None),
    ],
)
def test_configured_target_keeps_real_audit_failures(
    tmp_path: Path,
    apm_binary_path: Path,
    mutation: str,
    failed_check: str,
    drift_kind: str | None,
) -> None:
    """Configured intent must not depend on the integrity of installed claims."""
    scenario = _install_grok(tmp_path, apm_binary_path)
    deployed = scenario.project / _SKILL_PATH
    lock_path = scenario.project / "apm.lock.yaml"
    if mutation in {"content", "forged-hash"}:
        deployed.write_bytes(deployed.read_bytes() + b"\nUnexpected deployed edit.\n")
    if mutation in {"forged-hash", "missing-claim", "invalid-owner"}:
        document = load_yaml(lock_path)
        for row in list(document["deployments"]):
            if mutation == "missing-claim" and (
                row["value"] == _SKILL_PATH
                or _SKILL_PATH.startswith(row["value"].rstrip("/") + "/")
            ):
                document["deployments"].remove(row)
                continue
            if row["value"] != _SKILL_PATH:
                continue
            if mutation == "forged-hash":
                row["content_hash"] = f"sha256:{hashlib.sha256(deployed.read_bytes()).hexdigest()}"
            else:
                row["owners"] = ["departed-owner"]
                row["active_owner"] = "departed-owner"
        for dependency in document["dependencies"]:
            if mutation == "forged-hash":
                dependency["deployed_file_hashes"][_SKILL_PATH] = (
                    f"sha256:{hashlib.sha256(deployed.read_bytes()).hexdigest()}"
                )
            elif mutation == "missing-claim":
                dependency["deployed_files"] = [
                    path
                    for path in dependency["deployed_files"]
                    if path != _SKILL_PATH and not _SKILL_PATH.startswith(path.rstrip("/") + "/")
                ]
                dependency["deployed_file_hashes"].pop(_SKILL_PATH)
        dump_yaml(document, lock_path)
    elif mutation == "missing-file":
        deployed.unlink()
    elif mutation == "removed-source":
        scenario.skill.unlink()
    elif mutation == "disabled":
        scenario.run("experimental", "disable", "grok-cloud")

    payload = scenario.audit(expected_exit=1)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks[failed_check]["passed"] is False
    if drift_kind is not None:
        assert f"{drift_kind}: {_SKILL_PATH}" in checks["drift"]["details"]


def test_target_contraction_keeps_old_claims_in_comparison(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Old targets widen comparison coverage, not desired replay output."""
    scenario = _install_grok(tmp_path, apm_binary_path)
    scenario.run(
        "install", "--target", "grok-cloud,claude", "--no-policy", "--parallel-downloads", "0"
    )
    payload = scenario.audit(expected_exit=1)
    drift = next(check for check in payload["checks"] if check["name"] == "drift")
    assert f"orphaned: .claude/skills/{_SKILL_NAME}/SKILL.md" in drift["details"]
    assert not any(detail.startswith("unintegrated:") for detail in drift["details"])


def test_manifest_beats_stale_saved_default(tmp_path: Path, apm_binary_path: Path) -> None:
    """The validated manifest overrides a conflicting user default."""
    scenario = _install_grok(tmp_path, apm_binary_path)
    manifest_path = scenario.project / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["targets"] = ["grok-build"]
    dump_yaml(manifest, manifest_path)
    scenario.run("config", "set", "target", "claude")
    scenario.run("install", "--no-policy", "--parallel-downloads", "0")
    assert scenario.audit()["passed"] is True


def test_changed_default_audits_current_intent(tmp_path: Path, apm_binary_path: Path) -> None:
    """Changing the default cannot silently bless the prior installed target."""
    scenario = _install_grok(tmp_path, apm_binary_path)
    scenario.run("config", "set", "target", "claude")
    payload = scenario.audit(expected_exit=1)
    drift = next(check for check in payload["checks"] if check["name"] == "drift")
    assert f"unintegrated: .claude/skills/{_SKILL_NAME}/SKILL.md" in drift["details"]
    assert f"orphaned: {_SKILL_PATH}" in drift["details"]


def test_configured_grok_cloud_user_scope_audit(tmp_path: Path, apm_binary_path: Path) -> None:
    """Global replay uses the user deployment root, without writing to HOME."""
    scenario = _install_grok(tmp_path, apm_binary_path)
    scenario.run(
        "install",
        "--global",
        str(scenario.skill.parents[2]),
        "--target",
        "grok-cloud",
        "--no-policy",
        "--parallel-downloads",
        "0",
    )
    home_skill = scenario.isolated.home / _SKILL_PATH
    assert home_skill.read_bytes() == scenario.skill.read_bytes()
    user = replace(scenario, project=scenario.isolated.config_root)
    before_home = ArtifactSnapshot.capture(scenario.isolated.home)
    assert user.audit()["passed"] is True
    assert_unchanged(before_home, ArtifactSnapshot.capture(scenario.isolated.home))


@pytest.mark.parametrize("failed_replay", [False, True], ids=["clean", "unsupported-native"])
@pytest.mark.parametrize("cold_cache", [False, True], ids=["warm", "cold"])
def test_audit_startup_leaves_fresh_home_unchanged_outside_test_mode(
    tmp_path: Path, apm_binary_path: Path, failed_replay: bool, cold_cache: bool
) -> None:
    """The real entry point stays read-only without test-only update suppression."""
    scenario = _install_grok(tmp_path, apm_binary_path)
    manifest_path = scenario.project / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["targets"] = ["grok-build"]
    dump_yaml(manifest, manifest_path)
    (scenario.isolated.config_root / "config.json").unlink()
    if cold_cache:
        shutil.rmtree(scenario.project / "apm_modules")
    if failed_replay:
        lock_path = scenario.project / "apm.lock.yaml"
        document = load_yaml(lock_path)
        document["dependencies"][0]["deployed_files"].append(
            "copilot-app-db://workflows/audit-startup-fixture"
        )
        dump_yaml(document, lock_path)

    environment = scenario.isolated.subprocess_env()
    environment.pop("PYTEST_CURRENT_TEST", None)
    environment.pop("APM_E2E_TESTS", None)
    before_project = ArtifactSnapshot.capture(scenario.project)
    before_home = ArtifactSnapshot.capture(scenario.isolated.home)
    result = scenario.runner.run(
        _AUDIT_ARGS,
        scenario_id="audit-without-test-mode",
        cwd=scenario.project,
        env=environment,
    )
    assert result.returncode == int(failed_replay), result.stdout + result.stderr
    checks = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
    assert checks["content-integrity"]["passed"] is True
    if failed_replay:
        assert checks["drift"]["passed"] is False
        assert "no isolated filesystem scratch replay backend" in checks["drift"]["message"]
    else:
        assert all(check["passed"] for check in checks.values())
    assert_unchanged(before_project, ArtifactSnapshot.capture(scenario.project))
    assert_unchanged(before_home, ArtifactSnapshot.capture(scenario.isolated.home))
