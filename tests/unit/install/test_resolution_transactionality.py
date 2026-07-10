"""Regression coverage for transactional dependency resolution."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.models.apm_package import clear_apm_yml_cache


def _write_package(path: Path, name: str, dependency: str | None = None) -> None:
    """Write a local package and one Copilot instruction."""
    path.mkdir(parents=True, exist_ok=True)
    dependency_block = ""
    if dependency is not None:
        dependency_block = f"dependencies:\n  apm:\n    - path: {dependency}\n"
    (path / "apm.yml").write_text(
        f"name: {name}\nversion: 1.0.0\n{dependency_block}",
        encoding="ascii",
    )
    instructions = path / ".apm" / "instructions"
    instructions.mkdir(parents=True, exist_ok=True)
    (instructions / f"{name}.instructions.md").write_text(
        f"# {name}\n",
        encoding="ascii",
    )


def test_resolution_rollback_restores_replaced_cache_path(tmp_path: Path) -> None:
    """Rollback must restore a valid cache entry replaced by this attempt."""
    modules = tmp_path / "apm_modules"
    package = modules / "_local" / "package-a"
    package.mkdir(parents=True)
    marker = package / "marker.txt"
    marker.write_text("original", encoding="ascii")
    session = ResolutionStagingSession(modules)

    session.prepare_path(package)
    package.mkdir(parents=True)
    marker.write_text("replacement", encoding="ascii")
    session.rollback()

    assert marker.read_text(encoding="ascii") == "original"
    assert not (modules / ".apm-resolution-staging").exists()


def test_corrected_local_cycle_resumes_without_manual_cache_deletion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A rejected local cycle must not leave snapshots that poison the retry."""
    workspace = tmp_path / "workspace"
    project = workspace / "project"
    package_a = workspace / "package-a"
    package_b = workspace / "package-b"
    home = workspace / "home"
    project.mkdir(parents=True)
    home.mkdir()
    (project / ".github").mkdir()
    (project / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ndependencies:\n  apm:\n    - path: ../package-a\n",
        encoding="ascii",
    )
    _write_package(package_a, "package-a", "../package-b")
    _write_package(package_b, "package-b", "../package-a")

    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    runner = CliRunner()

    rejected = runner.invoke(
        cli,
        ["install", "--target", "copilot", "--verbose"],
        catch_exceptions=True,
    )

    assert rejected.exit_code != 0, rejected.output
    modules = project / "apm_modules"
    assert not (modules / "_local" / "package-a").exists()
    assert not (modules / "_local" / "package-b").exists()
    assert not (project / "apm.lock.yaml").exists()

    _write_package(package_b, "package-b")
    clear_apm_yml_cache()
    resumed = runner.invoke(
        cli,
        ["install", "--target", "copilot", "--verbose"],
        catch_exceptions=True,
    )

    assert resumed.exit_code == 0, resumed.output
    assert (project / "apm.lock.yaml").is_file()
    assert (project / ".github" / "instructions" / "package-a.instructions.md").is_file()
