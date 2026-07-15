"""Real command contracts for the Consume lifecycle promise."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import (
    ArtifactSnapshot,
    assert_paths_absent,
    assert_paths_present,
    assert_unchanged,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import (
    GitCommit,
    LocalGitRepository,
    LocalGitRepositoryFactory,
)
from tests.utils.local_package import LocalPackageFactory
from tests.utils.scenario_rows import LifecycleAction, ScenarioObservation, ScenarioRow

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")


@dataclass(frozen=True)
class _GitConsumerFixture:
    """Real package, repository, and consumer state for one scenario."""

    isolated: IsolatedApmEnvironment
    environment: dict[str, str]
    repositories: LocalGitRepositoryFactory
    repository: LocalGitRepository
    initial_commit: GitCommit
    git_source: str
    project_root: Path


def _skill_document(name: str, marker: str) -> str:
    """Return one valid skill document with observable content."""
    return f"---\nname: {name}\ndescription: Consume contract skill {name}\n---\n# {marker}\n"


def _configure_local_source_rewrite(
    git_source: str,
    repository: LocalGitRepository,
    *,
    environment: dict[str, str],
) -> None:
    """Route one production-shaped source through the local bare origin."""
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
        timeout=30,
    )


def _create_git_consumer(
    root: Path,
    *,
    git_source: str,
    declare_dependency: bool = True,
) -> _GitConsumerFixture:
    """Create a three-skill package and consumer backed by real local Git."""
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    package_factory = LocalPackageFactory(isolated.package_root)
    bundle = package_factory.create("consume-bundle", targets=("copilot",))
    for skill in ("alpha", "beta", "gamma"):
        package_factory.add_skill(
            bundle,
            skill,
            _skill_document(skill, f"{skill} version one"),
        )

    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create("consume-bundle", source_tree=bundle.root)
    initial_commit = repositories.commit(repository, message="seed consume bundle")
    _configure_local_source_rewrite(
        git_source,
        repository,
        environment=environment,
    )

    consumer = LocalPackageFactory(isolated.work_root).create(
        "consume-project",
        dependencies=(
            (
                {
                    "git": git_source,
                    "type": "gitlab",
                    "ref": "main",
                },
            )
            if declare_dependency
            else ()
        ),
        targets=("copilot",),
    )
    return _GitConsumerFixture(
        isolated=isolated,
        environment=environment,
        repositories=repositories,
        repository=repository,
        initial_commit=initial_commit,
        git_source=git_source,
        project_root=consumer.root,
    )


def _runner(apm_binary_path: Path) -> ApmLifecycleRunner:
    """Return the canonical real-binary lifecycle runner."""
    return ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=120,
        scenario_timeout_seconds=300,
    )


def _locked_dependency(project_root: Path) -> dict[str, object]:
    """Read the single dependency from persisted lock state."""
    lock = load_yaml(project_root / "apm.lock.yaml")
    dependencies = lock["dependencies"]
    assert len(dependencies) == 1
    return dependencies[0]


def _manifest_dependency(project_root: Path) -> dict[str, object]:
    """Read the single dependency from persisted manifest state."""
    manifest = load_yaml(project_root / "apm.yml")
    dependencies = manifest["dependencies"]["apm"]
    assert len(dependencies) == 1
    return dependencies[0]


def _audit_payload(result: CommandResult) -> dict[str, object]:
    """Parse and validate one real audit command result."""
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert payload["summary"]["failed"] == 0
    return payload


def _update_plan_entries(output: str) -> list[str]:
    """Return rendered dependency rows without counting the plan legend."""
    return [
        line.strip()
        for line in output.splitlines()
        if line.startswith("  [~] ") and line.strip() != "[~] updated"
    ]


def test_filtered_install_and_additive_subset_remain_audit_clean(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Install A then B without losing A across manifest, lock, or audit."""
    fixture = _create_git_consumer(
        tmp_path / "additive-subset",
        git_source="git@gitlab.example.invalid:group/consume-bundle.git",
        declare_dependency=False,
    )
    bundle_path = str(fixture.repository.worktree)
    row = ScenarioRow(
        id="filtered-additive-subset",
        source_inputs=(fixture.repository.origin,),
        lifecycle_actions=(
            LifecycleAction(
                (
                    "install",
                    bundle_path,
                    "--skill",
                    "alpha",
                    "--target",
                    "copilot",
                    "--no-policy",
                )
            ),
            LifecycleAction(_AUDIT_ARGS),
        ),
    )
    results = _runner(apm_binary_path).run_sequence(
        tuple(action.args for action in row.lifecycle_actions),
        expected_returncodes=tuple(action.expected_returncode for action in row.lifecycle_actions),
        scenario_id=row.id,
        cwd=fixture.project_root,
        env=fixture.environment,
    )
    first_snapshot = ArtifactSnapshot.capture(fixture.project_root)
    observation = ScenarioObservation(
        source_inputs=row.source_inputs,
        results=results,
        snapshots=(first_snapshot,),
    )

    assert observation.results[0].stdout
    first_lock = _locked_dependency(fixture.project_root)
    assert first_lock["source"] == "local"
    assert first_lock["skill_subset"] == ["alpha"]
    assert _manifest_dependency(fixture.project_root)["skills"] == ["alpha"]
    assert_paths_present(
        first_snapshot,
        {
            "apm.lock.yaml",
            ".agents/skills/alpha/SKILL.md",
        },
    )
    assert_paths_absent(
        first_snapshot,
        {
            ".agents/skills/beta/SKILL.md",
            ".agents/skills/gamma/SKILL.md",
        },
    )
    _audit_payload(observation.results[1])
    alpha_bytes = (fixture.project_root / ".agents" / "skills" / "alpha" / "SKILL.md").read_bytes()

    second_install, second_audit = _runner(apm_binary_path).run_sequence(
        (
            (
                "install",
                bundle_path,
                "--skill",
                "beta",
                "--target",
                "copilot",
                "--no-policy",
            ),
            _AUDIT_ARGS,
        ),
        expected_returncodes=(0, 0),
        scenario_id="additive-subset-second-install",
        cwd=fixture.project_root,
        env=fixture.environment,
    )
    second_snapshot = ArtifactSnapshot.capture(fixture.project_root)

    assert second_install.stdout
    second_lock = _locked_dependency(fixture.project_root)
    assert second_lock["source"] == "local"
    assert second_lock["skill_subset"] == ["alpha", "beta"]
    assert _manifest_dependency(fixture.project_root)["skills"] == ["alpha", "beta"]
    assert_paths_present(
        second_snapshot,
        {
            ".agents/skills/alpha/SKILL.md",
            ".agents/skills/beta/SKILL.md",
        },
    )
    assert_paths_absent(
        second_snapshot,
        {".agents/skills/gamma/SKILL.md"},
    )
    assert (
        fixture.project_root / ".agents" / "skills" / "alpha" / "SKILL.md"
    ).read_bytes() == alpha_bytes
    _audit_payload(second_audit)


