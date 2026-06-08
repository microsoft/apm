"""Regression tests for scoped MCP server config keys."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.core.safe_installer import SafeMCPInstaller


@pytest.fixture
def raw_stdio_servers() -> dict[str, dict]:
    """Return self-defined stdio server fixtures keyed by declared name."""
    return {
        "@playwright/mcp": {"_raw_stdio": {"command": "npx", "args": ["-y", "@playwright/mcp"]}},
        "@other/mcp": {"_raw_stdio": {"command": "npx", "args": ["-y", "@other/mcp"]}},
    }


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeClientAdapter, CodexClientAdapter, CopilotClientAdapter],
)
def test_scoped_server_name_is_preserved_as_config_key(adapter_cls, raw_stdio_servers):
    """Adapters must not truncate npm scoped package names to the basename."""
    captured_keys = []
    adapter = adapter_cls(project_root=Path.cwd(), user_scope=True)
    adapter.update_config = lambda updates: captured_keys.extend(updates.keys()) or True

    result = adapter.configure_mcp_server("@playwright/mcp", server_info_cache=raw_stdio_servers)

    assert result is True
    assert captured_keys == ["@playwright/mcp"]


@pytest.mark.parametrize(
    "adapter_cls",
    [ClaudeClientAdapter, CodexClientAdapter, CopilotClientAdapter],
)
def test_two_scoped_server_names_do_not_collide(adapter_cls, raw_stdio_servers):
    """Distinct scoped packages with the same basename must keep distinct keys."""
    captured_keys = []
    adapter = adapter_cls(project_root=Path.cwd(), user_scope=True)
    adapter.update_config = lambda updates: captured_keys.extend(updates.keys()) or True

    for server_ref in raw_stdio_servers:
        result = adapter.configure_mcp_server(server_ref, server_info_cache=raw_stdio_servers)
        assert result is True

    assert captured_keys == ["@playwright/mcp", "@other/mcp"]


def test_safe_installer_passes_scoped_server_name_to_adapter():
    """Installer must preserve scoped dependency names as server_name."""
    mock_adapter = Mock()
    mock_adapter.configure_mcp_server.return_value = True
    mock_conflict_detector = Mock()
    mock_conflict_detector.check_server_exists.return_value = False

    with (
        patch("apm_cli.core.safe_installer.ClientFactory.create_client") as mock_factory,
        patch("apm_cli.core.safe_installer.MCPConflictDetector") as mock_detector_class,
    ):
        mock_factory.return_value = mock_adapter
        mock_detector_class.return_value = mock_conflict_detector
        installer = SafeMCPInstaller("copilot")

    summary = installer.install_servers(["@playwright/mcp"])

    assert summary.installed == ["@playwright/mcp"]
    mock_adapter.configure_mcp_server.assert_called_once_with(
        "@playwright/mcp", server_name="@playwright/mcp"
    )


def test_safe_installer_keeps_owner_repo_fallback_available():
    """Installer must not override owner/repo basename fallback keys."""
    mock_adapter = Mock()
    mock_adapter.configure_mcp_server.return_value = True
    mock_conflict_detector = Mock()
    mock_conflict_detector.check_server_exists.return_value = False

    with (
        patch("apm_cli.core.safe_installer.ClientFactory.create_client") as mock_factory,
        patch("apm_cli.core.safe_installer.MCPConflictDetector") as mock_detector_class,
    ):
        mock_factory.return_value = mock_adapter
        mock_detector_class.return_value = mock_conflict_detector
        installer = SafeMCPInstaller("copilot")

    summary = installer.install_servers(["microsoft/azure-devops-mcp"])

    assert summary.installed == ["microsoft/azure-devops-mcp"]
    mock_adapter.configure_mcp_server.assert_called_once_with("microsoft/azure-devops-mcp")
