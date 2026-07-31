"""Required lifecycle proofs for cold-cache ``apm audit --ci``.

These rows use the repository's real-CLI lifecycle harness:

* a tracked committed deployment goes red from a fresh setup-only CI state,
  and ``apm audit --ci`` proves a no-write failure path;
* a normal repair install updates deployed bytes plus lock provenance, then two
  repeated cold audits prove a clean, idempotent pass path;
* a gitignored deployment remains a green positive control only when drift
  actually ran (never via a skip).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from tests.integration.test_required_lifecycle_state_machine import _assert_same_state
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_COPILOT_TARGETS = ("copilot",)
_SOURCE_INSTRUCTION = PurePosixPath(".apm/instructions/foo.instructions.md")
_AUDIT_BASE_ARGS = ("audit", "--ci", "--no-policy", "--no-fail-fast")


@dataclass(frozen=True)
class _ColdCacheScenario:
    isolated: IsolatedApmEnvironment
    project_root: Path
    environment: dict[str, str]
    runner: ApmLifecycleRunner


def _run_git(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command inside the lifecycle workspace."""
    return subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _git_status(cwd: Path, env: dict[str, str]) -> str:
    """Return stable porcelain output for commit-state assertions."""
    return _run_git(cwd, env, "status", "--porcelain").stdout


def _capture_state(project_root: Path) -> LifecycleStateSnapshot:
    """Capture exact and semantic lifecycle state for the copilot target."""
    return LifecycleStateSnapshot.capture(
        project_root,
        targets=_COPILOT_TARGETS,
        config_paths=(_SOURCE_INSTRUCTION,),
    )


def _audit(
    scenario: _ColdCacheScenario,
    *,
    expected_returncode: int,
    fmt: str,
    scenario_id: str,
) -> tuple[CommandResult, dict[str, object]]:
    """Run one real ``apm audit --ci`` invocation and parse its payload."""
    audit_args = (*_AUDIT_BASE_ARGS, "--format", fmt)
    result = scenario.runner.run_sequence(
        (audit_args,),
        expected_returncodes=(expected_returncode,),
        scenario_id=scenario_id,
        cwd=scenario.project_root,
        env=scenario.environment,
    )[0]
    return result, json.loads(result.stdout)


def _check(payload: dict[str, object], name: str) -> dict[str, object]:
    """Return one named CI check from the public audit payload."""
    return next(check for check in payload["checks"] if check["name"] == name)


def _evict_live_modules_and_cache(scenario: _ColdCacheScenario) -> None:
    """Mimic setup-only CI by deleting live modules plus the isolated APM cache."""
    modules_root = scenario.project_root / "apm_modules"
    if modules_root.exists():
        shutil.rmtree(modules_root)
    if scenario.isolated.cache_root.exists():
        shutil.rmtree(scenario.isolated.cache_root)
    scenario.isolated.cache_root.mkdir(parents=True, exist_ok=True)


