"""
Test suite for Codex Docker args duplication fix using real registry data.

This test verifies that the Docker args processing in the Codex adapter:
1. Uses registry runtime_arguments directly without duplication
2. Puts environment variable values in the [env] section (TOML format)
3. Handles both Docker and npm packages correctly
"""

import os
import sys
from unittest.mock import Mock, patch  # noqa: F401

import pytest

# Add the source directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apm_cli.adapters.client.codex import CodexClientAdapter


class TestCodexDockerArgsFix:
    """Test the Docker args duplication fix using real registry data."""

    @pytest.fixture
    def codex_adapter(self):
        """Create a Codex adapter instance for testing."""
        return CodexClientAdapter()

    @pytest.fixture
    def github_mcp_server_data(self):
        """Real GitHub MCP server data from registry API."""
        return {
            "id": "ab12cd34-5678-90ef-1234-567890abcdef",
            "name": "io.github.github/github-mcp-server",
            "description": "Official GitHub MCP Server that connects AI tools directly to GitHub's platform.",
            "packages": [
                {
                    "registry_name": "docker",
                    "name": "ghcr.io/github/github-mcp-server",
                    "runtime_hint": "docker",
                    "version": "latest",
                    "runtime_arguments": [
                        {
                            "format": "string",
                            "is_required": True,
                            "type": "positional",
                            "value": "run",
                            "value_hint": "docker_cmd",
                        },
                        {
                            "format": "string",
                            "is_required": True,
                            "type": "named",
                            "value": "-i",
                            "value_hint": "interactive_flag",
                        },
                        {
                            "format": "string",
                            "is_required": True,
                            "type": "named",
                            "value": "--rm",
                            "value_hint": "remove_flag",
                        },
                        {
                            "format": "string",
                            "is_required": True,
                            "type": "named",
                            "value": "-e",
                            "value_hint": "env_flag",
                        },
                        {
                            "format": "string",
                            "is_required": True,
                            "type": "positional",
                            "value": "GITHUB_PERSONAL_ACCESS_TOKEN",
                            "value_hint": "env_var_name",
                        },
                        {
                            "format": "string",
                            "is_required": True,
                            "type": "positional",
                            "value": "ghcr.io/github/github-mcp-server",
                            "value_hint": "image",
                        },
                    ],
                    "package_arguments": [],  # Empty for GitHub MCP server
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
                            "description": "GitHub Enterprise Server or ghe.com hostname (optional)",
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

    @pytest.fixture
    def notion_mcp_server_data(self):
        """Real Notion MCP server data from registry API."""
        return {
            "id": "8b9c1d20-2345-6789-abcd-ef0123456789",
            "name": "io.github.makenotion/notion-mcp-server",
            "description": "Official Notion MCP Server for API integration.",
            "packages": [
                {
                    "registry_name": "npm",
                    "name": "@notionhq/notion-mcp-server",
                    "runtime_hint": "npx",
                    "version": "1.8.1",
                    "runtime_arguments": [
                        {
                            "format": "string",
                            "type": "positional",
                            "value": "-y",
                            "value_hint": "noninteractive_mode",
                        },
                        {
                            "format": "string",
                            "type": "positional",
                            "value": "@notionhq/notion-mcp-server@1.8.1",
                            "value_hint": "notion_mcp_version",
                        },
                        {
                            "format": "string",
                            "type": "named",
                            "value": "--transport",
                            "value_hint": "transport_flag",
                        },
                        {
                            "format": "string",
                            "type": "positional",
                            "value": "stdio",
                            "value_hint": "stdio",
                        },
                        {
                            "format": "string",
                            "type": "named",
                            "value": "--port",
                            "value_hint": "port_flag",
                        },
                        {
                            "format": "string",
                            "type": "positional",
                            "value": "3000",
                            "value_hint": "3000",
                        },
                        {
                            "format": "string",
                            "is_required": True,
                            "type": "positional",
                            "value": "<NOTION_TOKEN>",
                            "value_hint": "notion_token",
                        },
                    ],
                    "package_arguments": [],
                    "environment_variables": [
                        {
                            "name": "NOTION_TOKEN",
                            "description": "Notion API token for authentication",
                        }
                    ],
                }
            ],
        }

    @pytest.fixture
    def sample_env_overrides(self):
        """Sample environment overrides as collected by apm install."""
        return {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test_token_12345",
            "GITHUB_TOOLSETS": "context",
            "GITHUB_READ_ONLY": "0",
            "NOTION_TOKEN": "secret_notion_token_67890",
        }

    def test_github_docker_server_config_generation(
        self, codex_adapter, github_mcp_server_data, sample_env_overrides
    ):
        """Test that GitHub MCP server generates correct Docker config without duplication."""
        # Mock the registry client
        with patch.object(codex_adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = github_mcp_server_data

            # Configure the server with environment overrides
            result = codex_adapter.configure_mcp_server(
                "io.github.github/github-mcp-server", env_overrides=sample_env_overrides
            )

            assert result is True

            # Verify the configuration was generated correctly
            config = codex_adapter.get_current_config()
            assert "mcp_servers" in config
            assert "github-mcp-server" in config["mcp_servers"]

            server_config = config["mcp_servers"]["github-mcp-server"]

            # Check command
            assert server_config["command"] == "docker"

            # Check args - should include -e flags for ALL environment variables from env_overrides
            args = server_config["args"]

            # Verify basic Docker structure
            assert "run" in args
            assert "-i" in args
            assert "--rm" in args
            assert "ghcr.io/github/github-mcp-server" in args

            # Verify ALL environment variables from sample_env_overrides are represented as -e flags
            expected_env_vars = {
                "GITHUB_PERSONAL_ACCESS_TOKEN",
                "GITHUB_TOOLSETS",
                "GITHUB_READ_ONLY",
            }
            actual_env_vars = set()

            for i, arg in enumerate(args):
                if arg == "-e" and i + 1 < len(args):
                    actual_env_vars.add(args[i + 1])

            assert expected_env_vars.issubset(actual_env_vars), (
                f"Missing env vars in args. Expected: {expected_env_vars}, Found: {actual_env_vars}"
            )

            # Check environment variables are in separate env section with actual values
            assert "env" in server_config
            env_vars = server_config["env"]
            assert env_vars["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_test_token_12345"
            assert env_vars["GITHUB_TOOLSETS"] == "context"
            assert env_vars["GITHUB_READ_ONLY"] == "0"

            # Verify no duplication - each element should appear only once
            args_str = " ".join(args)
            assert args_str.count("run") == 1
            assert args_str.count("ghcr.io/github/github-mcp-server") == 1

            # Verify args don't contain actual token values (only env var names)
            assert "ghp_test_token_12345" not in args_str
            assert "context" not in args_str

    def test_notion_npm_server_config_generation(
        self, codex_adapter, notion_mcp_server_data, sample_env_overrides
    ):
        """Test that Notion npm server generates correct config."""
        # Mock the registry client
        with patch.object(codex_adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = notion_mcp_server_data

            # Configure the server with environment overrides
            result = codex_adapter.configure_mcp_server(
                "io.github.makenotion/notion-mcp-server", env_overrides=sample_env_overrides
            )

            assert result is True

            # Verify the configuration was generated correctly
            config = codex_adapter.get_current_config()
            assert "mcp_servers" in config
            assert "notion-mcp-server" in config["mcp_servers"]

            server_config = config["mcp_servers"]["notion-mcp-server"]

            # Check command
            assert server_config["command"] == "npx"

            # Check args for npm package
            expected_args = [
                "-y",
                "@notionhq/notion-mcp-server@1.8.1",
                "--transport",
                "stdio",
                "--port",
                "3000",
                "secret_notion_token_67890",
            ]
            assert server_config["args"] == expected_args

            # Check environment variables are in separate env section
            assert "env" in server_config
            env_vars = server_config["env"]
            assert env_vars["NOTION_TOKEN"] == "secret_notion_token_67890"

    def test_docker_server_with_package_arguments(self, codex_adapter, sample_env_overrides):
        """Test Docker server that has both runtime_arguments and package_arguments."""
        docker_server_with_package_args = {
            "id": "test-docker-with-pkg-args",
            "name": "test-docker-server",
            "packages": [
                {
                    "registry_name": "docker",
                    "name": "example/test-server",
                    "runtime_hint": "docker",
                    "runtime_arguments": [
                        {"type": "positional", "value": "run"},
                        {"type": "named", "value": "-i"},
                        {"type": "named", "value": "--rm"},
                        {"type": "positional", "value": "example/test-server"},
                    ],
                    "package_arguments": [
                        {"type": "named", "value": "--verbose"},
                        {"type": "named", "value": "--config"},
                        {"type": "positional", "value": "/app/config.json"},
                    ],
                    "environment_variables": [{"name": "TEST_TOKEN", "description": "Test token"}],
                }
            ],
        }

        test_env_overrides = {"TEST_TOKEN": "test_token_value"}

        # Mock the registry client
        with patch.object(codex_adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = docker_server_with_package_args

            result = codex_adapter.configure_mcp_server(
                "test-docker-server", env_overrides=test_env_overrides
            )

            assert result is True

            config = codex_adapter.get_current_config()
            server_config = config["mcp_servers"]["test-docker-server"]

            # Check that both runtime_arguments and package_arguments are combined
            # Plus -e flags for environment variables (inserted before image name)
            expected_args = [
                "run",
                "-i",
                "--rm",
                "example/test-server",
                "--verbose",
                "--config",
                "-e",
                "TEST_TOKEN",
                "/app/config.json",
            ]
            assert server_config["args"] == expected_args

            # Environment variables should be in env section
            assert server_config["env"]["TEST_TOKEN"] == "test_token_value"

    def test_no_duplication_in_complex_scenarios(
        self, codex_adapter, github_mcp_server_data, sample_env_overrides
    ):
        """Test that complex scenarios don't cause duplication."""
        # Mock the registry client
        with patch.object(codex_adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = github_mcp_server_data

            # Configure the same server multiple times to test for accumulation
            for _ in range(3):
                result = codex_adapter.configure_mcp_server(
                    "io.github.github/github-mcp-server", env_overrides=sample_env_overrides
                )
                assert result is True

            config = codex_adapter.get_current_config()
            server_config = config["mcp_servers"]["github-mcp-server"]

            # Verify no accumulation of args - should include ALL env vars as -e flags
            expected_args = [
                "run",
                "-i",
                "--rm",
                "-e",
                "GITHUB_PERSONAL_ACCESS_TOKEN",
                "-e",
                "GITHUB_DYNAMIC_TOOLSETS",
                "-e",
                "GITHUB_READ_ONLY",
                "-e",
                "GITHUB_TOOLSETS",
                "ghcr.io/github/github-mcp-server",
            ]
            assert server_config["args"] == expected_args

            # Verify args don't contain duplicated elements (except -e which appears multiple times for multiple env vars)
            args = server_config["args"]
            assert args.count("run") == 1
            assert args.count("-i") == 1
            assert args.count("--rm") == 1
            assert (
                args.count("-e") == 4
            )  # One for each environment variable (including GITHUB_DYNAMIC_TOOLSETS default)
            assert args.count("GITHUB_PERSONAL_ACCESS_TOKEN") == 1
            assert args.count("ghcr.io/github/github-mcp-server") == 1

    def test_all_collected_env_vars_become_docker_flags(
        self, codex_adapter, github_mcp_server_data
    ):
        """Test that ALL collected environment variables become -e flags in Docker args."""
        # Comprehensive environment variables collected during apm install
        comprehensive_env_overrides = {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_test_token_12345",
            "GITHUB_TOOLSETS": "repos,issues,pull_requests",
            "GITHUB_HOST": "github.example.com",
            "GITHUB_READ_ONLY": "1",
            "GITHUB_DYNAMIC_TOOLSETS": "1",
        }

        # Mock the registry client
        with patch.object(codex_adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = github_mcp_server_data

            result = codex_adapter.configure_mcp_server(
                "io.github.github/github-mcp-server", env_overrides=comprehensive_env_overrides
            )

            assert result is True

            config = codex_adapter.get_current_config()
            server_config = config["mcp_servers"]["github-mcp-server"]

            # Extract all -e flags from args
            args = server_config["args"]
            env_flags = []
            for i, arg in enumerate(args):
                if arg == "-e" and i + 1 < len(args):
                    env_flags.append(args[i + 1])

            # Verify ALL environment variables are represented as -e flags
            expected_env_vars = set(comprehensive_env_overrides.keys())
            actual_env_flags = set(env_flags)

            assert expected_env_vars == actual_env_flags, (
                f"Missing env flags. Expected: {expected_env_vars}, Got: {actual_env_flags}"
            )

            # Verify the [env] section contains all actual values
            env_section = server_config["env"]
            for env_name, env_value in comprehensive_env_overrides.items():
                assert env_section[env_name] == env_value

            # Verify Docker command structure is maintained
            assert "run" in args
            assert "-i" in args
            assert "--rm" in args
            assert "ghcr.io/github/github-mcp-server" in args

            # Verify no actual env values leak into args (only env var names)
            args_str = " ".join(args)
            assert "ghp_test_token_12345" not in args_str
            assert "repos,issues,pull_requests" not in args_str
            assert "github.example.com" not in args_str

    def test_toml_format_output(self, codex_adapter, github_mcp_server_data, sample_env_overrides):
        """Test that the TOML output format is correct."""
        # Mock the registry client
        with patch.object(codex_adapter, "registry_client") as mock_registry:
            mock_registry.find_server_by_reference.return_value = github_mcp_server_data

            # Test the configuration generation without file I/O
            result = codex_adapter.configure_mcp_server(
                "io.github.github/github-mcp-server", env_overrides=sample_env_overrides
            )

            assert result is True

            # Get the in-memory configuration
            config = codex_adapter.get_current_config()

            # Verify configuration structure
            assert "mcp_servers" in config
            assert "github-mcp-server" in config["mcp_servers"]

            server_config = config["mcp_servers"]["github-mcp-server"]

            # Check server configuration
            assert server_config["command"] == "docker"
            expected_args = [
                "run",
                "-i",
                "--rm",
                "-e",
                "GITHUB_PERSONAL_ACCESS_TOKEN",
                "-e",
                "GITHUB_DYNAMIC_TOOLSETS",
                "-e",
                "GITHUB_READ_ONLY",
                "-e",
                "GITHUB_TOOLSETS",
                "ghcr.io/github/github-mcp-server",
            ]
            assert server_config["args"] == expected_args

            # Check environment variables section
            assert "env" in server_config
            env_vars = server_config["env"]
            assert env_vars["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_test_token_12345"
            assert env_vars["GITHUB_TOOLSETS"] == "context"

            # Verify no duplication in args
            args = server_config["args"]
            assert args.count("run") == 1
            assert args.count("ghcr.io/github/github-mcp-server") == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
