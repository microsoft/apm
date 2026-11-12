"""Real-binary Azure DevOps lock consumer contract."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_ADO_HOST = "dev.azure.com"
_ADO_ORGANIZATION = "apm-org"
_ADO_PROJECT = "apm-project"
_ADO_REPOSITORY = "ado-consume-bundle"
_ADO_SOURCE = f"https://{_ADO_HOST}/{_ADO_ORGANIZATION}/{_ADO_PROJECT}/_git/{_ADO_REPOSITORY}"
_ADO_LOCK_FIELDS = {"ado_organization", "ado_project", "ado_repo"}
_SKILL_PATH = Path(".agents/skills/ado-contract/SKILL.md")
_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")


def _skill_document(marker: str) -> str:
    return (
        "---\n"
        "name: ado-contract\n"
        "description: Azure DevOps lock consumer contract\n"
        "---\n"
        f"# {marker}\n"
    )


def _configure_ado_rewrite(
    repository: LocalGitRepository,
    *,
    environment: dict[str, str],
) -> None:
    """Route the production-shaped ADO URL through the local bare origin."""
    subprocess.run(
        (
            "git",
            "config",
            "--global",
            f"url.{repository.file_url}.insteadOf",
            _ADO_SOURCE,
        ),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    return ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=420,
    )


def _assert_success(result: CommandResult) -> None:
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _locked_dependency(project_root: Path) -> dict[str, object]:
    lock = load_yaml(project_root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert len(dependencies) == 1
    return dependencies[0]


def _audit_payload(result: CommandResult) -> dict[str, object]:
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert payload["summary"]["failed"] == 0
    return payload


def test_ado_lock_replay_drives_outdated_update_audit_and_convergence(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Generic lock identity reconstructs ADO state through every Consume transition."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "ado-lock", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.package_root)
    bundle = packages.create(_ADO_REPOSITORY, targets=("copilot",))
    source_skill = packages.add_skill(
        bundle,
        "ado-contract",
        _skill_document("version one"),
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create(_ADO_REPOSITORY, source_tree=bundle.root)
    first_commit = repositories.commit(repository, message="seed ADO consume bundle")
    repositories.tag(repository, "v1.0.0", first_commit)
    repository_skill = repository.worktree / source_skill.relative_to(bundle.root)
    repository_skill.write_bytes(_skill_document("version two").encode())
    second_commit = repositories.commit(repository, message="advance ADO consume bundle")
    repositories.tag(repository, "v1.1.0", second_commit)
    _configure_ado_rewrite(repository, environment=environment)

    consumer = LocalPackageFactory(isolated.work_root).create(
        "ado-consumer",
        dependencies=({"git": _ADO_SOURCE, "ref": "v1.0.0"},),
        targets=("copilot",),
    )
    runner = _runner(apm_binary_path)
    install = runner.run(
        ("install", "--target", "copilot", "--no-policy"),
        scenario_id="ado-lock-install",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(install)

    initial_lock = _locked_dependency(consumer.root)
    assert initial_lock["host"] == _ADO_HOST
    assert initial_lock["repo_url"] == (f"{_ADO_ORGANIZATION}/{_ADO_PROJECT}/{_ADO_REPOSITORY}")
    assert _ADO_LOCK_FIELDS.isdisjoint(initial_lock)
    assert initial_lock["resolved_commit"] == first_commit.sha
    assert initial_lock["resolved_ref"] == "v1.0.0"
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("version one").encode()

    lock_document = load_yaml(consumer.root / "apm.lock.yaml")
    lock_document["dependencies"][0]["future_ado_metadata"] = {"preserved": True}
    dump_yaml(lock_document, consumer.root / "apm.lock.yaml")
    reloaded = LockFile.read(consumer.root / "apm.lock.yaml")
    assert reloaded is not None
    locked = next(iter(reloaded.dependencies.values()))
    reconstructed = locked.to_dependency_ref()
    assert reconstructed.host == _ADO_HOST
    assert reconstructed.ado_organization == _ADO_ORGANIZATION
    assert reconstructed.ado_project == _ADO_PROJECT
    assert reconstructed.ado_repo == _ADO_REPOSITORY
    assert reconstructed.reference == "v1.0.0"
    assert reconstructed.to_github_url() == _ADO_SOURCE
    assert locked.to_dict()["future_ado_metadata"] == {"preserved": True}

    outdated = runner.run(
        ("outdated", "--parallel-checks", "0"),
        scenario_id="ado-lock-outdated",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(outdated)
    outdated_output = outdated.stdout + outdated.stderr
    assert "v1.0.0" in outdated_output
    assert "v1.1.0" in outdated_output
    assert "outdated" in outdated_output
    assert "git tags" in outdated_output
    assert "unknown" not in outdated_output

    before_update_audit = runner.run(
        _AUDIT_ARGS,
        scenario_id="ado-lock-audit-before-update",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(before_update_audit)
    _audit_payload(before_update_audit)

    manifest = load_yaml(consumer.root / "apm.yml")
    manifest["dependencies"]["apm"][0]["ref"] = "^1.0.0"
    dump_yaml(manifest, consumer.root / "apm.yml")
    update = runner.run(
        ("update", "--yes", "--target", "copilot", "--verbose"),
        scenario_id="ado-lock-bounded-update",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(update)
    update_output = update.stdout + update.stderr
    assert "1 updated" in update_output

    updated_lock = _locked_dependency(consumer.root)
    assert _ADO_LOCK_FIELDS.isdisjoint(updated_lock)
    assert updated_lock["resolved_commit"] == second_commit.sha, update_output
    assert updated_lock["resolved_ref"] == "v1.1.0"
    assert updated_lock["constraint"] == "^1.0.0"
    assert updated_lock["resolved_tag"] == "v1.1.0"
    assert (consumer.root / _SKILL_PATH).read_bytes() == _skill_document("version two").encode()
    after_update = ArtifactSnapshot.capture(consumer.root)

    converged = runner.run(
        ("update", "--yes", "--target", "copilot"),
        scenario_id="ado-lock-update-converges",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(converged)
    assert "All dependencies already at their latest matching refs." in (
        converged.stdout + converged.stderr
    )
    assert_unchanged(after_update, ArtifactSnapshot.capture(consumer.root))

    final_outdated = runner.run(
        ("outdated", "--parallel-checks", "0"),
        scenario_id="ado-lock-outdated-converged",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(final_outdated)
    assert "All dependencies are up-to-date" in (final_outdated.stdout + final_outdated.stderr)
    final_audit = runner.run(
        _AUDIT_ARGS,
        scenario_id="ado-lock-audit-converged",
        cwd=consumer.root,
        env=environment,
    )
    _assert_success(final_audit)
    _audit_payload(final_audit)
