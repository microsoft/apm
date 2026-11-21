"""apm-home-resolution owner: where scope metadata, sources and manifests live (#4121).

Never chdir here: the mutation runner resolves its source paths against the
process working directory. ``Path.cwd`` and ``HOME`` are patched instead.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

from apm_cli.core.scope import (
    InstallScope,
    get_apm_dir,
    get_apm_home,
    get_deploy_root,
    get_lockfile_dir,
    get_manifest_path,
    get_modules_dir,
    get_source_root,
    get_workspace_deploy_root,
    is_user_scope,
    is_user_scope_root,
    manifest_file,
    manifest_root,
    policy_discovery_uses_git_remote,
    scope_manifest_path,
    set_source_root_override,
)

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-apm-home-resolution")]

USER, PROJECT = InstallScope.USER, InstallScope.PROJECT


def _fresh_scope() -> ModuleType:
    """Import the scope module afresh, as a process starting now would.

    It captures the starting directory at import, like a new APM run. The
    working directory is never faked: mutmut resolves its sources through it.
    """
    spec = importlib.util.find_spec("apm_cli.core.scope")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _started_in_a_removed_directory() -> ModuleType:
    """A fresh scope module of a run whose starting directory was already gone."""
    started = _fresh_scope()
    started._STARTUP_CWD = None
    return started


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    for directory in (home, cwd):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("APM_HOME", raising=False)
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: cwd))
    set_source_root_override(None)
    yield {"home": home, "cwd": cwd, "tmp": tmp_path}
    set_source_root_override(None)


def test_apm_home_defaults_to_dot_apm_under_home(dirs: dict[str, Path]) -> None:
    assert get_apm_home() == dirs["home"] / ".apm"


def test_empty_apm_home_keeps_the_default(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APM_HOME", "")
    assert get_apm_home() == dirs["home"] / ".apm"


def test_absolute_apm_home_is_used_as_is(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APM_HOME", str(dirs["tmp"] / "apm-home"))
    assert get_apm_home() == dirs["tmp"] / "apm-home"


def test_apm_home_expands_a_leading_tilde(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APM_HOME", "~/custom")
    assert get_apm_home() == dirs["home"] / "custom"


def test_relative_apm_home_is_anchored_to_the_starting_directory(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _fresh_scope()
    monkeypatch.setenv("APM_HOME", "rel/home")
    # The directory the run started in, not the current one (Path.cwd is moved).
    assert started.get_apm_home() == Path(os.getcwd()) / "rel" / "home"
    assert Path.cwd() == dirs["cwd"]


def test_relative_apm_home_without_a_starting_directory_is_refused(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _started_in_a_removed_directory()
    monkeypatch.setenv("APM_HOME", "rel/home")
    with pytest.raises(ValueError) as refused:
        started.get_apm_home()
    assert str(refused.value) == (
        "APM_HOME='rel/home' is relative, but the starting directory is unavailable. "
        "Set an absolute APM_HOME or start APM from an existing directory."
    )


@pytest.mark.parametrize(("value", "user"), [(USER, True), (PROJECT, False), ("user", False)])
def test_only_the_user_enum_is_user_scope(value: object, user: bool) -> None:
    assert is_user_scope(value) is user


def test_project_roots_follow_the_working_directory(dirs: dict[str, Path]) -> None:
    assert get_source_root(PROJECT) == dirs["cwd"]
    assert get_deploy_root(PROJECT) == dirs["cwd"]
    assert get_apm_dir(PROJECT) == dirs["cwd"]
    assert get_modules_dir(PROJECT) == dirs["cwd"] / "apm_modules"
    assert get_manifest_path(PROJECT) == dirs["cwd"] / "apm.yml"
    assert get_lockfile_dir(PROJECT) == dirs["cwd"]


def test_user_roots_split_home_and_apm_home(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APM_HOME", str(dirs["tmp"] / "apm-home"))
    assert get_source_root(USER) == dirs["home"]
    assert get_deploy_root(USER) == dirs["home"]
    assert get_apm_dir(USER) == dirs["tmp"] / "apm-home"
    assert get_modules_dir(USER) == dirs["tmp"] / "apm-home" / "apm_modules"
    assert get_manifest_path(USER) == dirs["tmp"] / "apm-home" / "apm.yml"
    assert get_lockfile_dir(USER) == dirs["tmp"] / "apm-home"


def test_source_override_moves_project_sources_but_not_deployment(dirs: dict[str, Path]) -> None:
    source = dirs["tmp"] / "src-root"
    source.mkdir()
    set_source_root_override(source)
    assert get_source_root(PROJECT) == source.resolve()
    assert get_manifest_path(PROJECT) == source.resolve() / "apm.yml"
    assert get_deploy_root(PROJECT) == dirs["cwd"]
    assert get_apm_dir(PROJECT) == dirs["cwd"]
    assert get_source_root(USER) == dirs["home"]
    assert get_manifest_path(USER) == dirs["home"] / ".apm" / "apm.yml"
    set_source_root_override(None)
    assert get_source_root(PROJECT) == dirs["cwd"]


def test_source_override_is_stored_resolved(dirs: dict[str, Path]) -> None:
    source = dirs["tmp"] / "a" / ".." / "b"
    (dirs["tmp"] / "b").mkdir()
    set_source_root_override(source)
    assert get_source_root(PROJECT) == (dirs["tmp"] / "b").resolve()


def test_the_user_metadata_workspace_deploys_to_home(dirs: dict[str, Path]) -> None:
    (dirs["home"] / ".apm").mkdir()
    assert get_workspace_deploy_root(dirs["home"] / ".apm") == dirs["home"].resolve()
    other = dirs["tmp"] / "project"
    other.mkdir()
    assert get_workspace_deploy_root(other / ".") == other.resolve()


def test_manifest_file_joins_apm_yml_and_keeps_relative_paths() -> None:
    assert manifest_file(Path("pkg")) == Path("pkg") / "apm.yml"
    assert manifest_file(Path(".")) == Path("apm.yml")
    assert manifest_file("pkg") == Path("pkg") / "apm.yml"


def test_scope_manifest_lives_in_apm_home_at_user_scope(dirs: dict[str, Path]) -> None:
    project = dirs["tmp"] / "project"
    assert manifest_root(project, user_scope=False) == project
    assert manifest_root(str(project), user_scope=False) == project
    assert manifest_root(project, user_scope=True) == dirs["home"] / ".apm"
    assert scope_manifest_path(project, user_scope=False) == project / "apm.yml"
    assert scope_manifest_path(project, user_scope=True) == dirs["home"] / ".apm" / "apm.yml"


def test_user_scope_root_is_apm_home_and_skips_git_remote_policy(dirs: dict[str, Path]) -> None:
    apm_home = dirs["home"] / ".apm"
    apm_home.mkdir()
    project = dirs["tmp"] / "project"
    project.mkdir()
    assert is_user_scope_root(apm_home) is True
    assert is_user_scope_root(str(apm_home / "sub" / "..")) is True
    assert is_user_scope_root(project) is False
    assert policy_discovery_uses_git_remote(apm_home) is False
    assert policy_discovery_uses_git_remote(project) is True


def test_user_scope_root_check_fails_closed_to_project(
    dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _started_in_a_removed_directory()
    monkeypatch.setenv("APM_HOME", "rel")
    assert started.is_user_scope_root(dirs["cwd"]) is False
    assert started.policy_discovery_uses_git_remote(dirs["cwd"]) is True
