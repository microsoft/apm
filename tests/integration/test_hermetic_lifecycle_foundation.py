"""Cross-module contracts for the hermetic lifecycle test foundation."""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
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
_SCENARIO_TIMEOUT_SECONDS = 240.0


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
    scenario_deadline: float


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
    apm_binary_path: Path,
    base_env: dict[str, str],
) -> _LifecycleReceipt:
    scenario_deadline = time.monotonic() + _SCENARIO_TIMEOUT_SECONDS
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
        deadline=scenario_deadline,
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
        timeout=max(0.001, min(30, scenario_deadline - time.monotonic())),
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
    )

    results = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=max(
            0.001,
            scenario_deadline - time.monotonic(),
        ),
    ).run_sequence(
        tuple(action.args for action in row.lifecycle_actions),
        expected_returncodes=tuple(action.expected_returncode for action in row.lifecycle_actions),
        scenario_id=row.id,
        cwd=project.root,
        env=environment,
    )

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
        scenario_deadline=scenario_deadline,
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
    assert report["passed"] is True
    assert report["summary"] == {
        "total": len(report["checks"]),
        "passed": len(report["checks"]),
        "failed": 0,
    }
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["content-integrity"]["passed"] is True
    assert checks["drift"]["passed"] is True
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
    apm_binary_path: Path,
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
                apm_binary_path=apm_binary_path,
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


@pytest.mark.parametrize(
    "mutation",
    (
        "deployed-file",
        "resolved-ref",
        "resolved-commit",
        "content-hash",
        "source-identity",
    ),
)
def test_generated_artifact_tampering_reports_cause_and_recovers(
    tmp_path: Path,
    mutation: str,
    apm_binary_path: Path,
) -> None:
    scenario_id = mutation
    receipt = _run_lifecycle_scenario(
        tmp_path / scenario_id,
        scenario_id,
        apm_binary_path=apm_binary_path,
        base_env=dict(os.environ),
    )
    _assert_lifecycle_receipt(receipt)
    project_root = receipt.row.source_inputs[1].parent
    instruction_path = f".github/instructions/{receipt.instruction_name}.instructions.md"
    lock_path = project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    locked_dependency = lock["dependencies"][0]

    if mutation == "deployed-file":
        (project_root / instruction_path).write_text(
            "# Mutated generated deployment\n",
            encoding="utf-8",
        )
    elif mutation == "resolved-ref":
        locked_dependency["resolved_ref"] = "0" * 40
        dump_yaml(lock, lock_path)
    elif mutation == "resolved-commit":
        locked_dependency["resolved_commit"] = "0" * 40
        dump_yaml(lock, lock_path)
    elif mutation == "content-hash":
        locked_dependency["content_hash"] = f"sha256:{'0' * 64}"
        dump_yaml(lock, lock_path)
    else:
        locked_dependency["host"] = "attacker.invalid"
        locked_dependency["repo_url"] = "attacker/other"
        dump_yaml(lock, lock_path)

    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        scenario_timeout_seconds=max(
            0.001,
            receipt.scenario_deadline - time.monotonic(),
        ),
    )
    environment = receipt.isolated.subprocess_env()
    if mutation == "content-hash":
        negative_args = ("install", "--target", "copilot", "--frozen")
    else:
        negative_args = ("audit", "--ci", "--no-policy", "--format", "json")

    recovery_args = ["install", "--target", "copilot"]
    if mutation in {"content-hash", "resolved-commit", "source-identity"}:
        recovery_args.append("--update")
    result, recovery, clean_audit = runner.run_sequence(
        (
            negative_args,
            tuple(recovery_args),
            ("audit", "--ci", "--no-policy", "--format", "json"),
        ),
        expected_returncodes=(1, 0, 0),
        scenario_id=f"{scenario_id}-tamper-repair",
        cwd=project_root,
        env=environment,
    )

    if mutation == "content-hash":
        normalized_stdout = " ".join(result.stdout.split())
        assert "Content hash mismatch" in normalized_stdout
        assert f"expected sha256:{'0' * 64}" in normalized_stdout
        assert "This may indicate a supply-chain attack." in normalized_stdout
        assert "Use 'apm install --update' to accept new content" in normalized_stdout
        assert result.stderr == ""
    else:
        payload = json.loads(result.stdout)
        failed_checks = {check["name"]: check for check in payload["checks"] if not check["passed"]}
        assert result.stdout.lstrip().startswith("{")
        assert "[x]" not in result.stdout
        assert "[>] Replaying install (cache-only)..." in result.stderr
        if mutation == "deployed-file":
            assert set(failed_checks) == {"content-integrity", "drift"}
            assert instruction_path in failed_checks["content-integrity"]["details"][0]
            assert (
                "'apm install' to restore drifted files"
                in (failed_checks["content-integrity"]["message"])
            )
        elif mutation == "resolved-ref":
            assert set(failed_checks) == {"ref-consistency"}
            assert "run 'apm install'" in failed_checks["ref-consistency"]["message"]
            assert "--update" not in failed_checks["ref-consistency"]["message"]
            assert failed_checks["ref-consistency"]["details"] == [
                f"gitlab.example.invalid/group/fixture-{scenario_id}: "
                f"manifest ref '{receipt.commit_sha}' != lockfile ref '{'0' * 40}'"
            ]
        elif mutation == "resolved-commit":
            assert set(failed_checks) == {"ref-consistency"}
            assert "run 'apm install --update'" in (failed_checks["ref-consistency"]["message"])
            assert failed_checks["ref-consistency"]["details"] == [
                f"gitlab.example.invalid/group/fixture-{scenario_id}: "
                f"manifest commit '{receipt.commit_sha}' != "
                f"lockfile resolved_commit '{'0' * 40}'"
            ]
        else:
            assert set(failed_checks) == {"ref-consistency"}
            assert "run 'apm install --update'" in (failed_checks["ref-consistency"]["message"])
            assert failed_checks["ref-consistency"]["details"] == [
                f"gitlab.example.invalid/group/fixture-{scenario_id}: not found in lockfile"
            ]

    assert recovery.stdout
    assert recovery.stderr == ""

    clean_payload = json.loads(clean_audit.stdout)
    assert clean_payload["passed"] is True
    repaired_dependency = load_yaml(lock_path)["dependencies"][0]
    assert repaired_dependency["host"] == "gitlab.example.invalid"
    assert repaired_dependency["repo_url"] == f"group/fixture-{scenario_id}"
    assert repaired_dependency["resolved_commit"] == receipt.commit_sha
    assert repaired_dependency["content_hash"] != f"sha256:{'0' * 64}"
