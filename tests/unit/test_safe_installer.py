"""Tests for safe MCP installer functionality."""

import unittest
from unittest.mock import Mock, patch

from apm_cli.core.safe_installer import InstallationSummary, SafeMCPInstaller


class TestSafeMCPInstaller(unittest.TestCase):
    """Test suite for safe MCP installer."""

    def setUp(self):
        """Set up test fixtures."""
        # Mock the factory and adapter
        self.mock_adapter = Mock()
        self.mock_conflict_detector = Mock()

        with (
            patch("apm_cli.core.safe_installer.ClientFactory.create_client") as mock_factory,
            patch("apm_cli.core.safe_installer.MCPConflictDetector") as mock_detector_class,
        ):
            mock_factory.return_value = self.mock_adapter
            mock_detector_class.return_value = self.mock_conflict_detector

            self.installer = SafeMCPInstaller("copilot")

    def test_install_new_server(self):
        """Test installing a new server that doesn't conflict."""
        # Setup mocks
        self.mock_conflict_detector.check_server_exists.return_value = False
        self.mock_adapter.configure_mcp_server.return_value = True

        # Install server
        summary = self.installer.install_servers(["github"])

        # Verify results
        self.assertEqual(len(summary.installed), 1)
        self.assertEqual(len(summary.skipped), 0)
        self.assertEqual(len(summary.failed), 0)
        self.assertEqual(summary.installed[0], "github")

        # Verify adapter was called
        self.mock_adapter.configure_mcp_server.assert_called_once_with("github")

    def test_skip_existing_server(self):
        """Test skipping server that already exists."""
        # Setup mocks
        self.mock_conflict_detector.check_server_exists.return_value = True

        # Install server
        summary = self.installer.install_servers(["github"])

        # Verify results
        self.assertEqual(len(summary.installed), 0)
        self.assertEqual(len(summary.skipped), 1)
        self.assertEqual(len(summary.failed), 0)
        self.assertEqual(summary.skipped[0]["server"], "github")
        self.assertEqual(summary.skipped[0]["reason"], "already configured")

        # Verify adapter was not called
        self.mock_adapter.configure_mcp_server.assert_not_called()

    def test_handle_configuration_failure(self):
        """Test handling server configuration failure."""
        # Setup mocks
        self.mock_conflict_detector.check_server_exists.return_value = False
        self.mock_adapter.configure_mcp_server.return_value = False

        # Install server
        summary = self.installer.install_servers(["github"])

        # Verify results
        self.assertEqual(len(summary.installed), 0)
        self.assertEqual(len(summary.skipped), 0)
        self.assertEqual(len(summary.failed), 1)
        self.assertEqual(summary.failed[0]["server"], "github")
        self.assertEqual(summary.failed[0]["reason"], "configuration failed")

    def test_handle_configuration_exception(self):
        """Test handling exception during server configuration."""
        # Setup mocks
        self.mock_conflict_detector.check_server_exists.return_value = False
        self.mock_adapter.configure_mcp_server.side_effect = Exception("Network error")

        # Install server
        summary = self.installer.install_servers(["github"])

        # Verify results
        self.assertEqual(len(summary.installed), 0)
        self.assertEqual(len(summary.skipped), 0)
        self.assertEqual(len(summary.failed), 1)
        self.assertEqual(summary.failed[0]["server"], "github")
        self.assertEqual(summary.failed[0]["reason"], "Network error")

    def test_mixed_installation_results(self):
        """Test installation with mixed results."""

        # Setup mocks for different servers
        def mock_check_exists(server_ref):
            return server_ref == "existing-server"

        def mock_configure(server_ref):
            if server_ref == "failing-server":
                raise Exception("Configuration failed")
            return server_ref == "new-server"

        self.mock_conflict_detector.check_server_exists.side_effect = mock_check_exists
        self.mock_adapter.configure_mcp_server.side_effect = mock_configure

        # Install multiple servers
        servers = ["new-server", "existing-server", "failing-server"]
        summary = self.installer.install_servers(servers)

        # Verify results
        self.assertEqual(len(summary.installed), 1)
        self.assertEqual(len(summary.skipped), 1)
        self.assertEqual(len(summary.failed), 1)

        self.assertEqual(summary.installed[0], "new-server")
        self.assertEqual(summary.skipped[0]["server"], "existing-server")
        self.assertEqual(summary.failed[0]["server"], "failing-server")

    def test_check_conflicts_only(self):
        """Test conflict checking without installation."""

        # Setup mock
        def mock_get_conflict_summary(server_ref):
            return {
                "exists": server_ref == "existing-server",
                "canonical_name": f"canonical-{server_ref}",
                "conflicting_servers": [],
            }

        self.mock_conflict_detector.get_conflict_summary.side_effect = mock_get_conflict_summary

        # Check conflicts
        conflicts = self.installer.check_conflicts_only(["new-server", "existing-server"])

        # Verify results
        self.assertFalse(conflicts["new-server"]["exists"])
        self.assertTrue(conflicts["existing-server"]["exists"])
        self.assertEqual(conflicts["new-server"]["canonical_name"], "canonical-new-server")
        self.assertEqual(
            conflicts["existing-server"]["canonical_name"], "canonical-existing-server"
        )


class TestInstallationSummary(unittest.TestCase):
    """Test suite for installation summary."""

    def test_empty_summary(self):
        """Test empty installation summary."""
        summary = InstallationSummary()

        self.assertEqual(len(summary.installed), 0)
        self.assertEqual(len(summary.skipped), 0)
        self.assertEqual(len(summary.failed), 0)
        self.assertFalse(summary.has_any_changes())

    def test_add_operations(self):
        """Test adding different types of operations."""
        summary = InstallationSummary()

        summary.add_installed("server1")
        summary.add_skipped("server2", "already exists")
        summary.add_failed("server3", "network error")

        self.assertEqual(len(summary.installed), 1)
        self.assertEqual(len(summary.skipped), 1)
        self.assertEqual(len(summary.failed), 1)
        self.assertTrue(summary.has_any_changes())

        self.assertEqual(summary.installed[0], "server1")
        self.assertEqual(summary.skipped[0]["server"], "server2")
        self.assertEqual(summary.skipped[0]["reason"], "already exists")
        self.assertEqual(summary.failed[0]["server"], "server3")
        self.assertEqual(summary.failed[0]["reason"], "network error")

    def test_has_changes_with_skipped_only(self):
        """Test has_any_changes with only skipped items."""
        summary = InstallationSummary()
        summary.add_skipped("server1", "already exists")

        # Skipped items don't count as changes
        self.assertFalse(summary.has_any_changes())

    def test_has_changes_with_installed(self):
        """Test has_any_changes with installed items."""
        summary = InstallationSummary()
        summary.add_installed("server1")

        self.assertTrue(summary.has_any_changes())

    def test_has_changes_with_failed(self):
        """Test has_any_changes with failed items."""
        summary = InstallationSummary()
        summary.add_failed("server1", "error")

        self.assertTrue(summary.has_any_changes())


if __name__ == "__main__":
    unittest.main()
