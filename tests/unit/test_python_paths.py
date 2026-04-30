"""Test handling of Python path configuration in MCP server configuration."""

import unittest
from unittest import mock  # noqa: F401

from apm_cli.adapters.client.vscode import VSCodeClientAdapter


class TestPythonPaths(unittest.TestCase):
    """Test cases for handling custom Python paths in MCP server configuration."""

    def setUp(self):
        """Set up test fixtures."""
        self.adapter = VSCodeClientAdapter()

    def test_davinci_resolve_mcp_handling(self):
        """Test the specific case of davinci-resolve-mcp server."""
        # Mock server info based on the davinci-resolve-mcp server from the registry
        server_info = {
            "name": "io.github.samuelgursky/davinci-resolve-mcp",
            "package_canonical": "pypi",
            "packages": [
                {
                    "registry_name": "pypi",
                    "name": "samuelgursky/davinci-resolve-mcp",
                    "runtime_hint": "/path/to/your/venv/bin/python",
                    "runtime_arguments": [
                        {
                            "is_required": True,
                            "format": "string",
                            "value": "/path/to/your/davinci-resolve-mcp/src/main.py",
                            "default": "/path/to/your/davinci-resolve-mcp/src/main.py",
                            "type": "positional",
                            "value_hint": "/path/to/your/davinci-resolve-mcp/src/main.py",
                        }
                    ],
                }
            ],
        }

        # Format server config
        server_config, _ = self.adapter._format_server_config(server_info)

        # Validate the command matches the runtime_hint and args match runtime_arguments
        self.assertEqual(server_config["command"], "/path/to/your/venv/bin/python")
        self.assertEqual(len(server_config["args"]), 1)
        self.assertEqual(server_config["args"][0], "/path/to/your/davinci-resolve-mcp/src/main.py")


if __name__ == "__main__":
    unittest.main()
