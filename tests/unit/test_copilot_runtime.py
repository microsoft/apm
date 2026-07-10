"""Test Copilot Runtime."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from apm_cli.runtime.copilot_runtime import CopilotRuntime


class TestCopilotRuntime:
    """Test Copilot Runtime."""

    def test_get_runtime_name(self):
        """Test getting runtime name."""
        assert CopilotRuntime.get_runtime_name() == "copilot"

    def test_runtime_name_static(self):
        """Test runtime name is consistent."""
        with patch.object(CopilotRuntime, "is_available", return_value=True):
            runtime = CopilotRuntime()
            assert runtime.get_runtime_name() == "copilot"

    @patch("shutil.which")
    def test_is_available_true(self, mock_which):
        """Test is_available when copilot binary exists."""
        mock_which.return_value = "/usr/local/bin/copilot"
        assert CopilotRuntime.is_available() is True

    @patch("shutil.which")
    def test_is_available_false(self, mock_which):
        """Test is_available when copilot binary doesn't exist."""
        mock_which.return_value = None
        assert CopilotRuntime.is_available() is False

    def test_initialization_without_copilot(self):
        """Test initialization fails gracefully when copilot not available."""
        with patch.object(CopilotRuntime, "is_available", return_value=False):
            with pytest.raises(RuntimeError, match="GitHub Copilot CLI not available"):
                CopilotRuntime()

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

            # Test case 1: Local .mcp.json config path
            with patch.object(runtime, "get_mcp_config_path", return_value=Path("/workspace/.mcp.json")):
                info = runtime.get_runtime_info()
                assert info["capabilities"]["configuration"] == ".mcp.json"

            # Test case 2: Local .github/mcp.json config path
            with patch.object(runtime, "get_mcp_config_path", return_value=Path("/workspace/.github/mcp.json")):
                info = runtime.get_runtime_info()
                assert info["capabilities"]["configuration"] == ".github/mcp.json"

            # Test case 3: Global fallback config path
            with patch.object(runtime, "get_mcp_config_path", return_value=Path("/home/user/.copilot/mcp-config.json")):
                info = runtime.get_runtime_info()
                assert info["capabilities"]["configuration"] == "~/.copilot/mcp-config.json"

            assert info["name"] == "copilot"
            assert info["type"] == "copilot_cli"
            assert "capabilities" in info
            assert info["capabilities"]["model_execution"] is True
            assert info["capabilities"]["file_operations"] is True

    def test_list_available_models(self):
        """Test listing available models."""
        with patch.object(CopilotRuntime, "is_available", return_value=True):
            runtime = CopilotRuntime()
            models = runtime.list_available_models()

            assert "copilot-default" in models
            assert models["copilot-default"]["provider"] == "github-copilot"

    def test_get_mcp_config_path(self, tmp_path):
        """Test getting MCP configuration path."""
        with patch.object(CopilotRuntime, "is_available", return_value=True):
            runtime = CopilotRuntime()

            # Setup mock directories
            mock_cwd = tmp_path / "workspace"
            mock_cwd.mkdir()
            mock_home = tmp_path / "home"
            mock_home.mkdir()
            mock_copilot = mock_home / ".copilot"
            mock_copilot.mkdir()

            with (
                patch("pathlib.Path.cwd", return_value=mock_cwd),
                patch("pathlib.Path.home", return_value=mock_home),
            ):
                # 1. Global fallback (no workspace config)
                config_path = runtime.get_mcp_config_path()
                assert config_path.as_posix().endswith(".copilot/mcp-config.json")

                # 2. .github/mcp.json exists
                mock_github = mock_cwd / ".github"
                mock_github.mkdir()
                (mock_github / "mcp.json").touch()
                config_path = runtime.get_mcp_config_path()
                assert config_path.name == "mcp.json"
                assert ".github" in config_path.parts

                # 3. .mcp.json exists (highest priority)
                (mock_cwd / ".mcp.json").touch()
                config_path = runtime.get_mcp_config_path()
                assert config_path.name == ".mcp.json"
                assert ".github" not in config_path.parts

    def test_execute_prompt_basic(self):
        """Test basic prompt execution."""
        with (
            patch.object(CopilotRuntime, "is_available", return_value=True),
            patch("subprocess.Popen") as mock_popen,
        ):
            # Mock process
            mock_process = Mock()
            mock_process.stdout.readline.side_effect = [
                "Hello from Copilot!\n",
                "Task completed.\n",
                "",  # End of output
            ]
            mock_process.wait.return_value = 0
            mock_popen.return_value = mock_process

            runtime = CopilotRuntime()
            result = runtime.execute_prompt("Test prompt")

            assert "Hello from Copilot!" in result
            assert "Task completed." in result

            # Verify command was called correctly
            mock_popen.assert_called_once()
            call_args = mock_popen.call_args[0][0]
            assert call_args[0] == "copilot"
            assert "-p" in call_args
            assert "Test prompt" in call_args

    def test_execute_prompt_with_options(self):
        """Test prompt execution with additional options."""
        with (
            patch.object(CopilotRuntime, "is_available", return_value=True),
            patch("subprocess.Popen") as mock_popen,
        ):
            # Mock process
            mock_process = Mock()
            mock_process.stdout.readline.side_effect = ["Output\n", ""]
            mock_process.wait.return_value = 0
            mock_popen.return_value = mock_process

            runtime = CopilotRuntime()
            result = runtime.execute_prompt(  # noqa: F841
                "Test prompt", full_auto=True, log_level="debug", add_dirs=["/path/to/dir"]
            )

            # Verify command options were added
            call_args = mock_popen.call_args[0][0]
            assert "--allow-all-tools" in call_args
            assert "--log-level" in call_args
            assert "debug" in call_args
            assert "--add-dir" in call_args
            assert "/path/to/dir" in call_args

    def test_execute_prompt_error_handling(self):
        """Test error handling in prompt execution."""
        with (
            patch.object(CopilotRuntime, "is_available", return_value=True),
            patch("subprocess.Popen") as mock_popen,
        ):
            # Mock process that fails
            mock_process = Mock()
            mock_process.stdout.readline.side_effect = ["Error occurred\n", ""]
            mock_process.wait.return_value = 1  # Non-zero exit code
            mock_popen.return_value = mock_process

            runtime = CopilotRuntime()

            with pytest.raises(RuntimeError, match="Copilot CLI execution failed"):
                runtime.execute_prompt("Test prompt")

    def test_str_representation(self):
        """Test string representation."""
        with patch.object(CopilotRuntime, "is_available", return_value=True):
            runtime = CopilotRuntime("test-model")
            str_repr = str(runtime)
            assert "CopilotRuntime" in str_repr
            assert "test-model" in str_repr


