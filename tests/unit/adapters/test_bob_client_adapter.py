"""IBM Bob MCP adapter tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.adapters.client.bob import BobClientAdapter
from apm_cli.factory import ClientFactory


def test_factory_creates_bob_adapter() -> None:
    assert isinstance(ClientFactory.create_client("BoB"), BobClientAdapter)


def test_bob_config_paths_follow_documented_scopes(tmp_path: Path) -> None:
    assert BobClientAdapter(project_root=tmp_path).get_config_path() == str(
        tmp_path / ".bob" / "mcp.json"
    )
    with patch("pathlib.Path.home", return_value=tmp_path):
        assert BobClientAdapter(user_scope=True).get_config_path() == str(
            tmp_path / ".bob" / "mcp.json"
        )


def test_update_config_preserves_user_keys_and_makes_private_file(tmp_path: Path) -> None:
    bob_dir = tmp_path / ".bob"
    bob_dir.mkdir()
    config_path = bob_dir / "mcp.json"
    config_path.write_text(
        json.dumps({"userSetting": True, "mcpServers": {"keep": {"command": "node"}}}),
        encoding="utf-8",
    )

    BobClientAdapter(project_root=tmp_path).update_config(
        {"new": {"command": "python", "args": ["server.py"]}}
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["userSetting"] is True
    assert set(data["mcpServers"]) == {"keep", "new"}
    if sys.platform != "win32":
        assert config_path.stat().st_mode & 0o777 == 0o600


def test_stdio_config_uses_bob_schema_and_resolves_env(tmp_path: Path) -> None:
    adapter = BobClientAdapter(project_root=tmp_path)
    server_info = {
        "id": "not-written",
        "name": "local",
        "_raw_stdio": {
            "command": "node",
            "args": ["server.js", "--token", "${BOB_TOKEN}"],
            "cwd": "/workspace",
            "env": {"BOB_TOKEN": "${BOB_TOKEN}"},
        },
        "alwaysAllow": ["search"],
    }

    with patch.dict("os.environ", {"BOB_TOKEN": "resolved-secret"}, clear=False):
        config = adapter._format_server_config(server_info)

    assert config == {
        "command": "node",
        # New-style placeholders in argv are preserved by APM's legacy-mode
        # compatibility contract; the corresponding env block is resolved.
        "args": ["server.js", "--token", "${BOB_TOKEN}"],
        "cwd": "/workspace",
        "env": {"BOB_TOKEN": "resolved-secret"},
        "alwaysAllow": ["search"],
    }
    assert "type" not in config
    assert "tools" not in config
    assert "id" not in config


@pytest.mark.parametrize(
    ("transport", "expected_type"),
    [("http", "streamable-http"), ("streamable-http", "streamable-http"), ("sse", None)],
)
def test_remote_transport_uses_bob_schema(transport: str, expected_type: str | None) -> None:
    adapter = BobClientAdapter()
    server_info = {
        "name": "remote",
        "remotes": [
            {
                "transport_type": transport,
                "url": "https://example.test/mcp",
                "headers": [{"name": "Authorization", "value": "Bearer ${BOB_TOKEN}"}],
            }
        ],
    }
    with patch.dict("os.environ", {"BOB_TOKEN": "secret"}, clear=False):
        config = adapter._format_server_config(server_info)

    assert config["url"] == "https://example.test/mcp"
    assert config["headers"] == {"Authorization": "Bearer secret"}
    assert config.get("type") == expected_type
    assert "tools" not in config
    assert "id" not in config


def test_configure_server_writes_project_file(tmp_path: Path) -> None:
    adapter = BobClientAdapter(project_root=tmp_path)
    adapter.registry_client = MagicMock()
    adapter.registry_client.find_server_by_reference.return_value = {
        "packages": [{"name": "pkg", "registry_name": "npm", "runtime_hint": "npx"}]
    }

    assert adapter.configure_mcp_server("scope/server", server_name="srv") is True
    data = json.loads((tmp_path / ".bob" / "mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["srv"]["command"] == "npx"


def test_project_runtime_discovery_uses_bob_directory(tmp_path: Path) -> None:
    from apm_cli.integration.mcp_integrator_install import _discover_installed_runtimes

    (tmp_path / ".bob").mkdir()
    assert "bob" in _discover_installed_runtimes(tmp_path, user_scope=False)


def test_user_runtime_discovery_uses_global_bob_directory(tmp_path: Path) -> None:
    from apm_cli.integration.mcp_integrator_install import _discover_installed_runtimes

    (tmp_path / ".bob").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    with patch("pathlib.Path.home", return_value=tmp_path):
        assert "bob" in _discover_installed_runtimes(project, user_scope=True)


def test_remove_stale_bob_project_config(tmp_path: Path) -> None:
    from apm_cli.integration.mcp_integrator import MCPIntegrator

    bob_dir = tmp_path / ".bob"
    bob_dir.mkdir()
    config_path = bob_dir / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "keep": {"command": "node"},
                    "stale": {"command": "python"},
                }
            }
        ),
        encoding="utf-8",
    )

    MCPIntegrator.remove_stale({"stale"}, runtime="bob", project_root=tmp_path)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"keep"}
