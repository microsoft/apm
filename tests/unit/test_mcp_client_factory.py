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

    @patch("apm_cli.adapters.client.codex._rich_warning")
    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_sse_remote_rejected(self, mock_find_server, mock_warn):
        """SSE remotes are rejected with a warning that points to streamable-http."""
        mock_server_info = {
            "id": "remote-server-id",
            "name": "remote-server",
            "remotes": [{"transport_type": "sse", "url": "https://example.com/mcp"}],
            "packages": [],
        }
        mock_find_server.return_value = mock_server_info

        result = self.adapter.configure_mcp_server("remote-server")

        self.assertFalse(result)
        mock_find_server.assert_called_once_with("remote-server")

        mock_warn.assert_called_once()
        warn_message = mock_warn.call_args[0][0]
        self.assertIn("remote-server", warn_message)
        self.assertIn("SSE", warn_message)
        self.assertIn("streamable-http", warn_message)

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

    @staticmethod
    def _remote_with_headers(headers):
        """Build self-defined streamable-http server info carrying *headers*."""
        return {
            "name": "bridge",
            "remotes": [
                {
                    "transport_type": "streamable-http",
                    "url": "https://example.com/mcp",
                    "headers": [{"name": name, "value": value} for name, value in headers],
                }
            ],
        }

    def test_format_server_config_streamable_http_writes_url_and_id(self):
        """Streamable-HTTP remote produces url + id (no http_headers when none)."""
        server_info = {
            "name": "figma",
            "id": "ab12cd34-0000-0000-0000-000000000000",
            "remotes": [
                {
                    "url": "https://mcp.figma.com/mcp",
                    "transport_type": "streamable-http",
                }
            ],
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["url"], "https://mcp.figma.com/mcp")
        self.assertEqual(config["id"], "ab12cd34-0000-0000-0000-000000000000")
        self.assertNotIn("http_headers", config)

    def test_format_server_config_streamable_http_writes_headers(self):
        """Registry-supplied headers land under ``http_headers``."""
        server_info = {
            "name": "figma",
            "remotes": [
                {
                    "url": "https://mcp.figma.com/mcp",
                    "transport_type": "streamable-http",
                    "headers": [
                        {"name": "Authorization", "value": "Bearer ghp_xxx"},
                        {"name": "X-Figma-Region", "value": "us-east-1"},
                    ],
                }
            ],
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(
            config["http_headers"],
            {
                "Authorization": "Bearer ghp_xxx",
                "X-Figma-Region": "us-east-1",
            },
        )

    def test_format_server_config_streamable_http_self_defined(self):
        """Self-defined streamable-http info produces a remote config."""
        server_info = {
            "name": "my-remote",
            "remotes": [
                {
                    "transport_type": "streamable-http",
                    "url": "https://example.com/mcp",
                    "headers": [{"name": "Authorization", "value": "Bearer xyz"}],
                }
            ],
        }
        config = self.adapter._format_server_config(server_info)
        self.assertEqual(config["url"], "https://example.com/mcp")
        self.assertEqual(config["http_headers"], {"Authorization": "Bearer xyz"})

    def test_bearer_placeholder_becomes_bearer_token_env_var(self):
        """``Authorization: Bearer ${VAR}`` uses Codex's bearer-token field.

        ``http_headers`` is documented as static values, so the placeholder
        written there would reach the server as literal ``${VAR}`` text.
        """
        config = self.adapter._format_server_config(
            self._remote_with_headers([("Authorization", "Bearer ${TOKEN}")])
        )
        self.assertEqual(config["bearer_token_env_var"], "TOKEN")
        self.assertNotIn("http_headers", config)
        self.assertNotIn("env_http_headers", config)

    def test_placeholder_only_header_becomes_env_http_headers(self):
        """A value that is exactly ``${VAR}`` names the variable for Codex."""
        config = self.adapter._format_server_config(
            self._remote_with_headers([("X-Api-Key", "${API_KEY}")])
        )
        self.assertEqual(config["env_http_headers"], {"X-Api-Key": "API_KEY"})
        self.assertNotIn("http_headers", config)

    def test_env_prefixed_placeholder_is_recognized(self):
        """``${env:VAR}`` is the same reference as ``${VAR}``."""
        config = self.adapter._format_server_config(
            self._remote_with_headers([("X-Api-Key", "${env:API_KEY}")])
        )
        self.assertEqual(config["env_http_headers"], {"X-Api-Key": "API_KEY"})

    def test_authorization_placeholder_only_uses_env_http_headers(self):
        """A bare ``${VAR}`` on Authorization declares no ``Bearer`` scheme.

        ``bearer_token_env_var`` would make Codex send one, so the whole-value
        field is the faithful rendering.
        """
        config = self.adapter._format_server_config(
            self._remote_with_headers([("Authorization", "${TOKEN}")])
        )
        self.assertEqual(config["env_http_headers"], {"Authorization": "TOKEN"})
        self.assertNotIn("bearer_token_env_var", config)

    def test_literal_header_still_lands_in_http_headers(self):
        """A value with no placeholder is static, which is what the field means."""
        config = self.adapter._format_server_config(
            self._remote_with_headers([("X-Region", "us-east-1")])
        )
        self.assertEqual(config["http_headers"], {"X-Region": "us-east-1"})
        self.assertNotIn("env_http_headers", config)

    @patch("apm_cli.adapters.client.codex._rich_warning")
    def test_mixed_literal_and_placeholder_is_skipped_with_warning(self, mock_warn):
        """Codex has no field for a value that is part literal, part variable."""
        config = self.adapter._format_server_config(
            self._remote_with_headers(
                [("X-Mixed", "prefix-${SOME_VAR}-suffix"), ("X-Region", "us-east-1")]
            )
        )
        self.assertEqual(config["http_headers"], {"X-Region": "us-east-1"})
        self.assertNotIn("env_http_headers", config)
        mock_warn.assert_called_once()
        self.assertIn("X-Mixed", mock_warn.call_args[0][0])

    @patch("apm_cli.adapters.client.codex._rich_warning")
    def test_shell_default_placeholder_is_skipped_with_warning(self, mock_warn):
        """``${VAR:-}`` is a reference Codex cannot resolve from either field.

        The canonical parser does not model the shell default, so this value
        must not fall through to ``http_headers`` as though it were static --
        that is the shape the reported context7 manifest declares.
        """
        config = self.adapter._format_server_config(
            self._remote_with_headers([("Authorization", "${CONTEXT7_API_KEY:-}")])
        )
        self.assertNotIn("http_headers", config)
        self.assertNotIn("env_http_headers", config)
        self.assertNotIn("bearer_token_env_var", config)
        mock_warn.assert_called_once()
        self.assertIn("Authorization", mock_warn.call_args[0][0])

    @patch("apm_cli.adapters.client.codex._rich_warning")
    def test_input_variable_header_is_not_swept_into_the_skip_path(self, mock_warn):
        """``${input:...}`` is collected elsewhere and keeps its own handling.

        Widening the unsupported-reference check must not capture it, or the
        input-variable path would lose its header.
        """
        config = self.adapter._format_server_config(
            self._remote_with_headers([("X-Project", "${input:proj}")])
        )
        self.assertEqual(config["http_headers"], {"X-Project": "${input:proj}"})
        mock_warn.assert_not_called()

    @patch("apm_cli.adapters.client.codex._rich_warning")
    def test_bearer_placeholder_outside_authorization_is_skipped(self, mock_warn):
        """Only Authorization has a bearer field; elsewhere the shape is mixed."""
        config = self.adapter._format_server_config(
            self._remote_with_headers([("X-Auth", "Bearer ${TOKEN}")])
        )
        self.assertNotIn("bearer_token_env_var", config)
        self.assertNotIn("http_headers", config)
        self.assertNotIn("env_http_headers", config)
        mock_warn.assert_called_once()
        self.assertIn("X-Auth", mock_warn.call_args[0][0])

    @patch("apm_cli.adapters.client.codex._rich_warning")
    def test_format_server_config_streamable_http_rejects_non_https(self, mock_warn):
        """Non-HTTPS remote URLs are rejected to prevent cleartext header leakage."""
        server_info = {
            "name": "evil-remote",
            "id": "evil-id",
            "remotes": [
                {
                    "url": "http://mcp.example.com/mcp",
                    "transport_type": "streamable-http",
                    "headers": [{"name": "Authorization", "value": "Bearer secret"}],
                }
            ],
        }

        result = self.adapter._format_server_config(server_info)

        self.assertIsNone(result)
        mock_warn.assert_called_once()
        warn_message = mock_warn.call_args[0][0]
        self.assertIn("evil-remote", warn_message)
        self.assertIn("https://", warn_message)

    @patch("apm_cli.adapters.client.codex._rich_warning")
    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_http_remote_rejected(self, mock_find_server, mock_warn):
        """End-to-end: an http:// remote URL never lands in the Codex config."""
        mock_find_server.return_value = {
            "name": "evil-remote",
            "id": "evil-id",
            "remotes": [
                {
                    "url": "http://mcp.example.com/mcp",
                    "transport_type": "streamable-http",
                    "headers": [{"name": "Authorization", "value": "Bearer secret"}],
                }
            ],
            "packages": [],
        }

        result = self.adapter.configure_mcp_server("evil-remote")

        self.assertFalse(result)
        mock_warn.assert_called_once()
        warn_message = mock_warn.call_args[0][0]
        self.assertIn("evil-remote", warn_message)
        self.assertIn("https://", warn_message)

        # Verify no config was written
        config = self.adapter.get_current_config()
        self.assertNotIn("mcp_servers", config)

    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_streamable_http_writes_toml_entry(self, mock_find_server):
        """End-to-end install of a streamable-HTTP server writes a parseable TOML entry."""
        mock_find_server.return_value = {
            "name": "figma",
            "id": "ab12cd34-0000-0000-0000-000000000000",
            "remotes": [
                {
                    "url": "https://mcp.figma.com/mcp",
                    "transport_type": "streamable-http",
                    "headers": [{"name": "Authorization", "value": "Bearer ghp_xxx"}],
                }
            ],
        }

        result = self.adapter.configure_mcp_server("figma/figma")

        self.assertTrue(result)
        config = self.adapter.get_current_config()
        figma = config["mcp_servers"]["figma"]
        self.assertEqual(figma["url"], "https://mcp.figma.com/mcp")
        self.assertEqual(figma["id"], "ab12cd34-0000-0000-0000-000000000000")
        self.assertEqual(figma["http_headers"], {"Authorization": "Bearer ghp_xxx"})

    @patch("apm_cli.adapters.client.codex._rich_warning")
    def test_format_server_config_streamable_http_rejects_empty_url(self, mock_warn):
        """Empty / whitespace-only remote URLs are rejected with a clear message."""
        for empty_value in ("", "   ", None):
            with self.subTest(url=empty_value):
                mock_warn.reset_mock()
                server_info = {
                    "name": "broken-remote",
                    "id": "broken-id",
                    "remotes": [
                        {
                            "url": empty_value,
                            "transport_type": "streamable-http",
                        }
                    ],
                }
                result = self.adapter._format_server_config(server_info)
                self.assertIsNone(result)
                mock_warn.assert_called_once()
                msg = mock_warn.call_args[0][0]
                self.assertIn("broken-remote", msg)
                # Message must explicitly mention that the URL is empty/missing
                # rather than the misleading "no scheme" wording urlparse would
                # produce for an empty string.
                self.assertTrue(
                    "empty" in msg.lower() or "missing" in msg.lower() or "no url" in msg.lower(),
                    f"Expected empty/missing URL wording; got: {msg!r}",
                )

    @patch("apm_cli.adapters.client.codex._rich_success")
    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_streamable_http_emits_rich_success(
        self, mock_find_server, mock_success
    ):
        """Successful streamable-HTTP registration emits a green _rich_success line."""
        mock_find_server.return_value = {
            "name": "figma",
            "id": "ab12cd34-0000-0000-0000-000000000000",
            "remotes": [
                {
                    "url": "https://mcp.figma.com/mcp",
                    "transport_type": "streamable-http",
                }
            ],
        }
        result = self.adapter.configure_mcp_server("figma/figma")
        self.assertTrue(result)
        mock_success.assert_called_once()
        msg = mock_success.call_args[0][0]
        self.assertIn("figma", msg)
        self.assertIn("Codex CLI", msg)

    @patch("apm_cli.adapters.client.codex._rich_success")
    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_stdio_emits_rich_success(self, mock_find_server, mock_success):
        """stdio registrations also emit _rich_success (not bare print)."""
        mock_find_server.return_value = {
            "id": "test-id",
            "name": "test-server",
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
        result = self.adapter.configure_mcp_server("test-server", "my_server")
        self.assertTrue(result)
        mock_success.assert_called_once()

    @patch("apm_cli.adapters.client.codex._log")
    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_configure_mcp_server_hybrid_logs_precedence(self, mock_find_server, mock_log):
        """Hybrid servers (remotes + packages) log that packages win for Codex."""
        mock_find_server.return_value = {
            "id": "hybrid-server-id",
            "name": "hybrid-server",
            "remotes": [{"transport_type": "streamable-http", "url": "https://example.com/mcp"}],
            "packages": [
                {
                    "registry_name": "npm",
                    "name": "hybrid-package",
                    "version": "1.0.0",
                    "arguments": [],
                }
            ],
            "environment_variables": [],
        }
        result = self.adapter.configure_mcp_server("hybrid-server", "hybrid")
        self.assertTrue(result)
        # At least one debug call must mention the precedence decision.
        debug_messages = [str(call) for call in mock_log.debug.call_args_list]
        self.assertTrue(
            any("hybrid" in m and "package" in m.lower() for m in debug_messages),
            f"Expected a debug log about packages-win precedence; got: {debug_messages}",
        )


if __name__ == "__main__":
    unittest.main()
