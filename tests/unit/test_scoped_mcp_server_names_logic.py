"""Regression tests for scoped MCP server config keys (pure logic cases)."""

from unittest.mock import Mock, patch

import pytest

from apm_cli.core.safe_installer import SafeMCPInstaller

pytestmark = pytest.mark.unit


def test_safe_installer_passes_scoped_server_name_to_adapter():
    """Installer must preserve scoped dependency names as server_name."""
    mock_adapter = Mock()
    mock_adapter.written_mcp_hashes = {}
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
    mock_adapter.written_mcp_hashes = {}
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


def test_safe_installer_delegates_self_defined_server_name_to_adapter():
    """Installer must delegate key resolution for self-defined servers to adapters."""
    mock_adapter = Mock()
    mock_adapter.configure_mcp_server.return_value = True
    mock_conflict_detector = Mock()
    mock_conflict_detector.check_server_exists.return_value = False

    server_cache = {
        "com.azure.dev/mcp-remote": {
            "name": "com.azure.dev/mcp-remote",
            "is_self_defined": True,
        }
    }

    with (
        patch("apm_cli.core.safe_installer.ClientFactory.create_client") as mock_factory,
        patch("apm_cli.core.safe_installer.MCPConflictDetector") as mock_detector_class,
    ):
        mock_factory.return_value = mock_adapter
        mock_detector_class.return_value = mock_conflict_detector
        installer = SafeMCPInstaller("copilot")

    summary = installer.install_servers(
        ["com.azure.dev/mcp-remote"], server_info_cache=server_cache
    )

    assert summary.installed == ["com.azure.dev/mcp-remote"]
    mock_adapter.configure_mcp_server.assert_called_once_with(
        "com.azure.dev/mcp-remote",
        server_info_cache=server_cache,
    )
