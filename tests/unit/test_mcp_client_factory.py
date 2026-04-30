"""Unit tests for MCP client factory and adapters."""

import json  # noqa: F401
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch  # noqa: F401

from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter
from apm_cli.factory import ClientFactory


class TestMCPClientFactory(unittest.TestCase):
    """Test cases for the MCP client factory."""

    def test_create_vscode_client(self):
        """Test creating VSCode client adapter."""
        client = ClientFactory.create_client("vscode")
        self.assertIsInstance(client, VSCodeClientAdapter)

    def test_create_codex_client(self):
        """Test creating Codex CLI client adapter."""
        client = ClientFactory.create_client("codex")
        self.assertIsInstance(client, CodexClientAdapter)

    def test_create_client_case_insensitive(self):
        """Test creating clients with different case."""
        client1 = ClientFactory.create_client("VSCode")
        client3 = ClientFactory.create_client("Codex")

        self.assertIsInstance(client1, VSCodeClientAdapter)
        self.assertIsInstance(client3, CodexClientAdapter)

    def test_create_unsupported_client(self):
        """Test creating unsupported client type raises error."""
        with self.assertRaises(ValueError) as context:
            ClientFactory.create_client("unsupported")

        self.assertIn("Unsupported client type", str(context.exception))

    def test_all_supported_client_types(self):
        """Test that all supported client types can be created."""
        supported_types = ["vscode", "codex", "cursor"]

        for client_type in supported_types:
            with self.subTest(client_type=client_type):
                client = ClientFactory.create_client(client_type)
                self.assertIsNotNone(client)

                # Verify basic interface compliance
                self.assertTrue(hasattr(client, "get_config_path"))
                self.assertTrue(hasattr(client, "update_config"))
                self.assertTrue(hasattr(client, "get_current_config"))
                self.assertTrue(hasattr(client, "configure_mcp_server"))


