"""Codex and Hermes MCP configs follow the same symlink policy as the other clients.

A config file that is, or is reached through, a symlink is never written and
never replaced by a regular file: in a project, and under ``$HOME`` at user
scope. A user root relocated outside ``$HOME`` must not be a symlink itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.hermes import HermesClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter
from apm_cli.factory import _MCP_CLIENT_REGISTRY

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-INV-I5", "P-OWN-mcp-json-config-writer"),
    pytest.mark.skipif(sys.platform == "win32", reason="symlink creation"),
]

_SRV = {"pkg-srv": {"command": "npx", "args": ["x"]}}


def _refused(write) -> bool:
    try:
        return write() is False
    except OSError:
        return True


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for variable in ("CODEX_HOME", "HERMES_HOME"):
        monkeypatch.delenv(variable, raising=False)
    return home


def test_codex_project_config_dir_link_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / ".codex"
    outside.mkdir(parents=True)
    before = b'model = "x"\n'
    (outside / "config.toml").write_bytes(before)
    project = tmp_path / "proj"
    project.mkdir()
    os.symlink(outside, project / ".codex", target_is_directory=True)
    adapter = CodexClientAdapter(project_root=project, user_scope=False)

    assert _refused(lambda: adapter.update_config(_SRV))
    assert (outside / "config.toml").read_bytes() == before
    assert (project / ".codex").is_symlink()


def test_codex_project_config_file_link_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.toml"
    target.write_bytes(b"")
    project = tmp_path / "proj"
    (project / ".codex").mkdir(parents=True)
    os.symlink(target, project / ".codex" / "config.toml")
    adapter = CodexClientAdapter(project_root=project)

    assert _refused(lambda: adapter.update_config(_SRV))
    assert target.read_bytes() == b""
    assert (project / ".codex" / "config.toml").is_symlink()


def test_codex_user_config_link_under_home_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    (home / ".codex").mkdir()
    (dotfiles / "config.toml").write_bytes(b'model = "x"\n')
    os.symlink(dotfiles / "config.toml", home / ".codex" / "config.toml")
    adapter = CodexClientAdapter(user_scope=True)

    assert _refused(lambda: adapter.update_config(_SRV))
    assert (home / ".codex" / "config.toml").is_symlink()
    assert (dotfiles / "config.toml").read_bytes() == b'model = "x"\n'


def test_codex_home_outside_home_that_is_a_link_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    real = tmp_path / "real-codex"
    real.mkdir()
    (real / "config.toml").write_bytes(b'model = "x"\n')
    linked = tmp_path / "codex-home"
    os.symlink(real, linked, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(linked))
    adapter = CodexClientAdapter(user_scope=True)

    assert _refused(lambda: adapter.update_config(_SRV))
    assert (real / "config.toml").read_bytes() == b'model = "x"\n'


def test_codex_home_outside_home_regular_dir_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    adapter = CodexClientAdapter(user_scope=True)

    assert adapter.update_config(_SRV) is True
    assert "pkg-srv" in (codex_home / "config.toml").read_text(encoding="utf-8")


def test_hermes_user_config_link_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    config = Path(HermesClientAdapter(user_scope=True).get_config_path())
    assert config.is_relative_to(home)
    config.parent.mkdir(parents=True, exist_ok=True)
    (dotfiles / "config.yaml").write_bytes(b"model: x\n")
    os.symlink(dotfiles / "config.yaml", config)
    adapter = HermesClientAdapter(user_scope=True)

    assert _refused(lambda: adapter.update_config(_SRV))
    assert config.is_symlink()
    assert (dotfiles / "config.yaml").read_bytes() == b"model: x\n"


def test_hermes_home_that_is_a_link_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    real = tmp_path / "real-hermes"
    real.mkdir()
    (real / "config.yaml").write_bytes(b"model: x\n")
    linked = tmp_path / "hermes-home"
    os.symlink(real, linked, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(linked))
    adapter = HermesClientAdapter(user_scope=True)

    assert _refused(lambda: adapter.update_config(_SRV))
    assert (real / "config.yaml").read_bytes() == b"model: x\n"


def test_hermes_regular_user_config_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    adapter = HermesClientAdapter(user_scope=True)

    assert adapter.update_config(_SRV) is True
    assert "pkg-srv" in Path(adapter.get_config_path()).read_text(encoding="utf-8")


def _project_adapter(cls, project: Path):
    with (
        patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        patch("apm_cli.adapters.client.vscode.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.vscode.RegistryIntegration"),
    ):
        return cls(project_root=project, user_scope=False)


# Directories some writers require before they write a project config at all.
_WRITE_PRECONDITION_DIRS = (".claude", ".opencode")


def _registry_cases():
    for name, cls in sorted(_MCP_CLIENT_REGISTRY.items()):
        yield pytest.param(cls, id=name)


@pytest.mark.parametrize("cls", _registry_cases())
def test_every_registered_writer_refuses_a_linked_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cls
) -> None:
    """One guard: every MCP client writer refuses a project config reached through a link.

    A writer whose config is not under the project (no project scope) is
    skipped; adding a writer that bypasses the base policy fails here.
    """
    _home(tmp_path, monkeypatch)
    scratch = tmp_path / "scratch"
    for marker in _WRITE_PRECONDITION_DIRS:
        (scratch / marker).mkdir(parents=True)
    config = Path(_project_adapter(cls, scratch).get_config_path())
    if not Path(os.path.abspath(config)).is_relative_to(scratch):
        pytest.skip(f"{cls.__name__} writes no project-scope config")
    relative = Path(os.path.abspath(config)).relative_to(scratch)

    project = tmp_path / "project"
    for marker in _WRITE_PRECONDITION_DIRS:
        (project / marker).mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if len(relative.parts) == 1:
        seed = outside / relative.name
        seed.write_bytes(b"{}\n")
        os.symlink(seed, project / relative)
        link = project / relative
    else:
        link = project / relative.parts[0]
        if link.exists():
            link.rmdir()
        os.symlink(outside, link, target_is_directory=True)
    before = {path: path.read_bytes() for path in outside.rglob("*") if path.is_file()}
    adapter = _project_adapter(cls, project)
    updates = {"servers": _SRV} if cls is VSCodeClientAdapter else _SRV

    assert _refused(lambda: adapter.update_config(updates)), cls.__name__
    assert link.is_symlink()
    after = {path: path.read_bytes() for path in outside.rglob("*") if path.is_file()}
    assert after == before, cls.__name__