class TestMcpConfigUtf8RoundTrip:
    """Reading MCP config preserves non-ASCII content (Windows cp1252/cp950 guard)."""

    def test_get_mcp_servers_reads_non_ascii(self, tmp_path):
        import json as _json

        mcp_path = tmp_path / "mcp-config.json"
        servers = {
            "servers": {
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

    def test_get_mcp_servers_reads_mcp_servers_key(self, tmp_path):
        """Test reading MCP config supports the mcpServers key used in .mcp.json."""
        import json as _json

        mcp_path = tmp_path / "mcp.json"
        servers = {
            "mcpServers": {
                "workspace-server": {
                    "command": "node",
                    "args": ["server.js"],
                }
            }
        }
        mcp_path.write_bytes(_json.dumps(servers).encode("utf-8"))

        with patch.object(CopilotRuntime, "is_available", return_value=True):
            runtime = CopilotRuntime()
            with patch.object(CopilotRuntime, "get_mcp_config_path", return_value=mcp_path):
                got = runtime.get_mcp_servers()

        assert "workspace-server" in got

    def test_get_mcp_servers_handles_non_dict(self, tmp_path):
        """Test that get_mcp_servers returns an empty dict if servers is a list or non-dict."""
        import json as _json

        mcp_path = tmp_path / "mcp.json"
        servers = {"mcpServers": ["not", "a", "dict"]}
        mcp_path.write_bytes(_json.dumps(servers).encode("utf-8"))

        with patch.object(CopilotRuntime, "is_available", return_value=True):
            runtime = CopilotRuntime()
            with patch.object(CopilotRuntime, "get_mcp_config_path", return_value=mcp_path):
                got = runtime.get_mcp_servers()

        assert got == {}

    def test_get_mcp_servers_handles_malformed_config_root(self, tmp_path):
        """Test that get_mcp_servers returns an empty dict if the root is not a dict."""
        import json as _json

        mcp_path = tmp_path / "mcp.json"
        mcp_path.write_bytes(_json.dumps(["root", "is", "list"]).encode("utf-8"))

        with patch.object(CopilotRuntime, "is_available", return_value=True):
            runtime = CopilotRuntime()
            with patch.object(CopilotRuntime, "get_mcp_config_path", return_value=mcp_path):
                got = runtime.get_mcp_servers()

        assert got == {}
