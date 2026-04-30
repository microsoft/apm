"""Unit tests for the VSCode client adapter."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401

from apm_cli.adapters.client.base import MCPClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter


class TestVSCodeClientAdapter(unittest.TestCase):
    """Test cases for the VSCode client adapter."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.vscode_dir = os.path.join(self.temp_dir, ".vscode")
        os.makedirs(self.vscode_dir, exist_ok=True)
        self.temp_path = os.path.join(self.vscode_dir, "mcp.json")
        with open(self.temp_path, "w") as f:
            json.dump({"servers": {}}, f)

        # Create mock clients
        self.mock_registry_patcher = patch("apm_cli.adapters.client.vscode.SimpleRegistryClient")
        self.mock_registry_class = self.mock_registry_patcher.start()
        self.mock_registry = MagicMock()
        self.mock_registry_class.return_value = self.mock_registry

        self.mock_integration_patcher = patch("apm_cli.adapters.client.vscode.RegistryIntegration")
        self.mock_integration_class = self.mock_integration_patcher.start()
        self.mock_integration = MagicMock()
        self.mock_integration_class.return_value = self.mock_integration

        # Mock server details
        self.server_info = {
            "id": "12345",
            "name": "fetch",
            "description": "Fetch MCP server",
            "packages": [
                {
                    "name": "@mcp/fetch",
                    "version": "1.0.0",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                }
            ],
        }

        # Configure the mocks
        self.mock_registry.get_server_info.return_value = self.server_info
        self.mock_registry.get_server_by_name.return_value = self.server_info
        self.mock_registry.find_server_by_reference.return_value = self.server_info

    def tearDown(self):
        """Tear down test fixtures."""
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_get_current_config(self, mock_get_path):
        """Test getting the current configuration."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        config = adapter.get_current_config()
        self.assertEqual(config, {"servers": {}})

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_update_config(self, mock_get_path):
        """Test updating the configuration."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        new_config = {
            "servers": {
                "test-server": {
                    "type": "stdio",
                    "command": "uvx",
                    "args": ["mcp-server-test"],
                }
            }
        }

        result = adapter.update_config(new_config)

        with open(self.temp_path) as f:
            updated_config = json.load(f)

        self.assertEqual(updated_config, new_config)
        self.assertTrue(result)

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_update_config_nonexistent_file(self, mock_get_path):
        """Test updating configuration when file doesn't exist."""
        nonexistent_path = os.path.join(self.vscode_dir, "nonexistent.json")
        mock_get_path.return_value = nonexistent_path
        adapter = VSCodeClientAdapter()

        new_config = {
            "servers": {
                "test-server": {
                    "type": "stdio",
                    "command": "uvx",
                    "args": ["mcp-server-test"],
                }
            }
        }

        result = adapter.update_config(new_config)

        with open(nonexistent_path) as f:
            updated_config = json.load(f)

        self.assertEqual(updated_config, new_config)
        self.assertTrue(result)

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_configure_mcp_server(self, mock_get_path):
        """Test configuring an MCP server."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        result = adapter.configure_mcp_server(server_url="fetch", server_name="fetch")

        with open(self.temp_path) as f:
            updated_config = json.load(f)

        self.assertTrue(result)
        self.assertIn("servers", updated_config)
        self.assertIn("fetch", updated_config["servers"])

        # Verify the registry client was called
        self.mock_registry.find_server_by_reference.assert_called_once_with("fetch")

        # Verify the server configuration
        self.assertEqual(updated_config["servers"]["fetch"]["type"], "stdio")
        self.assertEqual(updated_config["servers"]["fetch"]["command"], "npx")
        self.assertEqual(updated_config["servers"]["fetch"]["args"], ["-y", "@mcp/fetch"])

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_configure_mcp_server_update_existing(self, mock_get_path):
        """Test updating an existing MCP server."""
        # Create a config with an existing server
        existing_config = {
            "servers": {
                "fetch": {
                    "type": "stdio",
                    "command": "docker",
                    "args": ["run", "-i", "--rm", "mcp/fetch"],
                }
            }
        }

        with open(self.temp_path, "w") as f:
            json.dump(existing_config, f)

        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        result = adapter.configure_mcp_server(server_url="fetch", server_name="fetch")

        with open(self.temp_path) as f:
            updated_config = json.load(f)

        self.assertTrue(result)
        self.assertIn("fetch", updated_config["servers"])

        # Verify the registry client was called
        self.mock_registry.find_server_by_reference.assert_called_once_with("fetch")

        # Verify the server configuration
        self.assertEqual(updated_config["servers"]["fetch"]["type"], "stdio")
        self.assertEqual(updated_config["servers"]["fetch"]["command"], "npx")
        self.assertEqual(updated_config["servers"]["fetch"]["args"], ["-y", "@mcp/fetch"])

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_configure_mcp_server_empty_url(self, mock_get_path):
        """Test configuring an MCP server with empty URL."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        result = adapter.configure_mcp_server(server_url="", server_name="Example Server")

        self.assertFalse(result)

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_configure_mcp_server_registry_error(self, mock_get_path):
        """Test error behavior when registry doesn't have server details."""
        # Configure the mock to return None when server is not found
        self.mock_registry.find_server_by_reference.return_value = None

        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        # Test that ValueError is raised when server details can't be retrieved
        with self.assertRaises(ValueError) as context:
            adapter.configure_mcp_server(server_url="unknown-server", server_name="unknown-server")

        self.assertIn(
            "Failed to retrieve server details for 'unknown-server'. Server not found in registry.",
            str(context.exception),
        )

    @patch("os.getcwd")
    def test_get_config_path_repository(self, mock_getcwd):
        """Test getting the config path in the repository."""
        mock_getcwd.return_value = self.temp_dir

        adapter = VSCodeClientAdapter()
        path = adapter.get_config_path()

        # Create Path objects for comparison to handle platform differences
        actual_path = Path(path)
        expected_path = Path(self.temp_dir) / ".vscode" / "mcp.json"

        # Compare parts of the path to avoid string formatting issues
        self.assertEqual(actual_path.parent, expected_path.parent)
        self.assertEqual(actual_path.name, expected_path.name)

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_http_remote(self, mock_get_path):
        """Test _format_server_config handles http transport in remotes."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "my-http-server",
            "remotes": [{"transport_type": "http", "url": "https://example.com/mcp"}],
        }
        config, inputs = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")
        self.assertEqual(config["url"], "https://example.com/mcp")
        self.assertEqual(config["headers"], {})
        self.assertEqual(inputs, [])

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_streamable_http_remote(self, mock_get_path):
        """Test _format_server_config handles streamable-http transport in remotes."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "streamable-server",
            "remotes": [
                {
                    "transport_type": "streamable-http",
                    "url": "https://stream.example.com",
                }
            ],
        }
        config, inputs = adapter._format_server_config(server_info)  # noqa: RUF059

        self.assertEqual(config["type"], "streamable-http")
        self.assertEqual(config["url"], "https://stream.example.com")

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_remote_with_list_headers(self, mock_get_path):
        """Test _format_server_config normalizes header list to dict."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "header-server",
            "remotes": [
                {
                    "transport_type": "http",
                    "url": "https://example.com",
                    "headers": [
                        {"name": "Authorization", "value": "Bearer token123"},
                        {"name": "X-Custom", "value": "val"},
                    ],
                }
            ],
        }
        config, inputs = adapter._format_server_config(server_info)  # noqa: RUF059

        self.assertEqual(config["type"], "http")
        self.assertEqual(
            config["headers"],
            {
                "Authorization": "Bearer token123",
                "X-Custom": "val",
            },
        )

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_configure_self_defined_http_via_cache(self, mock_get_path):
        """Test configuring a self-defined HTTP server through server_info_cache."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        # Synthetic server_info as built by _build_self_defined_server_info
        cache = {
            "my-private-srv": {
                "name": "my-private-srv",
                "remotes": [{"transport_type": "http", "url": "http://localhost:8787/"}],
            }
        }

        result = adapter.configure_mcp_server(
            server_url="my-private-srv",
            server_name="my-private-srv",
            server_info_cache=cache,
        )

        self.assertTrue(result)
        with open(self.temp_path) as f:
            config = json.load(f)

        self.assertIn("my-private-srv", config["servers"])
        self.assertEqual(config["servers"]["my-private-srv"]["type"], "http")
        self.assertEqual(config["servers"]["my-private-srv"]["url"], "http://localhost:8787/")

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_remote_missing_transport_type(self, mock_get_path):
        """Remote with no transport_type defaults to http (issue #654)."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "atlassian-mcp-server",
            "remotes": [{"url": "https://mcp.atlassian.com/v1/mcp"}],
        }
        config, inputs = adapter._format_server_config(server_info)  # noqa: RUF059

        self.assertEqual(config["type"], "http")
        self.assertEqual(config["url"], "https://mcp.atlassian.com/v1/mcp")
        self.assertEqual(config["headers"], {})

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_remote_empty_transport_type(self, mock_get_path):
        """Remote with empty transport_type defaults to http."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "remote-srv",
            "remotes": [{"transport_type": "", "url": "https://example.com/mcp"}],
        }
        config, inputs = adapter._format_server_config(server_info)  # noqa: RUF059

        self.assertEqual(config["type"], "http")
        self.assertEqual(config["url"], "https://example.com/mcp")

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_remote_none_transport_type(self, mock_get_path):
        """Remote with transport_type=None defaults to http."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "remote-srv",
            "remotes": [{"transport_type": None, "url": "https://example.com/mcp"}],
        }
        config, inputs = adapter._format_server_config(server_info)  # noqa: RUF059

        self.assertEqual(config["type"], "http")

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_remote_whitespace_transport_type(self, mock_get_path):
        """Remote with whitespace-only transport_type defaults to http."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "remote-srv",
            "remotes": [{"transport_type": "  ", "url": "https://example.com/mcp"}],
        }
        config, inputs = adapter._format_server_config(server_info)  # noqa: RUF059

        self.assertEqual(config["type"], "http")

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_remote_unsupported_transport_raises(self, mock_get_path):
        """Remote with an unrecognized transport_type raises ValueError."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "future-srv",
            "remotes": [{"transport_type": "grpc", "url": "https://example.com/mcp"}],
        }
        with self.assertRaises(ValueError) as ctx:
            adapter._format_server_config(server_info)

        self.assertIn("Unsupported remote transport", str(ctx.exception))
        self.assertIn("grpc", str(ctx.exception))

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_remote_skips_entries_without_url(self, mock_get_path):
        """Remotes with empty URLs are skipped; first with a valid URL is used."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "multi-remote",
            "remotes": [
                {"transport_type": "http", "url": ""},
                {"transport_type": "sse", "url": "https://good.example.com/sse"},
            ],
        }
        config, inputs = adapter._format_server_config(server_info)  # noqa: RUF059

        self.assertEqual(config["type"], "sse")
        self.assertEqual(config["url"], "https://good.example.com/sse")

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_format_server_config_remote_default_http_preserves_headers(self, mock_get_path):
        """Defaulting to http still normalizes headers and extracts input vars."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "name": "header-srv",
            "remotes": [
                {
                    "url": "https://example.com/mcp",
                    "headers": [
                        {"name": "Authorization", "value": "${input:auth-token}"},
                    ],
                }
            ],
        }
        config, inputs = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")
        self.assertEqual(config["headers"], {"Authorization": "${input:auth-token}"})
        self.assertTrue(len(inputs) > 0)
        self.assertEqual(inputs[0]["id"], "auth-token")


class TestVSCodeSelectBestPackage(unittest.TestCase):
    """Test cases for _select_best_package logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_registry_patcher = patch("apm_cli.adapters.client.vscode.SimpleRegistryClient")
        self.mock_registry_patcher.start()
        self.mock_integration_patcher = patch("apm_cli.adapters.client.vscode.RegistryIntegration")
        self.mock_integration_patcher.start()
        self.adapter = VSCodeClientAdapter()

    def tearDown(self):
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()

    def test_prefers_npm_over_nuget(self):
        """npm should be selected over nuget when both are available."""
        packages = [
            {"name": "Azure.Mcp", "registry_name": "nuget", "runtime_hint": "dotnet"},
            {"name": "@azure/mcp", "registry_name": "npm", "runtime_hint": "npx"},
            {"name": "msmcp-azure", "registry_name": "pypi", "runtime_hint": "uvx"},
        ]
        result = self.adapter._select_best_package(packages)
        self.assertEqual(result["registry_name"], "npm")

    def test_prefers_pypi_when_no_npm(self):
        """pypi should be selected when npm is not available."""
        packages = [
            {"name": "Azure.Mcp", "registry_name": "nuget", "runtime_hint": "dotnet"},
            {"name": "msmcp-azure", "registry_name": "pypi", "runtime_hint": "uvx"},
        ]
        result = self.adapter._select_best_package(packages)
        self.assertEqual(result["registry_name"], "pypi")

    def test_falls_back_to_runtime_hint(self):
        """Falls back to any package with runtime_hint when no priority match."""
        packages = [
            {"name": "Azure.Mcp", "registry_name": "nuget", "runtime_hint": "dotnet"},
            {
                "name": "azure-mcp-linux-x64",
                "registry_name": "mcpb",
                "runtime_hint": "",
            },
        ]
        result = self.adapter._select_best_package(packages)
        self.assertEqual(result["name"], "Azure.Mcp")

    def test_returns_first_if_no_match(self):
        """Returns first package if no priority or runtime_hint match."""
        packages = [
            {"name": "azure-mcp-linux-x64", "registry_name": "mcpb"},
        ]
        result = self.adapter._select_best_package(packages)
        self.assertEqual(result["name"], "azure-mcp-linux-x64")

    def test_returns_none_for_empty_list(self):
        self.assertIsNone(self.adapter._select_best_package([]))


