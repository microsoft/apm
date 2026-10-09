"""End-to-end skill deploy / lifecycle tests for the 'grok-bot' target.

grok-bot maps the skills primitive to ``agent-data/workflows/<skill>/``
(subdir ``workflows``, not ``skills``).  These tests drive the real CLI
against local folder packages and pin every consumer of that mapping:
install, lockfile paths, idempotent reinstall, uninstall, the
``agent-skills`` compatibility rule, and the all-targets-filtered error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

_ENV = {"APM_E2E_TESTS": "1"}
_SKILL = "grok-skill"


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    apm_dir = home / ".apm"
    apm_dir.mkdir(parents=True)
    (apm_dir / "apm.yml").write_text("name: test\ndescription: test\nversion: 0.0.1\n")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    import apm_cli.config as _conf

    monkeypatch.setattr(_conf, "CONFIG_DIR", str(apm_dir))
    monkeypatch.setattr(_conf, "CONFIG_FILE", str(apm_dir / "config.json"))
    monkeypatch.setattr(_conf, "_config_cache", None)
    yield home
    monkeypatch.setattr(_conf, "_config_cache", None)


def _write_manifest(pkg: Path, *, targets: list[str] | None = None) -> None:
    data: dict = {"name": pkg.name, "version": "1.0.0", "description": "grok-bot fixture"}
    if targets is not None:
        data["targets"] = targets
    (pkg / "apm.yml").write_text(yaml.dump(data), encoding="utf-8")


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: A skill for grok-bot tests\n---\n# {name}\nBody.\n"


def _plain_folder_package(root: Path, name: str = _SKILL, **kw) -> Path:
    """``<pkg>/apm.yml`` + ``<pkg>/SKILL.md`` (skill name is the folder name)."""
    pkg = root / name
    pkg.mkdir(parents=True)
    _write_manifest(pkg, **kw)
    (pkg / "SKILL.md").write_text(_skill_md(name), encoding="utf-8")
    return pkg


def _apm_skills_package(
    root: Path, name: str = "apm-pkg", skill: str = "inner-skill", **kw
) -> Path:
    """``<pkg>/apm.yml`` + ``<pkg>/.apm/skills/<skill>/SKILL.md``."""
    pkg = root / name
    skill_dir = pkg / ".apm" / "skills" / skill
    skill_dir.mkdir(parents=True)
    _write_manifest(pkg, **kw)
    (skill_dir / "SKILL.md").write_text(_skill_md(skill), encoding="utf-8")
    return pkg


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(
        yaml.dump({"name": "proj", "version": "1.0.0", "dependencies": {"apm": []}}),
        encoding="utf-8",
    )
    return project


def _apm(args: list[str], cwd: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(cwd)
    return CliRunner().invoke(cli, args, env=_ENV, catch_exceptions=False)


def _lock_deployed(root: Path) -> list[str]:
    lock = yaml.safe_load((root / "apm.lock.yaml").read_text(encoding="utf-8"))
    return sorted(f for dep in lock["dependencies"] for f in dep.get("deployed_files", []))


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "apm_modules" not in p.relative_to(root).parts
    }


class TestProjectScopeDeploy:
    def test_plain_folder_package_lands_in_workflows(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _plain_folder_package(tmp_path / "src")
        project = _project(tmp_path)

        result = _apm(["install", str(pkg), "--target", "grok-bot"], project, monkeypatch)

        assert result.exit_code == 0, result.output
        assert (project / "agent-data" / "workflows" / _SKILL / "SKILL.md").is_file()
        assert not (project / "agent-data" / "skills").exists()
        deployed = _lock_deployed(project)
        assert deployed
        assert all(f.startswith(f"agent-data/workflows/{_SKILL}") for f in deployed), deployed
        assert "agent-data/workflows/" in result.output
        assert "agent-data/skills" not in result.output

    def test_apm_skills_package_lands_in_workflows(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _apm_skills_package(tmp_path / "src")
        project = _project(tmp_path)

        result = _apm(["install", str(pkg), "--target", "grok-bot"], project, monkeypatch)

        assert result.exit_code == 0, result.output
        assert (project / "agent-data" / "workflows" / "inner-skill" / "SKILL.md").is_file()
        assert not (project / "agent-data" / "skills").exists()
        deployed = _lock_deployed(project)
        assert deployed
        assert all(f.startswith("agent-data/workflows/inner-skill") for f in deployed), deployed

    def test_reinstall_is_idempotent(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _plain_folder_package(tmp_path / "src")
        project = _project(tmp_path)
        args = ["install", str(pkg), "--target", "grok-bot"]

        assert _apm(args, project, monkeypatch).exit_code == 0
        before = _snapshot(project)

        second = _apm(["install", "--target", "grok-bot"], project, monkeypatch)

        assert second.exit_code == 0, second.output
        assert _snapshot(project) == before
        assert not (project / "agent-data" / "skills").exists()

    def test_uninstall_removes_deployed_workflows(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _plain_folder_package(tmp_path / "src")
        project = _project(tmp_path)
        assert (
            _apm(["install", str(pkg), "--target", "grok-bot"], project, monkeypatch).exit_code == 0
        )
        skill_dir = project / "agent-data" / "workflows" / _SKILL
        assert skill_dir.is_dir()

        result = _apm(["uninstall", str(pkg)], project, monkeypatch)

        assert result.exit_code == 0, result.output
        assert "could not remove tracked target files" not in result.output.lower()
        assert not skill_dir.exists()

    def test_nothing_deployed_leaves_no_empty_root(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path)

        _apm(["install", "--target", "grok-bot"], project, monkeypatch)

        assert not (project / "agent-data").exists()


class TestGlobalScopeDeploy:
    def test_plain_folder_package_lands_in_home_workflows(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _plain_folder_package(tmp_path / "src")
        cwd = tmp_path / "cwd"
        cwd.mkdir()

        result = _apm(["install", str(pkg), "--target", "grok-bot", "--global"], cwd, monkeypatch)

        assert result.exit_code == 0, result.output
        assert (fake_home / "agent-data" / "workflows" / _SKILL / "SKILL.md").is_file()
        assert not (fake_home / "agent-data" / "skills").exists()
        deployed = _lock_deployed(fake_home / ".apm")
        assert deployed
        assert all(f.startswith(f"agent-data/workflows/{_SKILL}") for f in deployed), deployed

    def test_apm_skills_package_lands_in_home_workflows_and_uninstalls(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _apm_skills_package(tmp_path / "src")
        cwd = tmp_path / "cwd"
        cwd.mkdir()

        result = _apm(["install", str(pkg), "--target", "grok-bot", "--global"], cwd, monkeypatch)
        assert result.exit_code == 0, result.output
        skill_dir = fake_home / "agent-data" / "workflows" / "inner-skill"
        assert (skill_dir / "SKILL.md").is_file()
        assert not (fake_home / "agent-data" / "skills").exists()

        before = _snapshot(fake_home)
        again = _apm(["install", "--target", "grok-bot", "--global"], cwd, monkeypatch)
        assert again.exit_code == 0, again.output
        assert _snapshot(fake_home) == before

        removed = _apm(["uninstall", str(pkg), "--global"], cwd, monkeypatch)
        assert removed.exit_code == 0, removed.output
        assert "could not remove tracked target files" not in removed.output.lower()
        assert not skill_dir.exists()


class TestPackageTargetCompatibility:
    def test_agent_skills_plus_cursor_package_installs_to_grok_bot(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _plain_folder_package(tmp_path / "src", targets=["agent-skills", "cursor"])
        project = _project(tmp_path)

        result = _apm(["install", str(pkg), "--target", "grok-bot"], project, monkeypatch)

        assert result.exit_code == 0, result.output
        assert "do not overlap" not in result.output
        assert (project / "agent-data" / "workflows" / _SKILL / "SKILL.md").is_file()

    def test_non_overlapping_package_targets_fail_with_hint(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _plain_folder_package(tmp_path / "src", targets=["cursor"])
        project = _project(tmp_path)
        before = _snapshot(project)

        result = _apm(["install", str(pkg), "--target", "grok-bot"], project, monkeypatch)

        assert result.exit_code != 0, result.output
        output = " ".join(result.output.split())
        assert "declares targets [cursor]" in output
        assert "requested [grok-bot]" in output
        assert "Install with --target cursor" in output
        assert not (project / "agent-data").exists()
        assert _snapshot(project) == before

    @pytest.mark.parametrize("target", ["grok-bot", "claude"])
    def test_transitive_no_overlap_warns_and_deploys_parent(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, target: str
    ) -> None:
        src = tmp_path / "src"
        child = _plain_folder_package(src, "child-skill", targets=["cursor"])
        parent = _plain_folder_package(src, "parent")
        data = yaml.safe_load((parent / "apm.yml").read_text(encoding="utf-8"))
        data["dependencies"] = {"apm": [str(child)]}
        (parent / "apm.yml").write_text(yaml.dump(data), encoding="utf-8")
        project = _project(tmp_path)

        result = _apm(["install", str(parent), "--target", target], project, monkeypatch)

        assert result.exit_code == 0, result.output
        assert "do not overlap authorized active targets" in " ".join(result.output.split())
        lock_files = _lock_deployed(project)
        if target == "grok-bot":
            assert (project / "agent-data" / "workflows" / "parent" / "SKILL.md").is_file()
            assert "agent-data/workflows/parent" in " ".join(lock_files)
            assert not (project / "agent-data" / "workflows" / "child-skill").exists()
        else:
            assert (project / ".claude" / "skills" / "parent" / "SKILL.md").is_file()
            assert not (project / ".claude" / "skills" / "child-skill").exists()
        assert not any("child-skill" in f for f in lock_files)
        lock = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
        assert any("parent" in dep.get("local_path", "") for dep in lock["dependencies"])

    def test_named_package_subtree_without_overlap_fails_before_writes(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "src"
        good = _plain_folder_package(src, "good-skill")
        bad = _plain_folder_package(src, "bad-skill", targets=["cursor"])
        project = _project(tmp_path)
        before = _snapshot(project)

        result = _apm(
            ["install", str(good), str(bad), "--target", "grok-bot"], project, monkeypatch
        )

        assert result.exit_code != 0, result.output
        output = " ".join(result.output.split())
        assert "declares targets [cursor]" in output
        assert "Install with --target cursor" in output
        assert not (project / "agent-data").exists()
        assert _snapshot(project) == before

    def test_partial_overlap_stays_a_success(
        self, tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pkg = _plain_folder_package(tmp_path / "src", targets=["cursor"])
        project = _project(tmp_path)

        result = _apm(["install", str(pkg), "--target", "grok-bot,cursor"], project, monkeypatch)

        assert result.exit_code == 0, result.output
        assert (project / ".agents" / "skills" / _SKILL / "SKILL.md").is_file()
        assert not (project / "agent-data" / "workflows" / _SKILL).exists()
