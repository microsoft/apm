"""Tests for environment variables handling in VSCode adapter."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from apm_cli.adapters.client.vscode import VSCodeClientAdapter


class TestEnvironmentVariablesHandling(unittest.TestCase):
    """Test cases for environment variables handling in VSCode adapter."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.vscode_dir = os.path.join(self.temp_dir, ".vscode")
        os.makedirs(self.vscode_dir, exist_ok=True)
        self.temp_path = os.path.join(self.vscode_dir, "mcp.json")

        # Create a temporary MCP configuration file
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

    def tearDown(self):
        """Tear down test fixtures."""
        self.mock_registry_patcher.stop()
        self.mock_integration_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    def test_configure_mcp_server_with_environment_variables(self, mock_get_path):
        """Test configuring an MCP server with environment variables."""
        # Prepare the server info with environment variables
        server_info = {
            "id": "eb5b0c73-1ed5-4180-b0ce-2cb8a36ee3f5",
            "name": "io.github.tinyfish-io/agentql-mcp",
            "description": "Model Context Protocol server that integrates AgentQL's data extraction capabilities.",
            "packages": [
                {
                    "registry_name": "npm",
                    "name": "agentql-mcp",
                    "version": "1.0.0",
                    "runtime_hint": "npx",
                    "environment_variables": [
                        {"description": "YOUR_API_KEY", "name": "AGENTQL_API_KEY"}
                    ],
                }
            ],
        }

        # Set up the mock
        mock_get_path.return_value = self.temp_path
        self.mock_registry.find_server_by_reference.return_value = server_info

        # Create the adapter and configure the server
        adapter = VSCodeClientAdapter()
        result = adapter.configure_mcp_server(
            server_url="io.github.tinyfish-io/agentql-mcp",
            server_name="io.github.tinyfish-io/agentql-mcp",
        )

        # Check the result
        self.assertTrue(result)

        # Read the config file and verify the content
        with open(self.temp_path) as f:
            updated_config = json.load(f)

        # Check the server configuration
        server_config = updated_config["servers"]["io.github.tinyfish-io/agentql-mcp"]
        self.assertEqual(server_config["type"], "stdio")
        self.assertEqual(server_config["command"], "npx")
        self.assertEqual(server_config["args"], ["-y", "agentql-mcp"])

        # Verify environment variables were added
        self.assertIn("env", server_config)
        self.assertIn("AGENTQL_API_KEY", server_config["env"])
        self.assertEqual(server_config["env"]["AGENTQL_API_KEY"], "${input:agentql-api-key}")

        # Verify input variables were added
        self.assertIn("inputs", updated_config)
        self.assertIsInstance(updated_config["inputs"], list)
        self.assertTrue(len(updated_config["inputs"]) > 0)

        # Check if the input variable for the API key is present
        input_var_found = False
        for input_var in updated_config["inputs"]:
            if input_var.get("id") == "agentql-api-key":
                input_var_found = True
                self.assertEqual(input_var["type"], "promptString")
                self.assertTrue(input_var["password"])
                self.assertIn("description", input_var)
                break

        self.assertTrue(input_var_found, "Input variable definition not found")


if __name__ == "__main__":
    unittest.main()
