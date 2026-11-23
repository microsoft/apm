"""Native executable discovery through isolated Git and CLI consumer processes."""

from __future__ import annotations

import json
import os
import shlex
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


def _plant_project_binary(directory: Path, name: str) -> Path:
    """Place a stand-in for helper *name* in *directory*; return its marker path.

    On Windows the ``.exe`` copy answers any arguments like a successful tool
    and the ``.cmd`` variant writes the marker; on POSIX a script writes it.
    """
    marker = directory / "stand-in-ran"
    if sys.platform == "win32":
        system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
        shutil.copyfile(system32 / "hostname.exe", directory / f"{name}.exe")
        script = '@echo off\r\necho stand-in>"%~dp0stand-in-ran"\r\n'
        (directory / f"{name}.cmd").write_bytes(script.encode("ascii"))
    else:
        stand_in = directory / name
        # An absolute marker path needs no PATH lookup, so a run is always detected.
        stand_in.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\n", encoding="utf-8")
        stand_in.chmod(0o755)
    return marker


def _search_cwd_first(monkeypatch: pytest.MonkeyPatch, *rest: str) -> None:
    """Put the working directory first on PATH (Windows searches it implicitly too)."""
    monkeypatch.setenv("PATH", os.pathsep.join([".", *rest]))


def _system_dirs() -> list[str]:
    """PATH entries that hold no az or scanner install."""
    if sys.platform == "win32":
        return [str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32")]
    return []


@pytest.fixture
def _fresh_helper_caches():
    """Drop cached helper paths so each cell resolves from its own cwd."""
    from apm_cli.core import azure_cli
    from apm_cli.utils import git_env

    git_env._git_executable = None
    azure_cli._provider_singleton = None
    yield
    git_env._git_executable = None
    azure_cli._provider_singleton = None


def _committed_repository(project: Path) -> Path:
    """Create a real repository with one committed ``f.md``; return the real git."""
    git = shutil.which("git")
    assert git is not None
    project.mkdir()
    (project / "f.md").write_text("tracked\n", encoding="utf-8")
    identity = (
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
    )
    for args in (("init", "-q"), ("add", "f.md"), (*identity, "commit", "-q", "-m", "f")):
        subprocess.run((git, *args), cwd=project, check=True, capture_output=True)
    return Path(git).resolve()


def test_lock_evidence_ignores_a_project_local_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fresh_helper_caches
) -> None:
    """Legacy lock evidence asks the real git, not a binary in the checkout."""
    from apm_cli.core.legacy_lock_evidence import git_permits_legacy_adoption

    project = tmp_path / "project"
    git = _committed_repository(project)
    (project / "g.md").write_text("untracked\n", encoding="utf-8")
    marker = _plant_project_binary(project, "git")
    _search_cwd_first(monkeypatch, str(git.parent), *_system_dirs())
    monkeypatch.chdir(project)

    assert git_permits_legacy_adoption(project / "f.md") is True
    assert git_permits_legacy_adoption(project / "g.md") is True
    assert not marker.exists()


