"""Project discovery never adopts an apm.yml in $HOME from a subdirectory (#5448).

PROMISE: P-OWN-install-command-scope-selection -- a project-scope install run
from a folder under $HOME operates on that folder, never on the user scope.
CELL: OP=install SCOPE=project START=under-home STRAY=~/apm.yml|~/.claude
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise("P-OWN-install-command-scope-selection"),
]

STRAY_HOME_MANIFEST = "name: home\nversion: 1.0.0\ntargets: [claude]\ndependencies:\n  apm: []\n"


def _describe(result: CommandResult) -> str:
    return f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def _world(tmp_path: Path) -> tuple[IsolatedApmEnvironment, dict[str, str], Path]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_E2E_TESTS"] = "1"
    package = isolated.home / "src" / "pkgs" / "a"
    (package / ".apm" / "skills" / "a-skill").mkdir(parents=True)
    (package / "apm.yml").write_text("name: a\nversion: 1.0.0\n", encoding="utf-8")
    (package / ".apm" / "skills" / "a-skill" / "SKILL.md").write_text(
        "---\nname: a-skill\ndescription: A skill\n---\nBody.\n", encoding="utf-8"
    )
    return isolated, environment, package


def _run(engine: tuple[str, ...], args: tuple[str, ...], cwd: Path, env: dict[str, str]):
    return ApmLifecycleRunner(engine).run(args, scenario_id="home-ceiling", cwd=cwd, env=env)


def test_a_project_install_under_home_ignores_a_stray_home_manifest(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated, environment, _ = _world(tmp_path)
    home_manifest = isolated.home / "apm.yml"
    home_manifest.write_text(STRAY_HOME_MANIFEST, encoding="utf-8")
    before = home_manifest.read_bytes()
    project = isolated.home / "work" / "newproj"
    (project / ".claude").mkdir(parents=True)

    result = _run(apm_engine_command, ("install", "../../src/pkgs/a"), project, environment)

    assert result.returncode == 0, _describe(result)
    assert (project / "apm.yml").is_file(), _describe(result)
    assert (project / ".claude" / "skills" / "a-skill").is_dir(), _describe(result)
    assert home_manifest.read_bytes() == before, _describe(result)
    assert not (isolated.home / "apm.lock.yaml").exists(), _describe(result)
    assert not (isolated.home / ".claude" / "skills").exists(), _describe(result)


def test_a_bare_install_in_an_empty_folder_under_home_reports_no_manifest(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated, environment, _ = _world(tmp_path)
    (isolated.home / "apm.yml").write_text(STRAY_HOME_MANIFEST, encoding="utf-8")
    fresh = isolated.home / "work" / "fresh"
    fresh.mkdir(parents=True)
    home_before = (isolated.home / "apm.yml").read_bytes()

    result = _run(apm_engine_command, ("install",), fresh, environment)

    assert result.returncode == 1, _describe(result)
    assert "No apm.yml found" in result.stdout + result.stderr, _describe(result)
    assert (isolated.home / "apm.yml").read_bytes() == home_before, _describe(result)
    assert not (fresh / "apm.yml").exists(), _describe(result)
    assert not (isolated.home / ".claude").exists(), _describe(result)
    assert not (isolated.home / "apm.lock.yaml").exists(), _describe(result)


def test_a_project_install_after_a_global_install_leaves_home_alone(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated, environment, package = _world(tmp_path)
    work = isolated.home / "work"
    work.mkdir()
    (isolated.home / ".claude").mkdir()
    home_manifest = isolated.home / "apm.yml"
    home_manifest.write_text(STRAY_HOME_MANIFEST, encoding="utf-8")
    home_before = home_manifest.read_bytes()

    global_result = _run(apm_engine_command, ("install", "-g", str(package)), work, environment)
    assert global_result.returncode == 0, _describe(global_result)
    user_manifest = isolated.home / ".apm" / "apm.yml"
    user_before = user_manifest.read_bytes() if user_manifest.exists() else None

    project = work / "newproj"
    (project / ".claude").mkdir(parents=True)
    result = _run(apm_engine_command, ("install", str(package)), project, environment)

    assert result.returncode == 0, _describe(result)
    assert (project / "apm.yml").is_file(), _describe(result)
    assert not (isolated.home / "apm.lock.yaml").exists(), _describe(result)
    assert home_manifest.read_bytes() == home_before, _describe(result)
    after = user_manifest.read_bytes() if user_manifest.exists() else None
    assert after == user_before, _describe(result)


def test_a_project_install_in_the_home_directory_is_refused_and_names_the_fix(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated, environment, package = _world(tmp_path)
    home_manifest = isolated.home / "apm.yml"
    home_manifest.write_text(STRAY_HOME_MANIFEST, encoding="utf-8")
    before = home_manifest.read_bytes()

    result = _run(apm_engine_command, ("install", str(package)), isolated.home, environment)

    output = result.stdout + result.stderr
    assert result.returncode != 0, _describe(result)
    assert "apm install -g" in output and "--root <dir>" in output, _describe(result)
    assert home_manifest.read_bytes() == before, _describe(result)
    assert not (isolated.home / ".claude").exists(), _describe(result)
    assert not (isolated.home / "apm.lock.yaml").exists(), _describe(result)


def test_a_global_install_from_the_home_directory_still_works(
    tmp_path: Path, apm_engine_command: tuple[str, ...]
) -> None:
    isolated, environment, package = _world(tmp_path)
    (isolated.home / ".claude").mkdir()

    result = _run(apm_engine_command, ("install", "-g", str(package)), isolated.home, environment)

    assert result.returncode == 0, _describe(result)
    assert (isolated.home / ".apm" / "apm.yml").is_file(), _describe(result)