class TestVSCodeStdioRegistryPackages(unittest.TestCase):
    """Test that VS Code adapter correctly handles stdio-only registry servers."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.vscode_dir = os.path.join(self.temp_dir, ".vscode")
        os.makedirs(self.vscode_dir, exist_ok=True)
        self.temp_path = os.path.join(self.vscode_dir, "mcp.json")
        with open(self.temp_path, "w") as f:
            json.dump({"servers": {}}, f)

        self.mock_registry_patcher = patch("apm_cli.adapters.client.vscode.SimpleRegistryClient")
        self.mock_registry_class = self.mock_registry_patcher.start()
        self.mock_registry = MagicMock()
        self.mock_registry_class.return_value = self.mock_registry

        self.mock_integration_patcher = patch("apm_cli.adapters.client.vscode.RegistryIntegration")
        self.mock_integration_patcher.start()

    def tearDown(self):
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_stdio_npm_selected_over_nuget(self, mock_get_path):
        """Reproduces the reported bug: server has nuget+npm+pypi+mcpb packages, no remotes."""
        mock_get_path.return_value = self.temp_path

        # Simulates com.microsoft/azure registry metadata
        server_info = {
            "id": "azure-mcp-id",
            "name": "azure",
            "description": "Azure MCP server",
            "packages": [
                {
                    "name": "Azure.Mcp",
                    "version": "2.0.0-beta.24",
                    "registry_name": "nuget",
                    "runtime_hint": "dotnet",
                    "runtime_arguments": [
                        {"is_required": True, "value_hint": "server"},
                        {"is_required": True, "value_hint": "start"},
                    ],
                },
                {
                    "name": "@azure/mcp",
                    "version": "2.0.0-beta.24",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                    "runtime_arguments": [
                        {"is_required": True, "value_hint": "server"},
                        {"is_required": True, "value_hint": "start"},
                    ],
                },
                {
                    "name": "msmcp-azure",
                    "version": "2.0.0-beta.24",
                    "registry_name": "pypi",
                    "runtime_hint": "uvx",
                    "runtime_arguments": [
                        {"is_required": True, "value_hint": "server"},
                        {"is_required": True, "value_hint": "start"},
                    ],
                },
                {
                    "name": "azure-mcp-linux-x64",
                    "version": "2.0.0-beta.24",
                    "registry_name": "mcpb",
                    "runtime_hint": "",
                    "runtime_arguments": [],
                },
            ],
            # No remotes key — server only provides stdio packages
        }

        self.mock_registry.find_server_by_reference.return_value = server_info
        adapter = VSCodeClientAdapter()

        result = adapter.configure_mcp_server(
            server_url="com.microsoft/azure",
            server_name="azure",
        )

        self.assertTrue(result)
        with open(self.temp_path) as f:
            config = json.load(f)

        server = config["servers"]["azure"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "npx")
        self.assertEqual(server["args"], ["-y", "@azure/mcp", "server", "start"])

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_generic_runtime_hint_fallback(self, mock_get_path):
        """Server with only nuget package should use generic fallback via runtime_hint."""
        mock_get_path.return_value = self.temp_path

        server_info = {
            "id": "nuget-only-id",
            "name": "nuget-server",
            "packages": [
                {
                    "name": "MyServer.Mcp",
                    "registry_name": "nuget",
                    "runtime_hint": "dotnet",
                    "runtime_arguments": [
                        {"is_required": True, "value_hint": "run"},
                        {"is_required": True, "value_hint": "--project"},
                        {"is_required": True, "value_hint": "MyServer.Mcp"},
                    ],
                }
            ],
        }
        self.mock_registry.find_server_by_reference.return_value = server_info
        adapter = VSCodeClientAdapter()

        result = adapter.configure_mcp_server(
            server_url="nuget-server",
            server_name="nuget-server",
        )

        self.assertTrue(result)
        with open(self.temp_path) as f:
            config = json.load(f)

        server = config["servers"]["nuget-server"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "dotnet")
        self.assertEqual(server["args"], ["run", "--project", "MyServer.Mcp"])

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_error_message_when_packages_exist_but_none_supported(self, mock_get_path):
        """Error message should list available registries, not claim 'no package information'."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        server_info = {
            "id": "binary-only-id",
            "name": "binary-only",
            "packages": [
                {"name": "binary-linux-x64", "registry_name": "mcpb"},
                {"name": "binary-linux-arm64", "registry_name": "mcpb"},
            ],
        }

        with self.assertRaises(ValueError) as ctx:
            adapter._format_server_config(server_info)

        self.assertIn("No supported transport for VS Code runtime", str(ctx.exception))
        self.assertIn("mcpb", str(ctx.exception))
        self.assertNotIn("no package information", str(ctx.exception))

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_error_message_when_no_packages_and_no_remotes(self, mock_get_path):
        """Truly empty server should still report 'no package information'."""
        mock_get_path.return_value = self.temp_path
        adapter = VSCodeClientAdapter()

        with self.assertRaises(ValueError) as ctx:
            adapter._format_server_config({"name": "empty-server"})

        self.assertIn("no package information", str(ctx.exception))


