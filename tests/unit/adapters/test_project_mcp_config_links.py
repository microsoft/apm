"""Project MCP config writers skip config paths that run through a symlink."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.adapters.client.kiro import KiroClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter

_SERVER = {"localsrv": {"command": "echo", "args": ["ok"]}}


def _adapter(cls, project_root: Path):
    with (
        patch("apm_cli.adapters.client.copilot.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.copilot.RegistryIntegration"),
        patch("apm_cli.adapters.client.vscode.SimpleRegistryClient"),
        patch("apm_cli.adapters.client.vscode.RegistryIntegration"),
    ):
        return cls(project_root=project_root)


def _write(adapter, cls) -> object:
    if cls is VSCodeClientAdapter:
        return adapter.update_config({"servers": _SERVER})
    return adapter.update_config(_SERVER)


@pytest.mark.parametrize(
    ("cls", "config_rel"),
    [
        (ClaudeClientAdapter, ".mcp.json"),
        (CopilotClientAdapter, ".github/mcp.json"),
        (VSCodeClientAdapter, ".vscode/mcp.json"),
        (KiroClientAdapter, ".kiro/settings/mcp.json"),
    ],
)
def test_project_config_link_is_not_written(tmp_path: Path, cls, config_rel: str) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    linked = tmp_path / "linked.json"
    linked.write_text("{}\n", encoding="utf-8")
    config = project / config_rel
    config.parent.mkdir(parents=True, exist_ok=True)
    try:
        config.symlink_to(linked)
    except OSError:
        pytest.skip("symlinks are unavailable")
    adapter = _adapter(cls, project)

    assert _write(adapter, cls) is False
    assert linked.read_text(encoding="utf-8") == "{}\n"
    assert config.is_symlink()


@pytest.mark.parametrize(
    ("cls", "config_rel"),
    [
        (ClaudeClientAdapter, ".mcp.json"),
        (CopilotClientAdapter, ".github/mcp.json"),
        (VSCodeClientAdapter, ".vscode/mcp.json"),
        (KiroClientAdapter, ".kiro/settings/mcp.json"),
    ],
)
def test_project_config_regular_file_is_written(tmp_path: Path, cls, config_rel: str) -> None:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / config_rel).parent.mkdir(parents=True, exist_ok=True)
    adapter = _adapter(cls, project)

    assert _write(adapter, cls) is not False
    written = json.loads((project / config_rel).read_text(encoding="utf-8"))
    assert "localsrv" in written.get("servers", written.get("mcpServers", {}))


def test_project_config_parent_link_is_not_written(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".kiro").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (project / ".kiro" / "settings").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    adapter = _adapter(KiroClientAdapter, project)

    assert _write(adapter, KiroClientAdapter) is False
    assert not any(outside.iterdir())