class TestCodexClientAdapter(unittest.TestCase):
    """Test cases for Codex CLI client adapter."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.temp_dir.name, "config.toml")

        # Create basic TOML config
        with open(self.config_path, "w") as f:
            f.write('model_provider = "github-models"\nmodel = "gpt-4o-mini"\n')

        # Create adapter and patch config path
        self.adapter = CodexClientAdapter()
        self.original_get_config_path = self.adapter.get_config_path
        self.adapter.get_config_path = lambda: self.config_path

    def tearDown(self):
        """Clean up test fixtures."""
        self.adapter.get_config_path = self.original_get_config_path
        self.temp_dir.cleanup()

    def test_get_config_path_default(self):
        """Test project-scope config path for Codex CLI."""
        project_root = Path(self.temp_dir.name) / "workspace"
        adapter = CodexClientAdapter(project_root=project_root)
        expected_path = str(project_root / ".codex" / "config.toml")
        self.assertEqual(adapter.get_config_path(), expected_path)

    def test_get_config_path_user_scope(self):
        """Test user-scope config path for Codex CLI."""
        adapter = CodexClientAdapter(user_scope=True)
        expected_path = str(Path.home() / ".codex" / "config.toml")
        self.assertEqual(adapter.get_config_path(), expected_path)

    def test_get_current_config_existing(self):
        """Test getting existing TOML config."""
        config = self.adapter.get_current_config()

        self.assertEqual(config["model_provider"], "github-models")
        self.assertEqual(config["model"], "gpt-4o-mini")

    def test_get_current_config_invalid_toml_returns_none(self):
        """Invalid existing TOML should not be treated as an empty config."""
        Path(self.config_path).write_text('invalid = "unterminated', encoding="utf-8")

        with patch("apm_cli.adapters.client.codex._rich_warning") as mock_warn:
            config = self.adapter.get_current_config()

        self.assertIsNone(config)
        mock_warn.assert_called_once()

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_does_not_overwrite_invalid_toml(self, mock_find_server):
        """Parse failures should skip writes to avoid destroying existing config."""
        Path(self.config_path).write_text('invalid = "unterminated', encoding="utf-8")
        mock_find_server.return_value = {
            "id": "test-id",
            "name": "test-server",
            "packages": [{"registry_name": "npm", "name": "test-package", "arguments": []}],
            "environment_variables": [],
        }

        original = Path(self.config_path).read_text(encoding="utf-8")
        with patch("apm_cli.adapters.client.codex._rich_warning") as mock_warn:
            result = self.adapter.configure_mcp_server("test-server", "my_server")

        self.assertFalse(result)
        self.assertEqual(Path(self.config_path).read_text(encoding="utf-8"), original)
        self.assertTrue(mock_warn.called)

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_basic(self, mock_find_server):
        """Test basic MCP server configuration for Codex."""
        # Mock registry response
        mock_server_info = {
            "id": "test-id",
            "name": "test-server",
            "package_canonical": "npm",
            "packages": [
                {
                    "registry_name": "npm",
                    "name": "test-package",
                    "version": "1.0.0",
                    "arguments": [],
                }
            ],
            "environment_variables": [],
        }
        mock_find_server.return_value = mock_server_info

        result = self.adapter.configure_mcp_server("test-server", "my_server")

        self.assertTrue(result)
        mock_find_server.assert_called_once_with("test-server")

        # Verify TOML config was updated
        config = self.adapter.get_current_config()
        self.assertIn("mcp_servers", config)
        self.assertIn("my_server", config["mcp_servers"])
        server_config = config["mcp_servers"]["my_server"]
        self.assertEqual(server_config["command"], "npx")

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_remote_rejected(self, mock_find_server):
        """Test that remote servers (SSE type) are rejected by Codex adapter."""
        # Mock registry response for remote-only server
        mock_server_info = {
            "id": "remote-server-id",
            "name": "remote-server",
            "remotes": [{"transport_type": "sse", "url": "https://example.com/mcp"}],
            "packages": [],  # No packages, only remote endpoints
        }
        mock_find_server.return_value = mock_server_info

        # Capture printed output
        with patch("builtins.print") as mock_print:
            result = self.adapter.configure_mcp_server("remote-server")

        # Should return False (rejected)
        self.assertFalse(result)
        mock_find_server.assert_called_once_with("remote-server")

        # Verify warning message was printed
        mock_print.assert_any_call(
            "[!]  Warning: MCP server 'remote-server' is a remote server (SSE type)"
        )
        mock_print.assert_any_call(
            "   Codex CLI only supports local servers with command/args configuration"
        )

        # Verify no config was updated
        config = self.adapter.get_current_config()
        self.assertNotIn("mcp_servers", config)

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_hybrid_accepted(self, mock_find_server):
        """Test that hybrid servers (both remote and packages) are accepted and configured using packages."""
        # Mock registry response for hybrid server
        mock_server_info = {
            "id": "hybrid-server-id",
            "name": "hybrid-server",
            "remotes": [{"transport_type": "sse", "url": "https://example.com/mcp"}],
            "packages": [
                {  # Has both remote and packages - use packages for Codex
                    "registry_name": "npm",
                    "name": "hybrid-package",
                    "version": "1.0.0",
                    "arguments": [],
                }
            ],
            "environment_variables": [],
        }
        mock_find_server.return_value = mock_server_info

        result = self.adapter.configure_mcp_server("hybrid-server", "hybrid")

        # Should succeed because it has packages
        self.assertTrue(result)
        mock_find_server.assert_called_once_with("hybrid-server")

        # Verify TOML config was updated using package info
        config = self.adapter.get_current_config()
        self.assertIn("mcp_servers", config)
        self.assertIn("hybrid", config["mcp_servers"])
        server_config = config["mcp_servers"]["hybrid"]
        self.assertEqual(server_config["command"], "npx")

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_name_extraction(self, mock_find_server):
        """Test server name extraction from URL for Codex."""
        # Mock registry response
        mock_server_info = {
            "id": "test-id",
            "name": "test-server",
            "packages": [{"registry_name": "npm", "name": "test-package", "arguments": []}],
            "environment_variables": [],
        }
        mock_find_server.return_value = mock_server_info

        # Test with org/repo format
        result = self.adapter.configure_mcp_server("microsoft/azure-devops-mcp")

        self.assertTrue(result)

        # Verify config uses extracted name
        config = self.adapter.get_current_config()
        self.assertIn("mcp_servers", config)
        self.assertIn("azure-devops-mcp", config["mcp_servers"])  # Should extract name after slash
        self.assertNotIn(
            "microsoft/azure-devops-mcp", config["mcp_servers"]
        )  # Should NOT use full path

    def test_self_defined_stdio_normalizes_project_placeholders(self):
        """Project-local Codex configs normalize VS Code placeholders to '.'."""
        adapter = CodexClientAdapter(project_root=Path(self.temp_dir.name))
        server_info = {
            "id": "stdio-id",
            "name": "local-filesystem",
            "_raw_stdio": {
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    "${workspaceFolder}",
                    "${projectRoot}",
                ],
                "env": {},
            },
        }

        config = adapter._format_server_config(server_info)

        self.assertEqual(
            config["args"],
            ["-y", "@modelcontextprotocol/server-filesystem", ".", "."],
        )


if __name__ == "__main__":
    unittest.main()
