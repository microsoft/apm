"""Installed-CLI lifecycle contract for Git ref freshness and cache reuse."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_SKILL_NAME = "ref-freshness"
_SKILL_PATH = Path(".agents") / "skills" / _SKILL_NAME / "SKILL.md"
_INSTALL_ARGS = (
    "install",
    "--target",
    "copilot",
    "--no-policy",
    "--parallel-downloads",
    "0",
    "--verbose",
)
_UPDATE_ARGS = (
    "update",
    "--yes",
    "--target",
    "copilot",
    "--parallel-downloads",
    "0",
    "--verbose",
)
_OUTDATED_ARGS = ("outdated", "--parallel-checks", "0", "--verbose")
_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")


@dataclass(frozen=True)
class _Scenario:
    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    packages: LocalPackageFactory
    repositories: LocalGitRepositoryFactory
    repository: LocalGitRepository
    consumer: LocalPackage
    runner: ApmLifecycleRunner
    skill_path: Path
    commit_a: GitCommit


def _skill_document(marker: str) -> str:
    return (
        "---\n"
        f"name: {_SKILL_NAME}\n"
        "description: Git ref freshness lifecycle fixture\n"
        "---\n"
        f"# {marker}\n"
    )


def _new_scenario(root: Path, apm_binary_path: Path) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env(overrides={"APM_TIERED_RESOLVER": "1"})
    packages = LocalPackageFactory(isolated.package_root)
    source = packages.create("ref-freshness-source", targets=("copilot",))
    source_skill = packages.add_skill(source, _SKILL_NAME, _skill_document("commit-a"))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create("ref-freshness-source", source_tree=source.root)
    commit_a = repositories.commit(repository, message="publish commit A")
    remote_url = "https://github.com/apm-fixture-org/ref-freshness-source"
    environment = repositories.url_rewrite_subprocess_env(repository, remote_url)
    rewrite_index = int(environment["GIT_CONFIG_COUNT"])
    environment["GIT_CONFIG_COUNT"] = str(rewrite_index + 1)
    environment[f"GIT_CONFIG_KEY_{rewrite_index}"] = f"url.{repository.file_url}/.insteadOf"
    environment[f"GIT_CONFIG_VALUE_{rewrite_index}"] = (
        "git@github.com:apm-fixture-org/ref-freshness-source.git"
    )
    consumer = LocalPackageFactory(isolated.work_root).create(
        "ref-freshness-consumer",
        dependencies=(
            {
                "git": remote_url,
                "ref": "main",
            },
        ),
        targets=("copilot",),
    )
    return _Scenario(
        isolated=isolated,
        environment=environment,
        packages=packages,
        repositories=repositories,
        repository=repository,
        consumer=consumer,
        runner=ApmLifecycleRunner(
            (str(apm_binary_path),),
            timeout_seconds=90,
            scenario_timeout_seconds=480,
        ),
        skill_path=repository.worktree / source_skill.relative_to(source.root),
        commit_a=commit_a,
    )


def _advance(scenario: _Scenario, marker: str) -> GitCommit:
    scenario.skill_path.write_text(_skill_document(marker), encoding="utf-8")
    return scenario.repositories.commit(
        scenario.repository,
        message=f"publish {marker}",
    )


def _run(
    scenario: _Scenario,
    args: tuple[str, ...],
    *,
    scenario_id: str,
    expected_returncode: int = 0,
) -> CommandResult:
    result = scenario.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=scenario.consumer.root,
        env=scenario.environment,
    )
    assert result.returncode == expected_returncode, (
        f"scenario={scenario_id!r}\n"
        f"command={result.command!r}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )
    return result


def _locked_commit(scenario: _Scenario) -> str:
    lock = LockFile.read(scenario.consumer.root / "apm.lock.yaml")
    assert lock is not None
    dependencies = lock.get_package_dependencies()
    assert len(dependencies) == 1
    return dependencies[0].resolved_commit


def _deployed_bytes(scenario: _Scenario) -> bytes:
    return (scenario.consumer.root / _SKILL_PATH).read_bytes()


def _module_skill_bytes(scenario: _Scenario) -> bytes:
    matches = list((scenario.consumer.root / "apm_modules").rglob(f"skills/{_SKILL_NAME}/SKILL.md"))
    modules_root = scenario.consumer.root / "apm_modules"
    assert len(matches) == 1, [path.relative_to(modules_root).as_posix() for path in matches]
    return matches[0].read_bytes()


def _capture(scenario: _Scenario) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(
        scenario.consumer.root,
        targets=("copilot",),
    )


def _assert_same_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    assert actual.manifest_bytes == expected.manifest_bytes
    assert actual.lockfile_bytes == expected.lockfile_bytes
    assert actual.deployment_records == expected.deployment_records
    assert actual.files == expected.files
    assert actual.mcp_state_bytes == expected.mcp_state_bytes
    assert actual.lsp_state_bytes == expected.lsp_state_bytes
    assert actual.semantic_bytes == expected.semantic_bytes


def _combined_output(result: CommandResult) -> str:
    return result.stdout + result.stderr


def test_installed_cli_current_state_commands_bypass_stale_bare_cache(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """A -> B -> C -> D proves cache, update, force, outdated, and audit."""
    scenario = _new_scenario(tmp_path / "ref-freshness", apm_binary_path)
    assert scenario.environment["GIT_TERMINAL_PROMPT"] == "0"

    initial = _run(
        scenario,
        _INSTALL_ARGS,
        scenario_id="ref-freshness-install-a",
    )
    assert _locked_commit(scenario) == scenario.commit_a.sha
    assert _deployed_bytes(scenario) == _skill_document("commit-a").encode("utf-8")
    assert "legacy_clone=1" in _combined_output(initial)
    installed_a = _capture(scenario)

    commit_b = _advance(scenario, "commit-b")
    normal = _run(
        scenario,
        _INSTALL_ARGS,
        scenario_id="ref-freshness-normal-install-stays-a",
    )
    assert _locked_commit(scenario) == scenario.commit_a.sha
    assert "ref-resolver tiers:" not in _combined_output(normal)
    assert "legacy_clone=" not in _combined_output(normal)
    _assert_same_state(installed_a, _capture(scenario))

    offline_origin = scenario.repository.origin.with_name(
        f"{scenario.repository.origin.name}.offline"
    )
    scenario.repository.origin.rename(offline_origin)
    try:
        offline = _run(
            scenario,
            _INSTALL_ARGS,
            scenario_id="ref-freshness-offline-install-stays-a",
        )
        assert "ref-resolver tiers:" not in _combined_output(offline)
        _assert_same_state(installed_a, _capture(scenario))
    finally:
        offline_origin.rename(scenario.repository.origin)

    updated_b = _run(
        scenario,
        _UPDATE_ARGS,
        scenario_id="ref-freshness-update-reaches-b",
    )
    assert _locked_commit(scenario) == commit_b.sha
    assert _deployed_bytes(scenario) == _skill_document("commit-b").encode("utf-8")
    assert "legacy_clone=1" in _combined_output(updated_b)
    assert "bare_rev_parse=" not in _combined_output(updated_b)
    assert "Resolving refs from upstream; local cached refs bypassed" in (
        _combined_output(updated_b)
    )

    converged_b = _capture(scenario)
    _run(
        scenario,
        _INSTALL_ARGS,
        scenario_id="ref-freshness-reset-converged-b",
    )
    _assert_same_state(converged_b, _capture(scenario))

    commit_c = _advance(scenario, "commit-c")
    updated_c = _run(
        scenario,
        (*_UPDATE_ARGS, "--force"),
        scenario_id="ref-freshness-force-reaches-c",
    )
    assert _locked_commit(scenario) == commit_c.sha
    assert _deployed_bytes(scenario) == _skill_document("commit-c").encode("utf-8")
    assert "legacy_clone=1" in _combined_output(updated_c)
    assert "bare_rev_parse=" not in _combined_output(updated_c)
    assert "Resolving refs from upstream; local cached refs bypassed" in (
        _combined_output(updated_c)
    )

    commit_d = _advance(scenario, "commit-d")
    before_outdated = _capture(scenario)
    outdated = _run(
        scenario,
        _OUTDATED_ARGS,
        scenario_id="ref-freshness-outdated-sees-d",
    )
    outdated_output = _combined_output(outdated)
    assert "outdated" in outdated_output, outdated_output
    assert commit_d.sha[:8] in outdated_output
    _assert_same_state(before_outdated, _capture(scenario))

    _run(
        scenario,
        _UPDATE_ARGS,
        scenario_id="ref-freshness-apply-outdated-d",
    )
    assert _locked_commit(scenario) == commit_d.sha
    assert _deployed_bytes(scenario) == _skill_document("commit-d").encode("utf-8")
    assert _module_skill_bytes(scenario) == _skill_document("commit-d").encode("utf-8")
    final_state = _capture(scenario)

    final_install = _run(
        scenario,
        _INSTALL_ARGS,
        scenario_id="ref-freshness-final-install-stays-d",
    )
    assert "ref-resolver tiers:" not in _combined_output(final_install)
    audit = _run(
        scenario,
        _AUDIT_ARGS,
        scenario_id="ref-freshness-final-audit",
    )
    audit_payload = json.loads(audit.stdout)
    assert audit_payload["passed"] is True
    assert audit_payload["summary"]["failed"] == 0
    _assert_same_state(final_state, _capture(scenario))

    scenario.repository.origin.rename(offline_origin)
    try:
        last_good = _capture(scenario)
        failed = _run(
            scenario,
            _UPDATE_ARGS,
            scenario_id="ref-freshness-remote-failure-is-closed",
            expected_returncode=1,
        )
        assert "Could not read from remote repository" in _combined_output(failed)
        _assert_same_state(last_good, _capture(scenario))
    finally:
        offline_origin.rename(scenario.repository.origin)