def test_audit_tracked_files_ignore_a_project_local_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fresh_helper_caches
) -> None:
    """The audit replay lists tracked files through the real git."""
    from apm_cli.install.audit_replay import _git_tracked_files

    project = tmp_path / "project"
    git = _committed_repository(project)
    marker = _plant_project_binary(project, "git")
    _search_cwd_first(monkeypatch, str(git.parent), *_system_dirs())
    monkeypatch.chdir(project)

    assert _git_tracked_files(project) == frozenset({"f.md"})
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX leg runs the CLI form")
def test_ci_audit_never_runs_a_project_local_git(tmp_path: Path) -> None:
    """``apm audit --ci`` in a checkout that ships its own git uses the real one."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    git = _committed_repository(project)
    (project / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    (project / "apm.lock.yaml").write_text("lockfile_version: '1'\ndependencies: []\n")
    marker = _plant_project_binary(project, "git")
    environment = _source_environment(isolated.subprocess_env())
    environment["PATH"] = os.pathsep.join([".", str(git.parent), "/usr/bin", "/bin"])

    result = subprocess.run(
        (*_CLI, "audit", "--ci", "--no-policy"),
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    # The audit must run to completion, so the probe it protects was reached.
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists(), result.stdout + result.stderr


def test_azure_cli_lookup_ignores_a_project_local_az(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fresh_helper_caches
) -> None:
    """Without an installed az, a project-local one is never selected or run."""
    from apm_cli.core.azure_cli import AzureCliBearerProvider

    project = tmp_path / "project"
    project.mkdir()
    marker = _plant_project_binary(project, "az")
    _search_cwd_first(monkeypatch, *_system_dirs())
    monkeypatch.chdir(project)

    command = AzureCliBearerProvider()._az_command
    assert command is None or not Path(command).resolve().is_relative_to(project.resolve())
    assert not marker.exists()


def test_external_scanner_ignores_a_project_local_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fresh_helper_caches
) -> None:
    """A scanner binary in the checkout neither counts as installed nor runs."""
    from apm_cli.security.external.base import ExternalScanError
    from apm_cli.security.external.skillspector import SkillSpectorAdapter

    project = tmp_path / "project"
    project.mkdir()
    (project / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    marker = _plant_project_binary(project, "skillspector")
    _search_cwd_first(monkeypatch, *_system_dirs())
    monkeypatch.chdir(project)

    adapter = SkillSpectorAdapter()
    assert adapter.is_available()[0] is False
    with pytest.raises(ExternalScanError, match="not found"):
        adapter.scan([project])
    assert not marker.exists()


def test_helper_owner_skips_the_working_directory_in_every_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'.', an empty entry and the project path itself never supply a helper."""
    from apm_cli.utils.git_env import trusted_executable

    project = tmp_path / "project"
    git = _committed_repository(project)
    _plant_project_binary(project, "git")
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(project), str(git.parent)]))
    monkeypatch.chdir(project)

    assert Path(trusted_executable("git") or "").resolve() == git


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows process setting")
def test_cli_start_disables_the_implicit_working_directory_search(tmp_path: Path) -> None:
    """Every child of the CLI inherits NoDefaultCurrentDirectoryInExePath."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    environment = _source_environment(isolated.subprocess_env())
    environment.pop("NoDefaultCurrentDirectoryInExePath", None)
    probe = (
        "import os, sys\n"
        "sys.argv = ['apm', '--version']\n"
        "from apm_cli.cli import main\n"
        "try:\n    main()\nexcept SystemExit:\n    pass\n"
        "print('SETTING=' + os.environ.get('NoDefaultCurrentDirectoryInExePath', ''))\n"
    )
    result = subprocess.run(
        (sys.executable, "-c", probe),
        cwd=isolated.work_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert "SETTING=1" in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize("user_value", [None, "0"], ids=["unset", "user-set"])
def test_user_scripts_keep_the_users_working_directory_search(
    tmp_path: Path, user_value: str | None
) -> None:
    """APM's helpers skip the working directory; scripts the user wrote get their own setting."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    environment = _source_environment(isolated.subprocess_env())
    for key in [key for key in environment if key.upper() == "NODEFAULTCURRENTDIRECTORYINEXEPATH"]:
        del environment[key]
    if user_value is not None:
        environment["NoDefaultCurrentDirectoryInExePath"] = user_value
    probe = (
        "import json, os, sys\n"
        "from apm_cli.utils.git_env import disable_implicit_cwd_search\n"
        "from apm_cli.core.lifecycle_scripts import ScriptEntry\n"
        "from apm_cli.core.script_executors import _build_script_env\n"
        "platform, sys.platform = sys.platform, 'win32'\n"
        "disable_implicit_cwd_search()\n"
        "sys.platform = platform\n"
        "name = 'NoDefaultCurrentDirectoryInExePath'\n"
        "script = _build_script_env(ScriptEntry(script_type='command', event='post-install',"
        " command='x'))\n"
        "print(json.dumps({'apm': os.environ.get(name), 'script': [v for k, v in script.items()"
        " if k.upper() == name.upper()]}))\n"
    )
    result = subprocess.run(
        (sys.executable, "-c", probe),
        cwd=isolated.work_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["apm"] == "1"
    assert evidence["script"] == ([] if user_value is None else [user_value])


def test_runtime_launch_refuses_a_project_local_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With only a project-local runtime on PATH, ``apm run`` refuses and runs nothing."""
    from apm_cli.core.script_runner import ScriptRunner

    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    marker = _plant_project_binary(project, "codex")
    _search_cwd_first(monkeypatch, *_system_dirs())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(project)

    with pytest.raises(RuntimeError, match="apm runtime setup codex"):
        ScriptRunner()._execute_runtime_command("codex exec", "prompt", dict(os.environ))
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_runtime_listing_never_follows_a_link_out_of_the_runtime_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed-runtime link into a checkout is neither reported nor version-probed."""
    from apm_cli.runtime.manager import RuntimeManager

    project = tmp_path / "project"
    project.mkdir()
    marker = _plant_project_binary(project, "codex")
    runtimes = tmp_path / "runtimes"
    runtimes.mkdir()
    (runtimes / "codex").symlink_to(project / "codex")
    _search_cwd_first(monkeypatch, *_system_dirs())
    monkeypatch.chdir(tmp_path)
    manager = RuntimeManager()
    manager.runtime_dir = runtimes

    listed = manager.list_runtimes()["codex"]

    assert listed["path"] is None or not Path(listed["path"]).resolve().is_relative_to(project)
    assert manager.is_runtime_available("codex") is False
    assert not marker.exists()


def _untrusted_path_entries(project: Path) -> str:
    """A PATH whose first entries resolve to the project: '.', empty and absolute."""
    return os.pathsep.join([".", "", str(project), *os.environ.get("PATH", "").split(os.pathsep)])


def _path_entries(env: dict[str, str]) -> list[str]:
    value = next((v for k, v in env.items() if k.upper() == "PATH"), "")
    return value.split(os.pathsep)


def test_apm_setup_scripts_run_with_a_path_outside_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime setup script APM authors never sees the project on PATH."""
    from unittest.mock import MagicMock, patch

    from apm_cli.runtime.manager import RuntimeManager

    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    monkeypatch.setenv("PATH", _untrusted_path_entries(project))
    monkeypatch.chdir(project)

    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
        RuntimeManager().run_embedded_script("echo setup", "echo common")

    entries = _path_entries(run.call_args.kwargs["env"])
    assert "." not in entries
    assert "" not in entries
    assert all(
        not Path(entry).resolve().is_relative_to(project.resolve()) for entry in entries if entry
    )


def test_apm_installer_runs_with_a_path_outside_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The self-update installer environment drops project PATH entries."""
    from apm_cli.commands import self_update

    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    monkeypatch.setenv("PATH", _untrusted_path_entries(project))
    monkeypatch.chdir(project)

    env = self_update._build_self_update_installer_env(
        self_update._resolve_self_update_release("1.0.0")
    )

    entries = _path_entries(env)
    assert "." not in entries
    assert "" not in entries
    assert all(
        not Path(entry).resolve().is_relative_to(project.resolve()) for entry in entries if entry
    )


def test_user_scripts_keep_the_users_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scripts the user wrote keep PATH exactly as the user set it."""
    from apm_cli.core.lifecycle_scripts import ScriptEntry
    from apm_cli.core.script_executors import _build_script_env

    project = tmp_path / "project"
    project.mkdir()
    user_path = _untrusted_path_entries(project)
    monkeypatch.setenv("PATH", user_path)
    monkeypatch.chdir(project)

    env = _build_script_env(ScriptEntry(script_type="command", event="post-install", command="x"))

    assert _path_entries(env) == user_path.split(os.pathsep)


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX shell searches an empty PATH")
def test_apm_script_path_with_no_directory_outside_the_project_is_refused(tmp_path: Path) -> None:
    """With only '.' and the project on PATH, no APM-authored script is launched."""
    from apm_cli.utils.git_env import trusted_path_environment

    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    marker = _plant_project_binary(project, "probe")
    original = os.getcwd()
    os.chdir(project)
    try:
        try:
            env = trusted_path_environment({"PATH": os.pathsep.join([".", str(project)])})
        except FileNotFoundError:
            env = None
        if env is not None:
            subprocess.run(
                ("/bin/sh", "-c", "probe"), cwd=project, env=env, capture_output=True, check=False
            )
    finally:
        os.chdir(original)

    assert env is None
    assert not marker.exists()
