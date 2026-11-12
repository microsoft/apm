"""MCP JSON config merges never destroy a file they cannot parse.

Every JSON adapter routes its merge-and-write through the base adapter owner
(``_parse_json_config_for_update`` / ``_write_json_config``): an unparsable
user file (JSONC comments, trailing commas) is left byte-identical and the
write is refused, and project-scope config files that are symbolic links
(or are reached through one) are refused like user-scope ones.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.adapters.client.cursor import CursorClientAdapter
from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.adapters.client.kiro import KiroClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter

_JSONC = '{\n  // user servers\n  "SERVERS": {\n    "user-srv": {"command": "mine"},\n  }\n}\n'
_ENTRY = {"command": "apm-srv", "args": []}


def _seed(path: Path, servers_key: str) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_JSONC.replace("SERVERS", servers_key), encoding="utf-8")
    return path.read_bytes()


@pytest.mark.parametrize(
    ("adapter_cls", "relative"),
    [
        (CopilotClientAdapter, ".github/mcp.json"),
        (GeminiClientAdapter, ".gemini/settings.json"),
        (KiroClientAdapter, ".kiro/settings/mcp.json"),
    ],
    ids=["copilot", "gemini", "kiro"],
)
def test_unparsable_config_is_kept_and_server_reported_failed(
    tmp_path: Path, adapter_cls: type[CopilotClientAdapter], relative: str
) -> None:
    adapter = adapter_cls(project_root=tmp_path)
    config = tmp_path / relative
    before = _seed(config, adapter.mcp_servers_key)

    assert adapter.update_config({"apm-srv": dict(_ENTRY)}) is False
    assert config.read_bytes() == before

    adapter._fetch_server_info = lambda *_args, **_kwargs: {"name": "apm-srv"}
    adapter._format_server_config = lambda *_args, **_kwargs: dict(_ENTRY)
    assert adapter.configure_mcp_server("apm-srv", server_name="apm-srv") is False
    assert config.read_bytes() == before


def test_vscode_jsonc_config_is_kept_and_server_reported_failed(tmp_path: Path) -> None:
    adapter = VSCodeClientAdapter(project_root=tmp_path)
    config = tmp_path / ".vscode/mcp.json"
    before = _seed(config, adapter.mcp_servers_key)

    configured = adapter.configure_mcp_server(
        "apm-srv", server_name="apm-srv", server_info_cache={"apm-srv": {"name": "apm-srv"}}
    )

    assert configured is False
    assert config.read_bytes() == before


_PROJECT_CONFIGS = [
    (ClaudeClientAdapter, ".mcp.json", ".claude"),
    (CopilotClientAdapter, ".github/mcp.json", None),
    (CursorClientAdapter, ".cursor/mcp.json", ".cursor"),
    (GeminiClientAdapter, ".gemini/settings.json", ".gemini"),
]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize(("adapter_cls", "relative", "root_dir"), _PROJECT_CONFIGS)
def test_project_config_that_is_a_symlink_is_refused(
    tmp_path: Path, adapter_cls: type[CopilotClientAdapter], relative: str, root_dir: str | None
) -> None:
    project = tmp_path / "project"
    if root_dir:
        (project / root_dir).mkdir(parents=True)
    config = project / relative
    config.parent.mkdir(parents=True, exist_ok=True)
    linked = tmp_path / "linked.json"
    linked.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    config.symlink_to(linked)

    assert not adapter_cls(project_root=project).update_config({"apm-srv": dict(_ENTRY)})

    assert config.is_symlink()
    assert linked.read_text(encoding="utf-8") == '{"mcpServers": {}}\n'


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_project_config_reached_through_a_linked_directory_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    linked_dir = tmp_path / "shared-vscode"
    linked_dir.mkdir()
    (project / ".vscode").symlink_to(linked_dir)
    adapter = VSCodeClientAdapter(project_root=project)

    configured = adapter.configure_mcp_server(
        "apm-srv", server_name="apm-srv", server_info_cache={"apm-srv": {"name": "apm-srv"}}
    )

    assert configured is False
    assert adapter.update_config({"servers": {}}) is False
    assert list(linked_dir.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_project_configs_keep_the_shared_file_mode(tmp_path: Path) -> None:
    reference = tmp_path / "reference.json"
    reference.write_text("{}", encoding="utf-8")
    shared_mode = stat.S_IMODE(reference.stat().st_mode)
    (tmp_path / ".claude").mkdir()

    assert CopilotClientAdapter(project_root=tmp_path).update_config({"apm-srv": dict(_ENTRY)})
    assert ClaudeClientAdapter(project_root=tmp_path).update_config({"apm-srv": dict(_ENTRY)})

    for relative in (".github/mcp.json", ".mcp.json"):
        assert stat.S_IMODE((tmp_path / relative).stat().st_mode) == shared_mode, relative


def test_vscode_update_config_keeps_a_jsonc_file(tmp_path: Path) -> None:
    adapter = VSCodeClientAdapter(project_root=tmp_path)
    config = tmp_path / ".vscode/mcp.json"
    before = _seed(config, adapter.mcp_servers_key)

    assert adapter.update_config({"servers": {"apm-srv": dict(_ENTRY)}}) is False
    assert config.read_bytes() == before
