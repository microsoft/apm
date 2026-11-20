"""Test Copilot Runtime (filesystem, process and CLI boundary cases)."""

from unittest.mock import Mock, patch

import pytest

from apm_cli.runtime.copilot_runtime import CopilotRuntime

pytestmark = pytest.mark.component


class TestCopilotRuntime:
    """Test Copilot Runtime."""

    def test_get_runtime_info(self):
        """Test getting runtime information."""
        with (
            patch.object(CopilotRuntime, "is_available", return_value=True),
            patch("subprocess.run") as mock_subprocess,
        ):
            # Mock successful version check
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = "copilot version 1.0.0"
            mock_subprocess.return_value = mock_result

            runtime = CopilotRuntime()
            info = runtime.get_runtime_info()

            assert info["name"] == "copilot"
            assert info["type"] == "copilot_cli"
            assert "capabilities" in info
            assert info["capabilities"]["model_execution"] is True
            assert info["capabilities"]["file_operations"] is True

    def test_get_mcp_config_path_uses_copilot_home(self, monkeypatch, tmp_path):
        """The runtime reads the adapter-owned global Copilot configuration."""
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        with patch.object(CopilotRuntime, "is_available", return_value=True):
            runtime = CopilotRuntime()
            config_path = runtime.get_mcp_config_path()

            assert config_path == tmp_path / "copilot-home" / "mcp-config.json"


class TestMcpConfigUtf8RoundTrip:
    """Reading MCP config preserves non-ASCII content (Windows cp1252/cp950 guard)."""

    def test_get_mcp_servers_reads_non_ascii(self, tmp_path):
        import json as _json

        mcp_path = tmp_path / "mcp-config.json"
        servers = {
            "mcpServers": {
                "demo-cafe": {
                    "command": "node",
                    "args": ["server.js"],
                    "description": "\u4e2d\u6587 description -- cafe",
                }
            }
        }
        mcp_path.write_bytes(_json.dumps(servers).encode("utf-8"))

        with patch.object(CopilotRuntime, "is_available", return_value=True):
            runtime = CopilotRuntime()
            with patch.object(CopilotRuntime, "get_mcp_config_path", return_value=mcp_path):
                got = runtime.get_mcp_servers()

        assert "demo-cafe" in got
        assert got["demo-cafe"]["description"] == "\u4e2d\u6587 description -- cafe"
