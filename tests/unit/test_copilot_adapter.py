"""Unit tests for the Copilot client adapter transport validation (issue #791)."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from apm_cli.adapters.client.copilot import CopilotClientAdapter


class TestCopilotRemoteTransportValidation(unittest.TestCase):
    """Validation of ``transport_type`` mirrors PR #656 (VS Code adapter)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = os.path.join(self.temp_dir, "mcp-config.json")
        with open(self.temp_path, "w") as f:
            json.dump({"mcpServers": {}}, f)

        self.mock_registry_patcher = patch(
            "apm_cli.adapters.client.copilot.SimpleRegistryClient"
        )
        self.mock_registry_class = self.mock_registry_patcher.start()
        self.mock_registry_class.return_value = MagicMock()

        self.mock_integration_patcher = patch(
            "apm_cli.adapters.client.copilot.RegistryIntegration"
        )
        self.mock_integration_class = self.mock_integration_patcher.start()
        self.mock_integration_class.return_value = MagicMock()

        self.get_path_patcher = patch(
            "apm_cli.adapters.client.copilot.CopilotClientAdapter.get_config_path",
            return_value=self.temp_path,
        )
        self.get_path_patcher.start()

    def tearDown(self):
        self.get_path_patcher.stop()
        self.mock_integration_patcher.stop()
        self.mock_registry_patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_remote_missing_transport_type_defaults_to_http(self):
        """Remote with no transport_type produces a type=http config (issue #791)."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-1",
            "name": "atlassian-mcp-server",
            "remotes": [{"url": "https://mcp.atlassian.com/v1/mcp"}],
        }

        config = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")
        self.assertEqual(config["url"], "https://mcp.atlassian.com/v1/mcp")

    def test_remote_empty_transport_type_defaults_to_http(self):
        """Empty string transport_type is treated as missing."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-2",
            "name": "remote-srv",
            "remotes": [{"transport_type": "", "url": "https://example.com/mcp"}],
        }

        config = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")
        self.assertEqual(config["url"], "https://example.com/mcp")

    def test_remote_none_transport_type_defaults_to_http(self):
        """Null transport_type is treated as missing."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-3",
            "name": "remote-srv",
            "remotes": [{"transport_type": None, "url": "https://example.com/mcp"}],
        }

        config = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")

    def test_remote_whitespace_transport_type_defaults_to_http(self):
        """Whitespace-only transport_type is treated as missing."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-4",
            "name": "remote-srv",
            "remotes": [{"transport_type": "  ", "url": "https://example.com/mcp"}],
        }

        config = adapter._format_server_config(server_info)

        self.assertEqual(config["type"], "http")

    def test_remote_unsupported_transport_raises(self):
        """Unrecognized transport_type raises ValueError with server name."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-5",
            "name": "future-srv",
            "remotes": [{"transport_type": "grpc", "url": "https://example.com/mcp"}],
        }

        with self.assertRaises(ValueError) as ctx:
            adapter._format_server_config(server_info)

        message = str(ctx.exception)
        self.assertIn("Unsupported remote transport", message)
        self.assertIn("grpc", message)
        self.assertIn("future-srv", message)
        self.assertIn("Copilot", message)

    def test_remote_supported_transports_do_not_raise(self):
        """'sse' and 'streamable-http' transports pass validation."""
        adapter = CopilotClientAdapter()

        for transport in ("http", "sse", "streamable-http"):
            server_info = {
                "id": f"remote-{transport}",
                "name": f"srv-{transport}",
                "remotes": [{"transport_type": transport, "url": "https://example.com/mcp"}],
            }

            config = adapter._format_server_config(server_info)
            # Copilot CLI always emits type="http" for auth compatibility.
            self.assertEqual(config["type"], "http")
            self.assertEqual(config["url"], "https://example.com/mcp")

    def test_remote_skips_entries_without_url(self):
        """Remotes with empty URLs are skipped; first usable remote wins."""
        adapter = CopilotClientAdapter()

        server_info = {
            "id": "remote-multi",
            "name": "multi-remote",
            "remotes": [
                {"transport_type": "http", "url": ""},
                {"transport_type": "sse", "url": "https://good.example.com/sse"},
            ],
        }

        config = adapter._format_server_config(server_info)
        self.assertEqual(config["url"], "https://good.example.com/sse")


class TestCopilotSelectRemoteWithUrl(unittest.TestCase):
    """Direct unit tests for the ``_select_remote_with_url`` helper."""

    def test_returns_first_remote_with_url(self):
        remotes = [
            {"url": ""},
            {"url": "https://example.com/a"},
            {"url": "https://example.com/b"},
        ]
        self.assertEqual(
            CopilotClientAdapter._select_remote_with_url(remotes)["url"],
            "https://example.com/a",
        )

    def test_returns_none_when_no_url(self):
        remotes = [{"url": ""}, {"url": "   "}, {"url": None}]
        self.assertIsNone(CopilotClientAdapter._select_remote_with_url(remotes))

    def test_handles_empty_list(self):
        self.assertIsNone(CopilotClientAdapter._select_remote_with_url([]))


if __name__ == "__main__":
    unittest.main()
