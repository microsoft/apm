"""Native executable discovery through isolated Git and CLI consumer processes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.windows_compat, pytest.mark.trusted_executable]

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
_CLI = (sys.executable, "-c", "from apm_cli.cli import main; main()")


def test_supported_interpreter_imports_preserve_types() -> None:
    """Legacy interpreter prerequisites retain string and self-type contracts."""
    from apm_cli.integration.mcp_integrator_install import _TargetSelectionSource
    from apm_cli.models.dependency.provider_coordinates import ProviderCoordinateMixin
    from apm_cli.models.dependency.reference import DependencyReference

    for source in _TargetSelectionSource:
        assert isinstance(source, str)
        assert source == source.value
        assert hash(source) == hash(source.value)
        assert str(source) == source.value
        assert format(source, ">20") == format(source.value, ">20")
        assert json.dumps(source) == json.dumps(source.value)

    class DerivedReference(DependencyReference):
        pass

    original = DerivedReference(repo_url="acme/package")
    derived = original.with_derived_provider_coordinates()
    assert type(derived) is DerivedReference
    assert derived == original
    assert derived is not original
    hints = get_type_hints(ProviderCoordinateMixin.with_derived_provider_coordinates)
    assert hints["self"] is hints["return"]
    assert hints["return"].__bound__.__forward_arg__ == "ProviderCoordinateMixin"


def _source_environment(environment: dict[str, str]) -> dict[str, str]:
    """Import the tested checkout while retaining the fixture network guard."""
    return {
        **environment,
        "PYTHONPATH": os.pathsep.join([str(_SOURCE_ROOT), environment["PYTHONPATH"]]),
    }


def _shadow(project: Path, name: str) -> Path:
    """Create an unusable project executable that must never be selected."""
    executable = project / (f"{name}.exe" if sys.platform == "win32" else name)
    executable.write_bytes(b"This project-local executable must not run.\n")
    executable.chmod(0o700)
    return executable


def _run_cli(
    project: Path, environment: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    """Run a bounded real CLI subprocess, including its real Git children."""
    return subprocess.run(
        (
            *_CLI,
            "install",
            "--target",
            "copilot",
            "--no-policy",
            "--https",
            "--parallel-downloads",
            "0",
            *args,
        ),
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("name", ["git", "gh"])
@pytest.mark.parametrize("project_shadow", [False, True])
def test_real_tools_resolve_and_execute_with_native_pathext(
    tmp_path: Path, name: str, project_shadow: bool
) -> None:
    """Pin the interpreter-specific repro and execute both real resolved tools."""
    installed = shutil.which(name)
    assert installed is not None, f"The native discovery contract requires installed {name}"
    trusted = Path(installed).resolve()
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    if project_shadow:
        _shadow(project, name)
    environment = _source_environment(isolated.subprocess_env())
    environment["PATH"] = os.pathsep.join([str(project), str(trusted.parent)])
    environment["PATHEXT"] = ".EXE;.CMD"
    environment["GH_NO_UPDATE_NOTIFIER"] = "1"
    environment.pop("NoDefaultCurrentDirectoryInExePath", None)
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import json, shutil, subprocess, sys; "
            "from pathlib import Path; "
            "from apm_cli.utils.git_env import get_git_executable, get_gh_executable; "
            "name, directory = sys.argv[1:]; "
            "legacy = shutil.which(str(Path(directory) / name)); "
            "resolved = (get_git_executable if name == 'git' else get_gh_executable)(); "
            "version = subprocess.run([resolved, '--version'], check=True, "
            "capture_output=True, text=True, timeout=60).stdout; "
            "print(json.dumps({'legacy': legacy, 'resolved': resolved, 'version': version}))",
            name,
            str(trusted.parent),
        ),
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert Path(evidence["resolved"]) == trusted
    assert evidence["version"].startswith(f"{name} version ")
    if sys.platform == "win32" and sys.version_info < (3, 12):
        assert evidence["legacy"] is None
    else:
        assert Path(evidence["legacy"]).resolve() == trusted


def test_install_reinstall_and_update_use_trusted_git(tmp_path: Path) -> None:
    """Native Git discovery supports installation, replay, and dependency updates."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    source = isolated.package_root / "package"
    instructions = source / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (source / "apm.yml").write_text(
        "name: trusted-discovery\nversion: 1.0.0\ndescription: local fixture\n",
        encoding="utf-8",
    )
    (instructions / "fixture.instructions.md").write_text("# First\n", encoding="utf-8")
    repository = repositories.create("package", source_tree=source)
    first_commit = repositories.commit(repository, message="First package")
    environment = _source_environment(
        repositories.url_rewrite_subprocess_env(
            repository, "https://github.com/acme/trusted-discovery.git"
        )
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\n"
        "dependencies:\n  apm:\n    - acme/trusted-discovery#main\n",
        encoding="utf-8",
    )
    user_file = project / ".github" / "instructions" / "user.instructions.md"
    user_file.parent.mkdir(parents=True)
    user_file.write_bytes(b"# Keep this user-owned file\n")
    user_config = isolated.home / ".gitconfig"
    original_config = user_config.read_bytes()
    if sys.platform == "win32":
        (project / "git.CMD").write_bytes(b"@exit /b 93\r\n")
    else:
        _shadow(project, "git")
    environment.pop("NoDefaultCurrentDirectoryInExePath", None)
    deployed = user_file.with_name("fixture.instructions.md")
    lockfile = project / "apm.lock.yaml"

    first = _run_cli(project, environment)
    assert first.returncode == 0, first.stdout + first.stderr
    initial_lock = lockfile.read_bytes()
    locked = yaml.safe_load(initial_lock)
    assert locked["dependencies"][0]["resolved_commit"] == first_commit.sha
    assert deployed.read_text(encoding="utf-8") == "# First\n"

    replay = _run_cli(project, environment)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert lockfile.read_bytes() == initial_lock
    assert deployed.read_text(encoding="utf-8") == "# First\n"

    (repository.worktree / ".apm" / "instructions" / "fixture.instructions.md").write_text(
        "# Updated\n", encoding="utf-8"
    )
    updated_commit = repositories.commit(repository, message="Update package")
    updated = _run_cli(project, environment, "--update")
    assert updated.returncode == 0, updated.stdout + updated.stderr
    locked = yaml.safe_load(lockfile.read_bytes())
    assert locked["dependencies"][0]["resolved_commit"] == updated_commit.sha
    assert deployed.read_text(encoding="utf-8") == "# Updated\n"
    assert user_file.read_bytes() == b"# Keep this user-owned file\n"
    assert user_config.read_bytes() == original_config


def test_install_without_trusted_git_fails_without_changing_project(tmp_path: Path) -> None:
    """The real CLI reports a trusted PATH miss without changing user inputs."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest = project / "apm.yml"
    manifest.write_text(
        "name: consumer\nversion: 1.0.0\n"
        "dependencies:\n  apm:\n    - acme/trusted-discovery#main\n",
        encoding="utf-8",
    )
    original_manifest = manifest.read_bytes()
    environment = _source_environment(isolated.subprocess_env())
    environment["PATH"] = str(project)
    environment["GIT_PYTHON_REFRESH"] = "quiet"

    result = _run_cli(project, environment)

    assert result.returncode != 0
    assert "git executable not found" in result.stdout + result.stderr
    assert manifest.read_bytes() == original_manifest
    assert not (project / "apm.lock.yaml").exists()
    assert not any(path.is_file() for path in (project / ".github").rglob("*"))
