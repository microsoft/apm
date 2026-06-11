"""Acceptance tests for Kiro MCP adapter support (#702)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.adapters.client.kiro import KiroClientAdapter
from apm_cli.factory import ClientFactory


class TestKiroClientFactory:
    """Verify KiroClientAdapter registration."""

    def test_factory_creates_kiro_adapter(self) -> None:
        adapter = ClientFactory.create_client("kiro")
        assert isinstance(adapter, KiroClientAdapter)

    def test_factory_accepts_case_insensitive_name(self) -> None:
        adapter = ClientFactory.create_client("Kiro")
        assert isinstance(adapter, KiroClientAdapter)


class TestKiroClientAdapter:
    """Core config operations for KiroClientAdapter."""

    def test_project_config_path_uses_kiro_settings(self, tmp_path: Path) -> None:
        adapter = KiroClientAdapter(project_root=tmp_path)
        assert adapter.get_config_path() == str(tmp_path / ".kiro" / "settings" / "mcp.json")

    def test_user_scope_config_path_uses_home_kiro_settings(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            adapter = KiroClientAdapter(user_scope=True)
            assert adapter.get_config_path() == str(tmp_path / ".kiro" / "settings" / "mcp.json")

    def test_update_config_skips_project_without_kiro_dir(self, tmp_path: Path) -> None:
        adapter = KiroClientAdapter(project_root=tmp_path)
        adapter.update_config({"srv": {"command": "node"}})
        assert not (tmp_path / ".kiro" / "settings" / "mcp.json").exists()

    def test_update_config_creates_settings_inside_existing_kiro_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".kiro").mkdir()
        adapter = KiroClientAdapter(project_root=tmp_path)

        adapter.update_config({"srv": {"command": "node", "args": ["server.js"]}})

        mcp_json = tmp_path / ".kiro" / "settings" / "mcp.json"
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["srv"]["command"] == "node"
        assert data["mcpServers"]["srv"]["args"] == ["server.js"]

    def test_user_scope_writes_without_existing_kiro_dir(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            adapter = KiroClientAdapter(user_scope=True)
            adapter.update_config({"srv": {"command": "node"}})

        mcp_json = tmp_path / ".kiro" / "settings" / "mcp.json"
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert data["mcpServers"]["srv"]["command"] == "node"

    def test_remote_config_uses_kiro_url_headers_and_tool_extensions(self) -> None:
        adapter = KiroClientAdapter()
        server_info = {
            "id": "registry-id-is-not-kiro-config",
            "name": "remote-server",
            "remotes": [
                {
                    "transport_type": "http",
                    "url": "https://mcp.example.com/server",
                    "headers": [{"name": "Authorization", "value": "Bearer ${KIRO_TOKEN}"}],
                }
            ],
            "autoApprove": ["search"],
            "disabledTools": ["delete"],
        }

        with patch.dict("os.environ", {"KIRO_TOKEN": "literal-secret"}, clear=False):
            config = adapter._format_server_config(server_info)

        assert config == {
            "url": "https://mcp.example.com/server",
            "headers": {"Authorization": "Bearer ${KIRO_TOKEN}"},
            "autoApprove": ["search"],
            "disabledTools": ["delete"],
        }
        assert "literal-secret" not in json.dumps(config)
        assert "type" not in config
        assert "tools" not in config
        assert "id" not in config

    def test_remote_header_values_are_string_coerced(self) -> None:
        adapter = KiroClientAdapter()
        server_info = {
            "name": "remote-server",
            "remotes": [
                {
                    "transport_type": "http",
                    "url": "https://mcp.example.com/server",
                    "headers": [{"name": "X-Retry", "value": 3}],
                }
            ],
        }

        config = adapter._format_server_config(server_info)

        assert config["headers"] == {"X-Retry": "3"}

    def test_remote_unsupported_transport_raises_for_kiro(self) -> None:
        adapter = KiroClientAdapter()
        server_info = {
            "name": "remote-server",
            "remotes": [
                {
                    "transport_type": "websocket",
                    "url": "https://mcp.example.com/server",
                }
            ],
        }

        try:
            adapter._format_server_config(server_info)
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("Expected ValueError for unsupported Kiro transport")

        assert "Unsupported remote transport" in message
        assert "websocket" in message
        assert "Kiro" in message

    def test_stdio_env_literals_are_written_as_runtime_placeholders(self) -> None:
        adapter = KiroClientAdapter()
        server_info = {
            "name": "stdio-server",
            "_raw_stdio": {
                "command": "node",
                "args": ["server.js", "--token", "${KIRO_TOKEN}"],
                "env": {"KIRO_TOKEN": "literal-secret"},
            },
        }

        with patch.dict("os.environ", {"KIRO_TOKEN": "ignored-os-secret"}, clear=False):
            config = adapter._format_server_config(server_info)

        assert config["command"] == "node"
        assert config["args"] == ["server.js", "--token", "${KIRO_TOKEN}"]
        assert config["env"] == {"KIRO_TOKEN": "${KIRO_TOKEN}"}
        assert "literal-secret" not in json.dumps(config)
        assert "ignored-os-secret" not in json.dumps(config)

    def test_configure_mcp_server_writes_project_config(self, tmp_path: Path) -> None:
        (tmp_path / ".kiro").mkdir()
        adapter = KiroClientAdapter(project_root=tmp_path)
        adapter.registry_client = MagicMock()
        adapter.registry_client.find_server_by_reference.return_value = {
            "packages": [{"name": "pkg", "registry_name": "npm", "runtime_hint": "npx"}]
        }

        assert adapter.configure_mcp_server("scope/server", server_name="srv") is True

        data = json.loads((tmp_path / ".kiro" / "settings" / "mcp.json").read_text())
        assert data["mcpServers"]["srv"]["command"] == "npx"

    def test_configure_mcp_server_error_names_config_key(self, tmp_path: Path) -> None:
        (tmp_path / ".kiro").mkdir()
        adapter = KiroClientAdapter(project_root=tmp_path)
        adapter.registry_client = MagicMock()
        adapter.registry_client.find_server_by_reference.return_value = {
            "name": "broken-server",
            "remotes": [{"transport_type": "websocket", "url": "https://mcp.example.com"}],
        }

        with patch("apm_cli.adapters.client.kiro._rich_error") as rich_error:
            assert adapter.configure_mcp_server("scope/server", server_name="srv") is False

        rich_error.assert_called_once_with(
            "Failed to configure MCP server 'srv' for Kiro",
            symbol="error",
        )

    def test_configure_mcp_server_sets_disabled_when_enabled_false(self, tmp_path: Path) -> None:
        (tmp_path / ".kiro").mkdir()
        adapter = KiroClientAdapter(project_root=tmp_path)
        adapter.registry_client = MagicMock()
        adapter.registry_client.find_server_by_reference.return_value = {
            "packages": [{"name": "pkg", "registry_name": "npm", "runtime_hint": "npx"}]
        }

        assert (
            adapter.configure_mcp_server("scope/server", server_name="srv", enabled=False) is True
        )

        data = json.loads((tmp_path / ".kiro" / "settings" / "mcp.json").read_text())
        assert data["mcpServers"]["srv"]["disabled"] is True

    def test_remove_stale_kiro_project_config(self, tmp_path: Path) -> None:
        from apm_cli.integration.mcp_integrator import MCPIntegrator

        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        mcp_json = settings_dir / "mcp.json"
        mcp_json.write_text(
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

        MCPIntegrator.remove_stale({"stale"}, runtime="kiro", project_root=tmp_path)

        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "keep" in data["mcpServers"]
        assert "stale" not in data["mcpServers"]