def _seed_consumer(
    tmp_path: Path,
    apm_binary_path: Path,
    *,
    track_deployments: bool,
) -> _ColdCacheScenario:
    """Create a hermetic consumer plus one local-git-backed dependency."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / ("tracked" if track_deployments else "gitignored"),
        base_env=dict(os.environ),
    )
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    packages = LocalPackageFactory(isolated.package_root)

    dependency = packages.create("remote-dependency")
    packages.add_instruction(
        dependency,
        "remote",
        "---\napplyTo: '**'\n---\n# Remote\nDependency instruction.\n",
    )
    dependency_repo = repositories.create("remote-dependency", source_tree=dependency.root)
    dependency_commit = repositories.commit(dependency_repo, message="seed dependency")
    remote_url = "https://github.com/test/remote-dependency.git"
    git_sources = (
        "git@github.com:test/remote-dependency.git",
        remote_url,
    )
    user_git_environment = dict(environment)
    user_git_environment.pop("GIT_CONFIG_GLOBAL", None)
    for git_environment in (environment, user_git_environment):
        for source in git_sources:
            subprocess.run(
                (
                    "git",
                    "config",
                    "--global",
                    "--add",
                    f"url.{dependency_repo.file_url}.insteadOf",
                    source,
                ),
                env=git_environment,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )

    consumer = packages.create(
        "consumer",
        dependencies=(
            {
                "git": remote_url,
                "ref": dependency_commit.sha,
            },
        ),
        targets=_COPILOT_TARGETS,
    )
    packages.add_instruction(
        consumer,
        "foo",
        "---\napplyTo: '**'\n---\n# Foo\nInitial content.\n",
    )

    _run_git(consumer.root, environment, "init", "--initial-branch=main")
    _run_git(consumer.root, environment, "config", "user.email", "test@example.com")
    _run_git(consumer.root, environment, "config", "user.name", "APM Test")
    gitignore_lines = ["apm_modules/"]
    if not track_deployments:
        gitignore_lines.append(".github/instructions/")
    (consumer.root / ".gitignore").write_text("\n".join(gitignore_lines) + "\n", encoding="utf-8")

    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )
    runner.run_sequence(
        (("install", "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="cold-cache-seed-install",
        cwd=consumer.root,
        env=environment,
    )
    _run_git(consumer.root, environment, "add", "--all")
    _run_git(consumer.root, environment, "commit", "-m", "seed consumer")

    return _ColdCacheScenario(
        isolated=isolated,
        project_root=consumer.root,
        environment=environment,
        runner=runner,
    )


def test_cold_cache_audit_fails_for_tracked_stale_deployments_and_reconciles_idempotently(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Tracked committed output fails cold audit, then normal install repairs it."""
    scenario = _seed_consumer(
        tmp_path,
        apm_binary_path,
        track_deployments=True,
    )
    baseline = _capture_state(scenario.project_root)
    assert _git_status(scenario.project_root, scenario.environment) == ""

    source_instruction = scenario.project_root / _SOURCE_INSTRUCTION
    deployed_instruction = (
        scenario.project_root / ".github" / "instructions" / "foo.instructions.md"
    )
    assert deployed_instruction.is_file()

    source_instruction.write_text(
        "---\napplyTo: '**'\n---\n# Foo\nUpdated content.\n",
        encoding="utf-8",
    )
    assert source_instruction.read_bytes() != baseline.file(_SOURCE_INSTRUCTION.as_posix()).content

    _evict_live_modules_and_cache(scenario)
    stale_before_audit = _capture_state(scenario.project_root)
    status_before_audit = _git_status(scenario.project_root, scenario.environment)

    _, json_payload = _audit(
        scenario,
        expected_returncode=1,
        fmt="json",
        scenario_id="tracked-cold-cache-audit-json",
    )
    after_json_audit = _capture_state(scenario.project_root)
    _assert_same_state(stale_before_audit, after_json_audit)
    assert _git_status(scenario.project_root, scenario.environment) == status_before_audit
    assert not (scenario.project_root / "apm_modules").exists()

    failed_checks = {check["name"] for check in json_payload["checks"] if not check["passed"]}
    assert failed_checks == {"drift"}
    drift = _check(json_payload, "drift")
    assert json_payload["passed"] is False
    assert json_payload["summary"]["failed"] == 1
    assert drift["passed"] is False
    assert "skipped" not in drift["message"].lower()
    assert "drift detected" in drift["message"]
    assert json_payload["drift"]["drift"][0]["kind"] == "modified"
    assert json_payload["drift"]["drift"][0]["path"] == ".github/instructions/foo.instructions.md"

    _, sarif_payload = _audit(
        scenario,
        expected_returncode=1,
        fmt="sarif",
        scenario_id="tracked-cold-cache-audit-sarif",
    )
    after_sarif_audit = _capture_state(scenario.project_root)
    _assert_same_state(stale_before_audit, after_sarif_audit)
    assert _git_status(scenario.project_root, scenario.environment) == status_before_audit
    rule_ids = {result.get("ruleId", "") for result in sarif_payload["runs"][0]["results"]}
    assert "apm/drift/modified" in rule_ids

    stale_manifest = stale_before_audit.manifest_bytes
    stale_lockfile = stale_before_audit.lockfile_bytes
    scenario.runner.run_sequence(
        (("install", "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="tracked-cold-cache-repair-install",
        cwd=scenario.project_root,
        env=scenario.environment,
    )
    repaired_state = _capture_state(scenario.project_root)
    repair_status = _git_status(scenario.project_root, scenario.environment)

    assert repaired_state.manifest_bytes == stale_manifest
    assert repaired_state.lockfile_bytes != stale_lockfile
    assert deployed_instruction.read_text(encoding="utf-8").endswith("Updated content.\n")

    _evict_live_modules_and_cache(scenario)
    _, first_clean_payload = _audit(
        scenario,
        expected_returncode=0,
        fmt="json",
        scenario_id="tracked-cold-cache-audit-clean-first",
    )
    after_first_clean = _capture_state(scenario.project_root)
    _assert_same_state(repaired_state, after_first_clean)
    assert _git_status(scenario.project_root, scenario.environment) == repair_status
    assert not (scenario.project_root / "apm_modules").exists()
    first_clean_drift = _check(first_clean_payload, "drift")
    assert first_clean_payload["passed"] is True
    assert first_clean_drift["passed"] is True
    assert "skipped" not in first_clean_drift["message"].lower()
    assert first_clean_drift["message"] == "no drift detected against lockfile"
    assert first_clean_payload["drift"]["drift"] == []

    _, second_clean_payload = _audit(
        scenario,
        expected_returncode=0,
        fmt="json",
        scenario_id="tracked-cold-cache-audit-clean-second",
    )
    after_second_clean = _capture_state(scenario.project_root)
    _assert_same_state(after_first_clean, after_second_clean)
    assert _git_status(scenario.project_root, scenario.environment) == repair_status
    assert not (scenario.project_root / "apm_modules").exists()
    second_clean_drift = _check(second_clean_payload, "drift")
    assert second_clean_payload["passed"] is True
    assert second_clean_drift["message"] == "no drift detected against lockfile"
    assert second_clean_payload["drift"]["drift"] == []


def test_cold_cache_audit_stays_green_for_gitignored_clean_deployments_when_comparison_runs(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Gitignored clean deployments stay green only when drift really ran."""
    scenario = _seed_consumer(
        tmp_path,
        apm_binary_path,
        track_deployments=False,
    )
    _evict_live_modules_and_cache(scenario)
    before_audit = _capture_state(scenario.project_root)
    status_before_audit = _git_status(scenario.project_root, scenario.environment)

    _, payload = _audit(
        scenario,
        expected_returncode=0,
        fmt="json",
        scenario_id="gitignored-cold-cache-audit-json",
    )
    after_audit = _capture_state(scenario.project_root)

    _assert_same_state(before_audit, after_audit)
    assert _git_status(scenario.project_root, scenario.environment) == status_before_audit
    assert not (scenario.project_root / "apm_modules").exists()
    drift = _check(payload, "drift")
    assert payload["passed"] is True
    assert drift["passed"] is True
    assert "skipped" not in drift["message"].lower()
    assert drift["message"] == "no drift detected against lockfile"
    assert payload["drift"]["drift"] == []
