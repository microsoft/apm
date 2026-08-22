"""Agent dependency subset persistence and deployment tests (issue #2491)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.policy.ci_checks import _check_agent_subset_consistency


def _make_agent_package(base: Path, names: tuple[str, ...]) -> Path:
    package = base / "agent-package"
    agents_dir = package / ".apm" / "agents"
    agents_dir.mkdir(parents=True)
    for name in names:
        (agents_dir / f"{name}.agent.md").write_text(
            f"---\nname: {name}\ndescription: {name} agent\n---\n# {name}\n",
            encoding="utf-8",
        )
    (package / "apm.yml").write_text(
        yaml.safe_dump({"name": "agent-package", "version": "1.0.0"}),
        encoding="utf-8",
    )
    return package


def _make_project(base: Path) -> Path:
    project = base / "project"
    project.mkdir(parents=True)
    (project / "apm.yml").write_text(
        yaml.safe_dump({"name": "consumer", "version": "1.0.0"}),
        encoding="utf-8",
    )
    return project


def _install(project: Path, monkeypatch, *args: str):
    from apm_cli.cli import cli
    from apm_cli.models.apm_package import clear_apm_yml_cache

    clear_apm_yml_cache()
    monkeypatch.chdir(project)
    return CliRunner().invoke(cli, ["install", *args], catch_exceptions=False)


def _deployed_agents(project: Path) -> set[str]:
    agents_dir = project / ".github" / "agents"
    if not agents_dir.exists():
        return set()
    return {path.name.removesuffix(".agent.md") for path in agents_dir.glob("*.agent.md")}


def _locked_agent_subset(project: Path) -> list[str]:
    data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    dependencies = data.get("dependencies", [])
    entries = dependencies.values() if isinstance(dependencies, dict) else dependencies
    for entry in entries:
        if isinstance(entry, dict) and entry.get("repo_url"):
            return sorted(entry.get("agent_subset") or [])
    return []


class TestAgentSubsetModel:
    def test_object_form_round_trip_is_sorted_and_deduplicated(self):
        ref = DependencyReference.parse_from_dict(
            {"git": "owner/repo", "agents": ["reviewer", "planner", "reviewer"]}
        )

        assert ref.agent_subset == ["planner", "reviewer"]
        assert ref.to_apm_yml_entry()["agents"] == ["planner", "reviewer"]

    @pytest.mark.parametrize("value", [[], "planner", [""], ["team/planner"]])
    def test_invalid_agent_subset_is_rejected(self, value):
        with pytest.raises(ValueError, match=r"agents|agent"):
            DependencyReference.parse_from_dict({"git": "owner/repo", "agents": value})

    def test_lockfile_round_trip_preserves_agent_subset(self):
        locked = LockedDependency.from_dependency_ref(
            DependencyReference(repo_url="owner/repo", agent_subset=["reviewer", "planner"]),
            resolved_commit="a" * 40,
            depth=1,
            resolved_by=None,
        )

        restored = LockedDependency.from_dict(locked.to_dict()).to_dependency_ref()

        assert restored.agent_subset == ["planner", "reviewer"]

    def test_ci_baseline_detects_agent_subset_drift(self):
        ref = DependencyReference(repo_url="owner/repo", agent_subset=["planner"])
        manifest = APMPackage(
            name="consumer",
            version="1.0.0",
            dependencies={"apm": [ref]},
        )
        lock = LockFile(
            dependencies={
                ref.get_unique_key(): LockedDependency(
                    repo_url="owner/repo",
                    agent_subset=["reviewer"],
                )
            }
        )

        result = _check_agent_subset_consistency(manifest, lock)

        assert result.passed is False
        assert result.name == "agent-subset-consistency"
        assert "manifest agents ['planner']" in result.details[0]


class TestAgentSubsetInstall:
    def test_unknown_cli_agent_fails_without_persisting_pin(self, tmp_path, monkeypatch):
        package = _make_agent_package(tmp_path / "source", ("planner", "reviewer"))
        project = _make_project(tmp_path / "consumer")
        original_manifest = (project / "apm.yml").read_text(encoding="utf-8")

        result = _install(
            project,
            monkeypatch,
            str(package),
            "--agent",
            "missing",
            "--target",
            "copilot",
        )

        assert result.exit_code == 1
        assert "missing" in result.output
        assert "planner" in result.output
        assert (project / "apm.yml").read_text(encoding="utf-8") == original_manifest

    def test_cli_subset_is_additive_persisted_and_replayed(self, tmp_path, monkeypatch):
        package = _make_agent_package(tmp_path / "source", ("planner", "reviewer", "writer"))
        project = _make_project(tmp_path / "consumer")

        first = _install(
            project, monkeypatch, str(package), "--agent", "planner", "--target", "copilot"
        )
        assert first.exit_code == 0, first.output
        assert _deployed_agents(project) == {"planner"}

        second = _install(
            project, monkeypatch, str(package), "--agent", "reviewer", "--target", "copilot"
        )
        assert second.exit_code == 0, second.output
        assert _deployed_agents(project) == {"planner", "reviewer"}

        manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
        assert manifest["dependencies"]["apm"][0]["agents"] == ["planner", "reviewer"]
        assert _locked_agent_subset(project) == ["planner", "reviewer"]

        replay = _install(project, monkeypatch, "--target", "copilot")
        assert replay.exit_code == 0, replay.output
        assert _deployed_agents(project) == {"planner", "reviewer"}

        reset = _install(project, monkeypatch, str(package), "--agent", "*", "--target", "copilot")
        assert reset.exit_code == 0, reset.output
        assert _deployed_agents(project) == {"planner", "reviewer", "writer"}
        assert _locked_agent_subset(project) == []