class TestVSCodeInferRegistryName(unittest.TestCase):
    """Test _infer_registry_name with various package metadata patterns."""

    def setUp(self):
        self.mock_registry_patcher = patch("apm_cli.adapters.client.vscode.SimpleRegistryClient")
        self.mock_registry_patcher.start()
        self.mock_integration_patcher = patch("apm_cli.adapters.client.vscode.RegistryIntegration")
        self.mock_integration_patcher.start()

    def tearDown(self):
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()

    def test_explicit_registry_name(self):
        self.assertEqual(
            VSCodeClientAdapter._infer_registry_name({"name": "pkg", "registry_name": "npm"}),
            "npm",
        )

    def test_empty_registry_name_scoped_npm(self):
        self.assertEqual(
            VSCodeClientAdapter._infer_registry_name({"name": "@azure/mcp", "registry_name": ""}),
            "npm",
        )

    def test_empty_registry_name_runtime_hint_npx(self):
        self.assertEqual(
            VSCodeClientAdapter._infer_registry_name(
                {"name": "some-pkg", "registry_name": "", "runtime_hint": "npx"}
            ),
            "npm",
        )

    def test_empty_registry_name_runtime_hint_uvx(self):
        self.assertEqual(
            VSCodeClientAdapter._infer_registry_name(
                {"name": "some-pkg", "registry_name": "", "runtime_hint": "uvx"}
            ),
            "pypi",
        )

    def test_empty_registry_name_docker_image(self):
        self.assertEqual(
            VSCodeClientAdapter._infer_registry_name(
                {"name": "ghcr.io/org/img", "registry_name": ""}
            ),
            "docker",
        )

    def test_empty_registry_name_nuget_pascal_case(self):
        self.assertEqual(
            VSCodeClientAdapter._infer_registry_name({"name": "Azure.Mcp", "registry_name": ""}),
            "nuget",
        )

    def test_empty_registry_name_mcpb_url(self):
        self.assertEqual(
            VSCodeClientAdapter._infer_registry_name(
                {"name": "https://example.com/bin.mcpb", "registry_name": ""}
            ),
            "mcpb",
        )

    def test_unknown_returns_empty(self):
        self.assertEqual(
            VSCodeClientAdapter._infer_registry_name({"name": "unknown-pkg", "registry_name": ""}),
            "",
        )

    def test_none_package(self):
        self.assertEqual(VSCodeClientAdapter._infer_registry_name(None), "")


