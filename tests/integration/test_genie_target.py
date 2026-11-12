"""Exercise Genie Code CLI deployment through APM's public commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli import config
from apm_cli.cli import cli


def _apm(*args: str) -> str:
    result = CliRunner().invoke(cli, args, env={"APM_E2E_TESTS": "1"}, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.output


def _write_manifest(root: Path, **fields) -> None:
    (root / "apm.yml").write_text(
        yaml.safe_dump({"name": root.name.lstrip("."), "version": "1.0.0", **fields}),
        encoding="utf-8",
    )


def _write_instruction(root: Path) -> None:
    instructions = root / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "demo.instructions.md").write_text(
        "---\napplyTo: '**'\n---\n\nAlways write tests first.\n",
        encoding="utf-8",
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    apm_dir = home / ".apm"
    apm_dir.mkdir(parents=True)
    _write_manifest(apm_dir, targets=["genie"])
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(config, "CONFIG_DIR", str(apm_dir))
    monkeypatch.setattr(config, "CONFIG_FILE", str(apm_dir / "config.json"))
    monkeypatch.setattr(config, "_config_cache", None)
    return home


@pytest.fixture
def package(tmp_path: Path) -> Path:
    package = tmp_path / "example"
    skill = package / ".apm" / "skills" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: example\ndescription: An example skill\n---\n\nExample skill body.\n",
        encoding="utf-8",
    )
    (skill / "reference.md").write_text("Example reference.\n", encoding="utf-8")
    _write_manifest(package, targets=["agent-skills", "codex"])
    _write_instruction(package)
    return package


@pytest.mark.parametrize("global_install", [False, True], ids=["project", "global"])
def test_genie_skill_lifecycle(
    tmp_path: Path,
    home: Path,
    package: Path,
    monkeypatch: pytest.MonkeyPatch,
    global_install: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manifest_root = home / ".apm" if global_install else project
    _write_manifest(
        manifest_root,
        targets=["genie"],
        dependencies={"apm": [{"path": str(package), "targets": ["genie"]}]},
    )
    monkeypatch.chdir(project)
    bundled_skill = home / ".genie" / "skills" / ".system" / "built-in" / "SKILL.md"
    bundled_skill.parent.mkdir(parents=True)
    bundled_skill.write_text("Genie bundled skill.\n", encoding="utf-8")

    scope_args = ["--global"] if global_install else []
    target_args = ["--target", "genie"] if global_install else []
    output = _apm("install", *scope_args, *target_args)
    deployed_root = home / ".genie" if global_install else project / ".agents"
    skill = deployed_root / "skills" / "example"
    assert (skill / "SKILL.md").is_file()
    assert (skill / "reference.md").read_text(encoding="utf-8") == "Example reference.\n"
    lock = yaml.safe_load((manifest_root / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert (
        f"{deployed_root.name}/skills/example/SKILL.md" in lock["dependencies"][0]["deployed_files"]
    )
    if global_install:
        assert "apm compile -g" not in output
        _apm("compile", "--global")
        assert not (deployed_root / "AGENTS.md").exists()

    modules = manifest_root / "apm_modules"
    modules.rename(tmp_path / "previous-apm-modules")
    (skill / "reference.md").unlink()
    update_output = _apm("update", "--yes", "--target", "genie", *scope_args)
    assert modules.is_dir(), update_output
    assert (skill / "reference.md").read_text(encoding="utf-8") == "Example reference.\n"
    with monkeypatch.context() as audit:
        audit.chdir(manifest_root)
        _apm("audit", "--ci")
    _apm("uninstall", str(package), *scope_args)
    assert not skill.exists()
    assert bundled_skill.read_text(encoding="utf-8") == "Genie bundled skill.\n"


def test_genie_project_compile_and_selection(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (".agents", ".genie"):
        (tmp_path / name).mkdir()
    _write_instruction(tmp_path)
    _write_manifest(tmp_path)

    def genie_row() -> dict:
        return next(
            row
            for row in json.loads(_apm("targets", "--all", "--json"))
            if row["target"] == "genie"
        )

    assert genie_row()["status"] == "inactive"
    _apm("compile", "--target", "genie", "--single-agents")
    agents_md = tmp_path / "AGENTS.md"
    assert "Always write tests first." in agents_md.read_text(encoding="utf-8")
    assert genie_row()["status"] == "inactive"

    _write_manifest(tmp_path, targets=["genie", "claude"])
    assert genie_row()["status"] == "active"
    assert genie_row()["source"] == "apm.yml"
    agents_md.unlink()
    _apm("compile", "--single-agents")
    assert "Always write tests first." in agents_md.read_text(encoding="utf-8")
    assert (tmp_path / "CLAUDE.md").is_file()


def test_dropping_genie_preserves_shared_skill(
    tmp_path: Path, home: Path, package: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    deps = {"apm": [{"path": str(package)}]}
    _write_manifest(tmp_path, targets=["genie", "codex"], dependencies=deps)
    _apm("install")
    skill = tmp_path / ".agents" / "skills" / "example" / "SKILL.md"
    original = skill.read_bytes()
    _write_manifest(tmp_path, targets=["codex"], dependencies=deps)
    _apm("install")
    assert skill.read_bytes() == original
    _apm("audit", "--ci")
