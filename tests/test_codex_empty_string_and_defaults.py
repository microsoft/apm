"""
Test suite for empty string handling and default values in the Codex adapter.

This test verifies that the adapter:
1. Treats empty strings as "no value" and applies defaults
2. Respects user-provided non-empty values
3. Adds essential default environment variables for GitHub MCP server
4. Maintains consistent behavior for environment variable handling
"""

import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch  # noqa: F401

import pytest

# Add the source directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apm_cli.adapters.client.codex import CodexClientAdapter

pytestmark = pytest.mark.component


class TestCodexEmptyStringAndDefaults:
    """Test empty string handling and default values for Codex adapter."""

    @pytest.fixture
    def github_mcp_server_data(self):
        """GitHub MCP server data for testing."""
        return {
            "id": "ab12cd34-5678-90ef-1234-567890abcdef",
            "name": "io.github.github/github-mcp-server",
            "packages": [
                {
                    "registry_name": "docker",
                    "name": "ghcr.io/github/github-mcp-server",
                    "runtime_hint": "docker",
                    "runtime_arguments": [
                        {
                            "format": "string",
                            "is_required": True,
                            "type": "positional",
                            "value": "run",
                        },
                        {"format": "string", "is_required": True, "type": "named", "value": "-i"},
                        {"format": "string", "is_required": True, "type": "named", "value": "--rm"},
                        {
                            "format": "string",
                            "is_required": True,
                            "type": "positional",
                            "value": "ghcr.io/github/github-mcp-server",
                        },
                    ],
                    "package_arguments": [],
                    "environment_variables": [
                        {
                            "name": "GITHUB_PERSONAL_ACCESS_TOKEN",
                            "description": "GitHub Personal Access Token for authentication",
                        },
                        {
                            "name": "GITHUB_TOOLSETS",
                            "description": "Comma-separated list of enabled toolsets",
                        },
                        {
                            "name": "GITHUB_HOST",
                            "description": "GitHub Enterprise Server hostname (optional)",
                        },
                        {
                            "name": "GITHUB_READ_ONLY",
                            "description": "Enable read-only mode (1 for true)",
                        },
                        {
                            "name": "GITHUB_DYNAMIC_TOOLSETS",
                            "description": "Enable dynamic toolset discovery (1 for true)",
                        },
                    ],
                }
            ],
        }

    def test_codex_user_values_override_defaults(self, github_mcp_server_data):
        """Test that Codex adapter respects user-provided values over defaults."""
        adapter = CodexClientAdapter()

        # Provide non-empty values for variables that have defaults
        env_overrides = {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_token_123",
            "GITHUB_TOOLSETS": "custom_toolset",  # User value - should not get default
            "GITHUB_DYNAMIC_TOOLSETS": "0",  # User value - should not get default
        }

        with patch.object(adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = github_mcp_server_data

            result = adapter.configure_mcp_server(
                "io.github.github/github-mcp-server", env_overrides=env_overrides
            )

            assert result is True

            config = adapter.get_current_config()
            server_config = config["mcp_servers"]["github-mcp-server"]

            # Should use user values, not defaults
            env_section = server_config["env"]
            assert env_section["GITHUB_TOOLSETS"] == "custom_toolset"  # User value
            assert env_section["GITHUB_DYNAMIC_TOOLSETS"] == "0"  # User value


class TestCodexSelfDefinedStdioEnvResolution:
    """Regression coverage for issue #1266.

    Self-defined stdio MCP servers declared in ``apm.yml`` pass their ``env``
    block through the adapter as a plain dict (the ``_raw_stdio["env"]``
    shape). Before #1266, the Codex adapter wrote that dict to disk verbatim,
    so placeholders like ``${TOKEN}`` ended up as literal strings in
    ``~/.codex/config.toml`` and authentication silently failed. The fix
    routes the dict through ``_resolve_environment_variables`` in legacy
    mode so all three placeholder syntaxes resolve to literal values from
    ``env_overrides`` -> ``os.environ`` at install time.
    """

    @pytest.fixture
    def adapter(self, tmp_path, monkeypatch):
        """Codex adapter writing into an isolated ~/.codex under tmp_path."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        return CodexClientAdapter(user_scope=True)

    @staticmethod
    def _server_info_with_placeholders():
        return {
            "name": "bitbucket",
            "id": "",
            "_raw_stdio": {
                "command": "pnpx",
                "args": ["@aashari/mcp-server-atlassian-bitbucket@3.1.0"],
                "env": {
                    "TOKEN_DOLLAR": "${ATLASSIAN_API_TOKEN}",
                    "TOKEN_ENVPREFIX": "${env:ATLASSIAN_API_TOKEN}",
                    "TOKEN_ANGLE": "<ATLASSIAN_API_TOKEN>",
                    "LITERAL_EMAIL": "user@example.com",
                },
            },
        }

    def test_all_three_placeholder_syntaxes_resolve_to_literal(self, adapter):
        env_overrides = {"ATLASSIAN_API_TOKEN": "real-secret-xyz123"}

        with patch.object(adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = (
                self._server_info_with_placeholders()
            )
            ok = adapter.configure_mcp_server("bitbucket", env_overrides=env_overrides)

        assert ok is True
        env_block = adapter.get_current_config()["mcp_servers"]["bitbucket"]["env"]
        assert env_block["TOKEN_DOLLAR"] == "real-secret-xyz123"
        assert env_block["TOKEN_ENVPREFIX"] == "real-secret-xyz123"
        assert env_block["TOKEN_ANGLE"] == "real-secret-xyz123"
        assert env_block["LITERAL_EMAIL"] == "user@example.com"

    def test_unresolvable_placeholder_is_preserved(self, adapter, monkeypatch):
        """When neither env_overrides nor os.environ provides the value,
        the placeholder must round-trip unchanged rather than disappear."""
        monkeypatch.delenv("ATLASSIAN_API_TOKEN", raising=False)

        with patch.object(adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = (
                self._server_info_with_placeholders()
            )
            adapter.configure_mcp_server("bitbucket")

        env_block = adapter.get_current_config()["mcp_servers"]["bitbucket"]["env"]
        assert env_block["TOKEN_DOLLAR"] == "${ATLASSIAN_API_TOKEN}"
        assert env_block["TOKEN_ENVPREFIX"] == "${env:ATLASSIAN_API_TOKEN}"
        assert env_block["TOKEN_ANGLE"] == "<ATLASSIAN_API_TOKEN>"

    def test_placeholders_in_args_also_resolve(self, adapter):
        server_info = {
            "name": "demo",
            "id": "",
            "_raw_stdio": {
                "command": "demo",
                "args": ["--token", "<API_TOKEN>"],
                "env": {"API_TOKEN": "<API_TOKEN>"},
            },
        }

        with patch.object(adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = server_info
            adapter.configure_mcp_server("demo", env_overrides={"API_TOKEN": "tok-abc"})

        srv = adapter.get_current_config()["mcp_servers"]["demo"]
        assert srv["env"]["API_TOKEN"] == "tok-abc"
        assert srv["args"] == ["--token", "tok-abc"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