class TestVSCodeExtractPackageArgs(unittest.TestCase):
    """Test _extract_package_args with both API formats."""

    def setUp(self):
        self.mock_registry_patcher = patch("apm_cli.adapters.client.vscode.SimpleRegistryClient")
        self.mock_registry_patcher.start()
        self.mock_integration_patcher = patch("apm_cli.adapters.client.vscode.RegistryIntegration")
        self.mock_integration_patcher.start()

    def tearDown(self):
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()

    def test_package_arguments_api_format(self):
        pkg = {
            "name": "@azure/mcp",
            "package_arguments": [
                {"type": "positional", "value": "server"},
                {"type": "positional", "value": "start"},
            ],
        }
        self.assertEqual(VSCodeClientAdapter._extract_package_args(pkg), ["server", "start"])

    def test_runtime_arguments_legacy_format(self):
        pkg = {
            "name": "@azure/mcp",
            "runtime_arguments": [
                {"is_required": True, "value_hint": "server"},
                {"is_required": True, "value_hint": "start"},
            ],
        }
        self.assertEqual(VSCodeClientAdapter._extract_package_args(pkg), ["server", "start"])

    def test_prefers_package_arguments_over_runtime(self):
        pkg = {
            "name": "pkg",
            "package_arguments": [{"type": "positional", "value": "run"}],
            "runtime_arguments": [{"is_required": True, "value_hint": "old"}],
        }
        self.assertEqual(VSCodeClientAdapter._extract_package_args(pkg), ["run"])

    def test_empty_returns_empty(self):
        self.assertEqual(VSCodeClientAdapter._extract_package_args({}), [])
        self.assertEqual(VSCodeClientAdapter._extract_package_args(None), [])


