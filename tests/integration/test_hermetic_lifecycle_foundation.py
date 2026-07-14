"""Cross-module contracts for the hermetic lifecycle test foundation."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import (
    ArtifactSnapshot,
    assert_paths_absent,
    assert_paths_present,
    assert_unchanged,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory
from tests.utils.scenario_rows import (
    LifecycleAction,
    ScenarioObservation,
    ScenarioRow,
)

_AUDIT_ARGS = (
    "audit",
    "--ci",
    "--no-policy",
    "--format",
    "json",
    "--output",
    "reports/audit.json",
)


@dataclass(frozen=True)
class _LifecycleReceipt:
    isolated: IsolatedApmEnvironment
    row: ScenarioRow
    observation: ScenarioObservation
    manifest_bytes: bytes
    commit_sha: str
    git_source: str
    skill_name: str
    instruction_name: str


def _command_evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _assert_action_results(
    row: ScenarioRow,
    results: tuple[CommandResult, ...],
) -> None:
    assert len(results) == len(row.lifecycle_actions)
    for action, result in zip(row.lifecycle_actions, results, strict=True):
        if result.returncode != action.expected_returncode:
            raise AssertionError(_command_evidence(result))


def _fingerprint(snapshot: ArtifactSnapshot, relative_path: str) -> str:
    matches = [entry for entry in snapshot.entries if entry.relative_path == relative_path]
    assert len(matches) == 1
    fingerprint = matches[0].fingerprint
    assert fingerprint is not None
    return fingerprint


def _run_lifecycle_scenario(
    root: Path,
    scenario_id: str,
    *,
    base_env: dict[str, str],
) -> _LifecycleReceipt:
    isolated = IsolatedApmEnvironment.create(root, base_env=base_env)
    environment = isolated.subprocess_env(overrides={"APM_TEST_SCENARIO_ID": scenario_id})
    dependency_name = f"fixture-{scenario_id}"
    skill_name = f"skill-{scenario_id}"
    instruction_name = f"instruction-{scenario_id}"

    package_factory = LocalPackageFactory(isolated.package_root)
    dependency = package_factory.create(dependency_name, targets=("copilot",))
    package_factory.add_skill(
        dependency,
        skill_name,
        (
            "---\n"
            f"name: {skill_name}\n"
            "description: Hermetic lifecycle fixture skill\n"
            "---\n"
            f"# {scenario_id} skill\n"
        ),
    )
    package_factory.add_instruction(
        dependency,
        instruction_name,
        (
            "---\n"
            "applyTo: '**'\n"
            "description: Hermetic lifecycle fixture instruction\n"
            "---\n"
            f"# {scenario_id} instruction\n"
        ),
    )
    source_before = ArtifactSnapshot.capture(dependency.root)

    repository_factory = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repository_factory.create(
        dependency_name,
        source_tree=dependency.root,
    )
    commit = repository_factory.commit(repository, message="seed lifecycle fixture")
    git_source = f"git@gitlab.example.invalid:group/{dependency_name}.git"
    subprocess.run(
        (
            "git",
            "config",
            "--global",
            f"url.{repository.file_url}.insteadOf",
            git_source,
        ),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    project_factory = LocalPackageFactory(isolated.work_root)
    project = project_factory.create(
        f"consumer-{scenario_id}",
        dependencies=(
            {
                "git": git_source,
                "type": "gitlab",
                "ref": commit.sha,
                "alias": dependency_name,
            },
        ),
        targets=("copilot",),
    )
    manifest_bytes = project.manifest_path.read_bytes()
    row = ScenarioRow(
        id=scenario_id,
        source_inputs=(dependency.root, project.manifest_path),
        lifecycle_actions=(
            LifecycleAction(("install", "--target", "copilot")),
            LifecycleAction(("compile", "--target", "copilot", "--force-instructions")),
            LifecycleAction(("pack", "--offline")),
            LifecycleAction(_AUDIT_ARGS),
        ),
        assertions=(),
    )

    results = ApmLifecycleRunner().run_sequence(
        tuple(action.args for action in row.lifecycle_actions),
        cwd=project.root,
        env=environment,
    )
    _assert_action_results(row, results)

    source_after = ArtifactSnapshot.capture(dependency.root)
    project_snapshot = ArtifactSnapshot.capture(project.root)
    project_recapture = ArtifactSnapshot.capture(project.root)
    cache_snapshot = ArtifactSnapshot.capture(isolated.cache_root)
    cache_recapture = ArtifactSnapshot.capture(isolated.cache_root)
    observation = ScenarioObservation(
        source_inputs=row.source_inputs,
        results=results,
        snapshots=(
            source_before,
            source_after,
            project_snapshot,
            project_recapture,
            cache_snapshot,
            cache_recapture,
        ),
    )
    return _LifecycleReceipt(
        isolated=isolated,
        row=row,
        observation=observation,
        manifest_bytes=manifest_bytes,
        commit_sha=commit.sha,
        git_source=git_source,
        skill_name=skill_name,
        instruction_name=instruction_name,
    )


def _assert_lifecycle_receipt(receipt: _LifecycleReceipt) -> None:
    (
        source_before,
        source_after,
        project_snapshot,
        project_recapture,
        cache_snapshot,
        cache_recapture,
    ) = receipt.observation.snapshots
    project_root = receipt.row.source_inputs[1].parent
    bundle_root = f"build/{project_root.name}-0.1.0"
    source_skill = f"skills/{receipt.skill_name}/SKILL.md"
    deployed_skill = f".agents/skills/{receipt.skill_name}/SKILL.md"
    bundled_skill = f"{bundle_root}/skills/{receipt.skill_name}/SKILL.md"
    source_instruction = f".apm/instructions/{receipt.instruction_name}.instructions.md"
    deployed_instruction = f".github/instructions/{receipt.instruction_name}.instructions.md"
    bundled_instruction = f"{bundle_root}/instructions/{receipt.instruction_name}.instructions.md"

    assert_unchanged(source_before, source_after)
    assert_unchanged(project_snapshot, project_recapture)
    assert_unchanged(cache_snapshot, cache_recapture)
    assert_paths_absent(
        source_before,
        {
            "apm.lock.yaml",
            "apm_modules",
            "build",
            "reports",
        },
    )
    assert_paths_present(
        project_snapshot,
        {
            "AGENTS.md",
            "apm.lock.yaml",
            deployed_skill,
            deployed_instruction,
            f"{bundle_root}/apm.lock.yaml",
            bundled_skill,
            bundled_instruction,
            "reports/audit.json",
        },
    )
    assert_paths_present(
        cache_snapshot,
        {
            "git",
            "git/checkouts_v1",
            "git/db_v1",
        },
    )
    assert (
        _fingerprint(source_before, source_skill)
        == _fingerprint(project_snapshot, deployed_skill)
        == _fingerprint(project_snapshot, bundled_skill)
    )
    assert (
        _fingerprint(source_before, source_instruction)
        == _fingerprint(project_snapshot, deployed_instruction)
        == _fingerprint(project_snapshot, bundled_instruction)
    )

    assert receipt.row.source_inputs[1].read_bytes() == receipt.manifest_bytes
    manifest = load_yaml(receipt.row.source_inputs[1])
    source_entry = manifest["dependencies"]["apm"][0]
    assert source_entry == {
        "git": receipt.git_source,
        "type": "gitlab",
        "ref": receipt.commit_sha,
        "alias": receipt.row.source_inputs[0].name,
    }

    lock = load_yaml(project_root / "apm.lock.yaml")
    locked_dependency = lock["dependencies"][0]
    assert locked_dependency["host"] == "gitlab.example.invalid"
    assert locked_dependency["host_type"] == "gitlab"
    assert locked_dependency["resolved_commit"] == receipt.commit_sha
    deployed_hash = locked_dependency["deployed_file_hashes"][deployed_instruction]
    assert deployed_hash == f"sha256:{_fingerprint(project_snapshot, deployed_instruction)}"
    assert locked_dependency["content_hash"].startswith("sha256:")

    report = json.loads((project_root / "reports" / "audit.json").read_text())
    assert isinstance(report, dict)
    environment = receipt.isolated.process_environment
    assert environment["GIT_ALLOW_PROTOCOL"] == "file"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["HOME"] == str(receipt.isolated.home)
    assert "GITHUB_APM_PAT" not in environment
    assert "ADO_APM_PAT" not in environment


@pytest.mark.parametrize("batch", ("first", "second"))
def test_concurrent_real_lifecycles_are_worker_isolated(
    tmp_path: Path,
    worker_id: str,
    batch: str,
) -> None:
    parent_environment = dict(os.environ)
    poisoned_environment = {
        **parent_environment,
        "GITHUB_APM_PAT": "ambient-github-credential",
        "ADO_APM_PAT": "ambient-ado-credential",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.ssh://attacker.invalid/.insteadOf",
        "GIT_CONFIG_VALUE_0": "git@gitlab.example.invalid:",
    }
    scenario_parent = tmp_path / f"{worker_id}-{batch}"
    scenario_parent.mkdir()
    scenario_ids = (f"{batch}-alpha", f"{batch}-beta")

    with ThreadPoolExecutor(max_workers=len(scenario_ids)) as executor:
        futures = {
            scenario_id: executor.submit(
                _run_lifecycle_scenario,
                scenario_parent / scenario_id,
                scenario_id,
                base_env=poisoned_environment,
            )
            for scenario_id in scenario_ids
        }
        receipts = {scenario_id: future.result() for scenario_id, future in futures.items()}

    assert len({receipt.isolated.root for receipt in receipts.values()}) == 2
    assert len({receipt.isolated.home for receipt in receipts.values()}) == 2
    assert len({receipt.isolated.cache_root for receipt in receipts.values()}) == 2
    assert len({receipt.isolated.repository_root for receipt in receipts.values()}) == 2
    assert dict(os.environ) == parent_environment

    for scenario_id, receipt in receipts.items():
        _assert_lifecycle_receipt(receipt)
        project_snapshot = receipt.observation.snapshots[2]
        other_id = next(candidate for candidate in scenario_ids if candidate != scenario_id)
        assert_paths_present(
            project_snapshot,
            {f".agents/skills/skill-{scenario_id}/SKILL.md"},
        )
        assert_paths_absent(
            project_snapshot,
            {f".agents/skills/skill-{other_id}/SKILL.md"},
        )


@pytest.mark.parametrize("mutation", ("deployed-file", "lockfile-hash"))
def test_genuine_generated_artifact_negative_twins_retain_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    scenario_id = mutation.replace("-file", "").replace("-hash", "")
    receipt = _run_lifecycle_scenario(
        tmp_path / scenario_id,
        scenario_id,
        base_env=dict(os.environ),
    )
    _assert_lifecycle_receipt(receipt)
    project_root = receipt.row.source_inputs[1].parent
    instruction_path = f".github/instructions/{receipt.instruction_name}.instructions.md"

    if mutation == "deployed-file":
        (project_root / instruction_path).write_text(
            "# Mutated generated deployment\n",
            encoding="utf-8",
        )
    else:
        lock_path = project_root / "apm.lock.yaml"
        lock = load_yaml(lock_path)
        lock["dependencies"][0]["resolved_ref"] = "0" * 40
        dump_yaml(lock, lock_path)

    failing_action = LifecycleAction(
        ("audit", "--ci", "--no-policy"),
        expected_returncode=1,
    )
    failing_row = ScenarioRow(
        id=f"{scenario_id}-negative",
        source_inputs=receipt.row.source_inputs,
        lifecycle_actions=(failing_action,),
        assertions=(),
    )
    result = ApmLifecycleRunner().run(
        failing_action.args,
        cwd=project_root,
        env=receipt.isolated.subprocess_env(),
    )
    _assert_action_results(failing_row, (result,))
    assert result.stdout
    assert result.stderr

    unexpected_success_row = ScenarioRow(
        id=f"{scenario_id}-unexpected-success",
        source_inputs=receipt.row.source_inputs,
        lifecycle_actions=(LifecycleAction(failing_action.args),),
        assertions=(),
    )
    with pytest.raises(AssertionError) as exc_info:
        _assert_action_results(unexpected_success_row, (result,))
    assert str(exc_info.value) == _command_evidence(result)