@pytest.mark.parametrize(
    "git_source",
    (
        "git@gitlab.example.invalid:group/consume-bundle.git",
        "https://gitlab.example.invalid/group/consume-bundle.git",
    ),
    ids=("scp-source", "https-source"),
)
def test_update_observes_branch_advance_then_converges(
    tmp_path: Path,
    apm_binary_path: Path,
    git_source: str,
) -> None:
    """A real branch advance changes once; the next update has zero effect."""
    fixture = _create_git_consumer(
        tmp_path / git_source.split(":", maxsplit=1)[0].replace("/", "-"),
        git_source=git_source,
    )
    runner = _runner(apm_binary_path)
    install = runner.run(
        ("install", "--target", "copilot", "--no-policy"),
        scenario_id="update-convergence-install",
        cwd=fixture.project_root,
        env=fixture.environment,
    )
    assert install.returncode == 0, install.stderr
    initial_lock = _locked_dependency(fixture.project_root)
    assert initial_lock["resolved_commit"] == fixture.initial_commit.sha

    skill_path = fixture.repository.worktree / "skills" / "alpha" / "SKILL.md"
    skill_path.write_text(
        _skill_document("alpha", "alpha version two"),
        encoding="utf-8",
    )
    advanced_commit = fixture.repositories.commit(
        fixture.repository,
        message="advance consume bundle",
    )

    changed = runner.run(
        ("update", "--yes", "--target", "copilot"),
        scenario_id="update-observes-branch-advance",
        cwd=fixture.project_root,
        env=fixture.environment,
    )
    assert changed.returncode == 0, changed.stderr
    changed_output = changed.stdout + changed.stderr
    assert _update_plan_entries(changed_output) == ["[~] group/consume-bundle"]
    assert "1 updated" in changed_output
    updated_lock = _locked_dependency(fixture.project_root)
    assert updated_lock["resolved_commit"] == advanced_commit.sha
    assert updated_lock["resolved_commit"] != fixture.initial_commit.sha
    assert "alpha version two" in (
        fixture.project_root / ".agents" / "skills" / "alpha" / "SKILL.md"
    ).read_text(encoding="utf-8")
    after_changed_update = ArtifactSnapshot.capture(fixture.project_root)

    unchanged = runner.run(
        ("update", "--yes", "--target", "copilot"),
        scenario_id="update-converges",
        cwd=fixture.project_root,
        env=fixture.environment,
    )
    assert unchanged.returncode == 0, unchanged.stderr
    unchanged_output = unchanged.stdout + unchanged.stderr
    assert _update_plan_entries(unchanged_output) == []
    assert "All dependencies already at their latest matching refs." in unchanged_output
    after_unchanged_update = ArtifactSnapshot.capture(fixture.project_root)
    assert_unchanged(after_changed_update, after_unchanged_update)
    assert _locked_dependency(fixture.project_root)["resolved_commit"] == advanced_commit.sha