class TestVSCodeRealApiFormat(unittest.TestCase):
    """Test with the actual MCP registry API response format (empty registry_name, package_arguments)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.vscode_dir = os.path.join(self.temp_dir, ".vscode")
        os.makedirs(self.vscode_dir, exist_ok=True)
        self.temp_path = os.path.join(self.vscode_dir, "mcp.json")
        with open(self.temp_path, "w") as f:
            json.dump({"servers": {}}, f)

        self.mock_registry_patcher = patch("apm_cli.adapters.client.vscode.SimpleRegistryClient")
        self.mock_registry_class = self.mock_registry_patcher.start()
        self.mock_registry = MagicMock()
        self.mock_registry_class.return_value = self.mock_registry

        self.mock_integration_patcher = patch("apm_cli.adapters.client.vscode.RegistryIntegration")
        self.mock_integration_patcher.start()

    def tearDown(self):
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_azure_mcp_real_api_format(self, mock_get_path):
        """Test with the actual registry API response for Azure MCP (empty registry_name)."""
        mock_get_path.return_value = self.temp_path

        # Matches the real API response: registry_name is empty, uses package_arguments
        server_info = {
            "id": "d3965c5a53be4f8bab7921b9d0511419",
            "name": "azure",
            "description": "Azure MCP Server",
            "packages": [
                {
                    "name": "@azure/mcp",
                    "version": "2.0.0-beta.24",
                    "registry_name": "",
                    "package_arguments": [
                        {"type": "positional", "value": "server"},
                        {"type": "positional", "value": "start"},
                    ],
                },
                {
                    "name": "msmcp-azure",
                    "version": "2.0.0-beta.24",
                    "registry_name": "",
                    "package_arguments": [
                        {"type": "positional", "value": "server"},
                        {"type": "positional", "value": "start"},
                    ],
                },
                {
                    "name": "Azure.Mcp",
                    "version": "2.0.0-beta.24",
                    "registry_name": "",
                    "package_arguments": [],
                },
            ],
        }

        self.mock_registry.find_server_by_reference.return_value = server_info
        adapter = VSCodeClientAdapter()

        result = adapter.configure_mcp_server(
            server_url="com.microsoft/azure",
            server_name="azure",
        )

        self.assertTrue(result)
        with open(self.temp_path) as f:
            config = json.load(f)

        server = config["servers"]["azure"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "npx")
        # npm inferred from @azure/mcp scoped name, -y flag added, package name prepended
        self.assertEqual(server["args"], ["-y", "@azure/mcp", "server", "start"])

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_pypi_inferred_from_name_pattern(self, mock_get_path):
        """PyPI package selected when only non-scoped simple names available."""
        mock_get_path.return_value = self.temp_path

        server_info = {
            "id": "abc123",
            "name": "my-pypi-server",
            "packages": [
                {
                    "name": "my-mcp-server",
                    "version": "1.0.0",
                    "registry_name": "",
                    "runtime_hint": "uvx",
                    "package_arguments": [],
                },
            ],
        }

        self.mock_registry.find_server_by_reference.return_value = server_info
        adapter = VSCodeClientAdapter()

        result = adapter.configure_mcp_server(
            server_url="my-pypi-server",
            server_name="my-pypi-server",
        )

        self.assertTrue(result)
        with open(self.temp_path) as f:
            config = json.load(f)

        server = config["servers"]["my-pypi-server"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "uvx")
        self.assertEqual(server["args"], ["my-mcp-server"])


class TestExtractInputVariables(unittest.TestCase):
    """Tests for ${input:...} variable extraction in self-defined MCP servers."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.vscode_dir = os.path.join(self.temp_dir, ".vscode")
        os.makedirs(self.vscode_dir, exist_ok=True)
        self.temp_path = os.path.join(self.vscode_dir, "mcp.json")
        with open(self.temp_path, "w") as f:
            json.dump({"servers": {}, "inputs": []}, f)

        self.mock_registry_patcher = patch("apm_cli.adapters.client.vscode.SimpleRegistryClient")
        self.mock_registry_class = self.mock_registry_patcher.start()
        self.mock_registry = MagicMock()
        self.mock_registry_class.return_value = self.mock_registry

        self.mock_integration_patcher = patch("apm_cli.adapters.client.vscode.RegistryIntegration")
        self.mock_integration_class = self.mock_integration_patcher.start()
        self.mock_integration = MagicMock()
        self.mock_integration_class.return_value = self.mock_integration

    def tearDown(self):
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_extract_single_input_variable(self):
        adapter = VSCodeClientAdapter()
        result = adapter._extract_input_variables(
            {"Authorization": "Bearer ${input:my-token}"}, "my-server"
        )
        assert len(result) == 1
        assert result[0]["id"] == "my-token"
        assert result[0]["type"] == "promptString"
        assert result[0]["password"] is True
        assert "my-server" in result[0]["description"]

    def test_extract_multiple_input_variables(self):
        adapter = VSCodeClientAdapter()
        result = adapter._extract_input_variables(
            {
                "Authorization": "Bearer ${input:my-token}",
                "X-Project": "${input:my-project}",
            },
            "my-server",
        )
        ids = {v["id"] for v in result}
        assert ids == {"my-token", "my-project"}

    def test_dedup_same_variable(self):
        adapter = VSCodeClientAdapter()
        result = adapter._extract_input_variables(
            {
                "Authorization": "Bearer ${input:shared-token}",
                "X-Alt-Auth": "Token ${input:shared-token}",
            },
            "my-server",
        )
        assert len(result) == 1
        assert result[0]["id"] == "shared-token"

    def test_no_input_variables(self):
        adapter = VSCodeClientAdapter()
        result = adapter._extract_input_variables({"Content-Type": "application/json"}, "my-server")
        assert result == []

    def test_empty_mapping(self):
        adapter = VSCodeClientAdapter()
        assert adapter._extract_input_variables({}, "s") == []
        assert adapter._extract_input_variables(None, "s") == []

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_self_defined_http_headers_generate_inputs(self, mock_get_path):
        """End-to-end: self-defined HTTP server with ${input:} in headers."""
        mock_get_path.return_value = self.temp_path

        server_info = {
            "name": "my-server",
            "remotes": [
                {
                    "transport_type": "http",
                    "url": "https://my-server.example.com/mcp/",
                    "headers": [
                        {
                            "name": "Authorization",
                            "value": "Bearer ${input:my-server-token}",
                        },
                        {"name": "X-Project", "value": "${input:my-server-project}"},
                    ],
                }
            ],
        }
        self.mock_registry.find_server_by_reference.return_value = server_info

        adapter = VSCodeClientAdapter()
        result = adapter.configure_mcp_server(server_url="my-server", server_name="my-server")

        assert result is True
        with open(self.temp_path) as f:
            config = json.load(f)

        inputs = config["inputs"]
        input_ids = {v["id"] for v in inputs}
        assert "my-server-token" in input_ids
        assert "my-server-project" in input_ids
        for inp in inputs:
            assert inp["type"] == "promptString"
            assert inp["password"] is True

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_self_defined_stdio_env_generates_inputs(self, mock_get_path):
        """End-to-end: self-defined stdio server with ${input:} in env."""
        mock_get_path.return_value = self.temp_path

        server_info = {
            "name": "my-cli",
            "_raw_stdio": {
                "command": "my-cli",
                "args": ["serve"],
                "env": {"API_KEY": "${input:my-cli-api-key}"},
            },
        }
        self.mock_registry.find_server_by_reference.return_value = server_info

        adapter = VSCodeClientAdapter()
        result = adapter.configure_mcp_server(server_url="my-cli", server_name="my-cli")

        assert result is True
        with open(self.temp_path) as f:
            config = json.load(f)

        inputs = config["inputs"]
        assert len(inputs) == 1
        assert inputs[0]["id"] == "my-cli-api-key"

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_input_variables_dedup_across_servers(self, mock_get_path):
        """Input variables already present in config are not duplicated."""
        mock_get_path.return_value = self.temp_path

        # Pre-populate with an existing input
        with open(self.temp_path, "w") as f:
            json.dump(
                {
                    "servers": {},
                    "inputs": [
                        {
                            "type": "promptString",
                            "id": "my-server-token",
                            "description": "existing",
                            "password": True,
                        }
                    ],
                },
                f,
            )

        server_info = {
            "name": "my-server",
            "remotes": [
                {
                    "transport_type": "http",
                    "url": "https://example.com/mcp/",
                    "headers": [
                        {
                            "name": "Authorization",
                            "value": "Bearer ${input:my-server-token}",
                        },
                    ],
                }
            ],
        }
        self.mock_registry.find_server_by_reference.return_value = server_info

        adapter = VSCodeClientAdapter()
        adapter.configure_mcp_server(server_url="my-server", server_name="my-server")

        with open(self.temp_path) as f:
            config = json.load(f)

        token_entries = [i for i in config["inputs"] if i["id"] == "my-server-token"]
        assert len(token_entries) == 1


class TestWarnInputVariables(unittest.TestCase):
    """Tests for _warn_input_variables on adapters that don't support input prompts."""

    def test_warning_emitted_for_input_reference(
        self,
    ):
        mapping = {"Authorization": "Bearer ${input:my-token}"}
        with patch("builtins.print") as mock_print:
            MCPClientAdapter._warn_input_variables(mapping, "my-server", "Copilot CLI")
        mock_print.assert_called_once()
        msg = mock_print.call_args[0][0]
        assert "my-token" in msg
        assert "Copilot CLI" in msg

    def test_no_warning_for_plain_values(self):
        mapping = {"Content-Type": "application/json"}
        with patch("builtins.print") as mock_print:
            MCPClientAdapter._warn_input_variables(mapping, "s", "Codex CLI")
        mock_print.assert_not_called()

    def test_no_warning_for_empty_mapping(self):
        with patch("builtins.print") as mock_print:
            MCPClientAdapter._warn_input_variables({}, "s", "Codex CLI")
            MCPClientAdapter._warn_input_variables(None, "s", "Codex CLI")
        mock_print.assert_not_called()


if __name__ == "__main__":
    unittest.main()
