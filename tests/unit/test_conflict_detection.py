"""Tests for MCP conflict detection functionality."""

import unittest
from unittest.mock import Mock, patch  # noqa: F401

from apm_cli.adapters.client.base import MCPClientAdapter
from apm_cli.core.conflict_detector import MCPConflictDetector


class TestMCPConflictDetection(unittest.TestCase):
    """Test suite for MCP conflict detection."""

    def setUp(self):
        """Set up test fixtures."""
        # Create a mock adapter
        self.mock_adapter = Mock(spec=MCPClientAdapter)
        self.mock_adapter.__class__.__name__ = "CopilotClientAdapter"
        self.mock_adapter.registry_client = Mock()

        # Mock existing configuration with UUIDs
        self.existing_config = {
            "mcpServers": {
                "github-server": {
                    "command": "docker",
                    "args": ["run", "ghcr.io/github/github-mcp-server"],
                    "id": "github-server-uuid-123",
                },
                "my-github-server": {
                    "command": "docker",
                    "args": ["run", "ghcr.io/github/github-mcp-server"],
                    "id": "github-server-uuid-123",  # Same UUID as above
                },
            }
        }
        self.mock_adapter.get_current_config.return_value = self.existing_config

        self.detector = MCPConflictDetector(self.mock_adapter)

    def test_detects_exact_canonical_match(self):
        """Test detection of exact canonical name matches."""
        # Mock registry to return canonical name and UUID
        self.mock_adapter.registry_client.find_server_by_reference.return_value = {
            "name": "io.github.github/github-mcp-server",
            "id": "github-server-uuid-123",
        }

        # Test that "github-server" (which exists in config) is detected
        result = self.detector.check_server_exists("github-server")
        self.assertTrue(result)

    def test_detects_canonical_name_match(self):
        """Test detection of servers with same canonical name."""

        # Mock registry to resolve "github" to canonical name and UUID
        def mock_find_server(server_ref):
            if (
                server_ref == "github"  # noqa: PLR1714
                or server_ref == "my-github-server"
                or server_ref == "github-server"
            ):
                return {
                    "name": "io.github.github/github-mcp-server",
                    "id": "github-server-uuid-123",
                }
            return None

        self.mock_adapter.registry_client.find_server_by_reference.side_effect = mock_find_server

        # Test that "github" is detected as conflicting with existing server
        result = self.detector.check_server_exists("github")
        self.assertTrue(result)

    def test_handles_user_defined_names(self):
        """Test handling of user-defined server names."""

        # Mock registry to resolve both servers to same canonical name and UUID
        def mock_find_server(server_ref):
            if server_ref in ["github", "my-github-server", "github-server"]:
                return {
                    "name": "io.github.github/github-mcp-server",
                    "id": "github-server-uuid-123",
                }
            return None

        self.mock_adapter.registry_client.find_server_by_reference.side_effect = mock_find_server

        # Test that new "github" conflicts with existing "my-github-server"
        result = self.detector.check_server_exists("github")
        self.assertTrue(result)

    def test_allows_different_servers(self):
        """Test that different servers are not flagged as conflicts."""

        # Mock registry to return different canonical names and UUIDs
        def mock_find_server(server_ref):
            if server_ref == "notion":
                return {
                    "name": "io.github.makenotion/notion-mcp-server",
                    "id": "notion-server-uuid-456",
                }
            elif server_ref in ["my-github-server", "github-server"]:
                return {
                    "name": "io.github.github/github-mcp-server",
                    "id": "github-server-uuid-123",
                }
            return None

        self.mock_adapter.registry_client.find_server_by_reference.side_effect = mock_find_server

        # Test that "notion" doesn't conflict with existing GitHub servers
        result = self.detector.check_server_exists("notion")
        self.assertFalse(result)

    def test_handles_registry_lookup_failure(self):
        """Test graceful handling when registry lookup fails."""
        # Mock registry to raise exception
        self.mock_adapter.registry_client.find_server_by_reference.side_effect = Exception(
            "Registry unavailable"
        )

        # Should not raise exception and fall back to canonical name comparison
        result = self.detector.check_server_exists("some-unknown-server")
        self.assertFalse(result)

        # Should detect exact string match even when registry fails (fallback to canonical name matching)
        result = self.detector.check_server_exists("github-server")
        self.assertTrue(result)  # Should find exact match in existing config

    def test_get_existing_server_configs_copilot(self):
        """Test extraction of existing server configs for Copilot."""
        self.mock_adapter.__class__.__name__ = "CopilotClientAdapter"

        configs = self.detector.get_existing_server_configs()
        expected = {
            "github-server": {
                "command": "docker",
                "args": ["run", "ghcr.io/github/github-mcp-server"],
                "id": "github-server-uuid-123",
            },
            "my-github-server": {
                "command": "docker",
                "args": ["run", "ghcr.io/github/github-mcp-server"],
                "id": "github-server-uuid-123",
            },
        }
        self.assertEqual(configs, expected)

    def test_get_existing_server_configs_codex(self):
        """Test extraction of existing server configs for Codex."""
        self.mock_adapter.__class__.__name__ = "CodexClientAdapter"

        # Mock TOML-style config
        toml_config = {
            "mcp_servers.github": {
                "command": "docker",
                "args": ["run", "ghcr.io/github/github-mcp-server"],
            },
            'mcp_servers."io.github.github/github-mcp-server"': {
                "command": "docker",
                "args": ["run", "ghcr.io/github/github-mcp-server"],
            },
            "mcp_servers.github.env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            "model_provider": "github-models",
        }
        self.mock_adapter.get_current_config.return_value = toml_config

        configs = self.detector.get_existing_server_configs()
        expected = {
            "github": {"command": "docker", "args": ["run", "ghcr.io/github/github-mcp-server"]},
            "io.github.github/github-mcp-server": {
                "command": "docker",
                "args": ["run", "ghcr.io/github/github-mcp-server"],
            },
        }
        self.assertEqual(configs, expected)

    def test_get_conflict_summary(self):
        """Test detailed conflict summary generation."""

        # Mock registry lookup
        def mock_find_server(server_ref):
            if server_ref in ["github", "my-github-server", "github-server"]:
                return {
                    "name": "io.github.github/github-mcp-server",
                    "id": "github-server-uuid-123",
                }
            return None

        self.mock_adapter.registry_client.find_server_by_reference.side_effect = mock_find_server

        summary = self.detector.get_conflict_summary("github")

        self.assertTrue(summary["exists"])
        self.assertEqual(summary["canonical_name"], "io.github.github/github-mcp-server")
        self.assertEqual(len(summary["conflicting_servers"]), 2)

        # Check that we have canonical matches (both user-defined server names resolve to same canonical name)
        conflict_types = [server["type"] for server in summary["conflicting_servers"]]
        self.assertIn("canonical_match", conflict_types)
        # Verify the server names found
        server_names = [server["name"] for server in summary["conflicting_servers"]]
        self.assertIn("github-server", server_names)
        self.assertIn("my-github-server", server_names)
        self.assertIn("canonical_match", conflict_types)


if __name__ == "__main__":
    unittest.main()
