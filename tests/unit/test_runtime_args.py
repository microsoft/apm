"""Test handling of runtime arguments in MCP server configuration."""

import unittest
from unittest import mock  # noqa: F401

from apm_cli.adapters.client.vscode import VSCodeClientAdapter


class TestRuntimeArguments(unittest.TestCase):
    """Test cases for the handling of runtime arguments in MCP server configuration."""

    def setUp(self):
        """Set up test fixtures."""
        self.adapter = VSCodeClientAdapter()

    def test_npm_runtime_args_handling(self):
        """Test that npm runtime arguments are correctly added to the args list."""
        # Mock server info with runtime arguments for an npm package
        server_info = {
            "name": "test-server",
            "packages": [
                {
                    "name": "test-package",
                    "registry_name": "npm",
                    "runtime_hint": "npx",
                    "runtime_arguments": [
                        {
                            "is_required": True,
                            "value": "test-package",
                            "value_hint": "test-package",
                        },
                        {
                            "is_required": True,
                            "value": "<API_KEY>",
                            "value_hint": "<YOUR_API_KEY>",
                            "description": "API Key for authentication",
                        },
                        {
                            "is_required": True,
                            "value": "<APP_KEY>",
                            "value_hint": "<YOUR_APP_KEY>",
                            "description": "App Key for authorization",
                        },
                    ],
                }
            ],
        }

        # Format server config
        server_config, _ = self.adapter._format_server_config(server_info)

        # Validate the args array includes all required runtime arguments
        self.assertEqual(server_config["type"], "stdio")
        self.assertEqual(server_config["command"], "npx")
        self.assertEqual(len(server_config["args"]), 4)
        self.assertEqual(server_config["args"][0], "-y")
        self.assertEqual(server_config["args"][1], "test-package")
        self.assertEqual(server_config["args"][2], "<YOUR_API_KEY>")
        self.assertEqual(server_config["args"][3], "<YOUR_APP_KEY>")

    def test_datadog_mcp_server_args(self):
        """Test that Datadog MCP Server arguments are correctly processed."""
        # Mock server info based on the Datadog MCP server example
        server_info = {
            "name": "io.github.geli2001/datadog-mcp-server",
            "description": "MCP server interacts with the official Datadog API",
            "packages": [
                {
                    "registry_name": "npm",
                    "name": "datadog-mcp-server",
                    "version": "1.0.8",
                    "runtime_hint": "npx",
                    "runtime_arguments": [
                        {
                            "is_required": True,
                            "format": "string",
                            "value": "datadog-mcp-server",
                            "default": "datadog-mcp-server",
                            "type": "positional",
                            "value_hint": "datadog-mcp-server",
                        },
                        {
                            "description": "Datadog API Key value",
                            "is_required": True,
                            "format": "string",
                            "value": "<YOUR_API_KEY>",
                            "default": "<YOUR_API_KEY>",
                            "type": "positional",
                            "value_hint": "<YOUR_API_KEY>",
                        },
                        {
                            "description": "Datadog Application Key value",
                            "is_required": True,
                            "format": "string",
                            "value": "<YOUR_APP_KEY>",
                            "default": "<YOUR_APP_KEY>",
                            "type": "positional",
                            "value_hint": "<YOUR_APP_KEY>",
                        },
                        {
                            "description": "Datadog Site value (e.g. us5.datadoghq.com)",
                            "is_required": True,
                            "format": "string",
                            "value": "<YOUR_DD_SITE>(e.g us5.datadoghq.com)",
                            "default": "<YOUR_DD_SITE>(e.g us5.datadoghq.com)",
                            "type": "positional",
                            "value_hint": "<YOUR_DD_SITE>(e.g us5.datadoghq.com)",
                        },
                    ],
                }
            ],
        }

        # Format server config
        server_config, _ = self.adapter._format_server_config(server_info)

        # Validate the args array includes all required runtime arguments for Datadog MCP server
        self.assertEqual(server_config["command"], "npx")
        self.assertEqual(len(server_config["args"]), 5)
        self.assertEqual(server_config["args"][0], "-y")
        self.assertEqual(server_config["args"][1], "datadog-mcp-server")
        self.assertEqual(server_config["args"][2], "<YOUR_API_KEY>")
        self.assertEqual(server_config["args"][3], "<YOUR_APP_KEY>")
        self.assertEqual(server_config["args"][4], "<YOUR_DD_SITE>(e.g us5.datadoghq.com)")

    def test_docker_runtime_args_handling(self):
        """Test that docker runtime arguments are correctly added to the args list."""
        # Mock server info with runtime arguments for a docker package
        server_info = {
            "name": "test-server",
            "packages": [
                {
                    "name": "test-image",
                    "registry_name": "docker",
                    "runtime_hint": "docker",
                    "runtime_arguments": [
                        {"is_required": True, "value": "test-image", "value_hint": "test-image"},
                        {
                            "is_required": True,
                            "value": "<PORT>",
                            "value_hint": "8080",
                            "description": "Port to expose",
                        },
                    ],
                }
            ],
        }

        # Format server config
        server_config, _ = self.adapter._format_server_config(server_info)

        # Validate the args array includes all required runtime arguments
        self.assertEqual(server_config["type"], "stdio")
        self.assertEqual(server_config["command"], "docker")
        self.assertEqual(
            len(server_config["args"]), 2
        )  # All arguments should come directly from runtime_arguments
        self.assertEqual(server_config["args"][0], "test-image")
        self.assertEqual(server_config["args"][1], "8080")

    def test_python_runtime_args_handling(self):
        """Test that python runtime arguments are correctly added to the args list."""
        # Mock server info with runtime arguments for a python package
        server_info = {
            "name": "test-server",
            "packages": [
                {
                    "name": "mcp-server-test",
                    "registry_name": "pypi",
                    "runtime_hint": "uvx",
                    "runtime_arguments": [
                        {
                            "is_required": True,
                            "value": "mcp-server-test",
                            "value_hint": "mcp-server-test",
                        },
                        {
                            "is_required": True,
                            "value": "<CONFIG>",
                            "value_hint": "config.yaml",
                            "description": "Configuration file path",
                        },
                    ],
                }
            ],
        }

        # Format server config
        server_config, _ = self.adapter._format_server_config(server_info)

        # Validate the args array includes all required runtime arguments
        self.assertEqual(server_config["type"], "stdio")
        self.assertEqual(server_config["command"], "uvx")
        self.assertEqual(len(server_config["args"]), 2)
        self.assertEqual(server_config["args"][0], "mcp-server-test")
        self.assertEqual(server_config["args"][1], "config.yaml")


if __name__ == "__main__":
    unittest.main()
