"""Unit tests for the default MCP package manager."""

import unittest
from unittest.mock import MagicMock, patch

from apm_cli.adapters.package_manager.default_manager import DefaultMCPPackageManager


class TestDefaultMCPPackageManager(unittest.TestCase):
    """Test cases for the default MCP package manager."""

    def setUp(self):
        """Set up test fixtures."""
        self.package_manager = DefaultMCPPackageManager()

    @patch("apm_cli.factory.ClientFactory.create_client")
    @patch("apm_cli.config.get_default_client")
    def test_install(self, mock_get_default_client, mock_create_client):
        """Test installing a package."""
        # Setup mocks
        mock_client = MagicMock()
        mock_client.configure_mcp_server.return_value = True
        mock_create_client.return_value = mock_client
        mock_get_default_client.return_value = "vscode"

        # Test regular install
        result = self.package_manager.install("test-package")
        self.assertTrue(result)
        mock_client.configure_mcp_server.assert_called_with("test-package", "test-package", True)

        # Test install with version
        result = self.package_manager.install("test-package", "1.0.0")
        self.assertTrue(result)
        mock_client.configure_mcp_server.assert_called_with("test-package", "test-package", True)

    @patch("apm_cli.factory.ClientFactory.create_client")
    @patch("apm_cli.config.get_default_client")
    def test_uninstall(self, mock_get_default_client, mock_create_client):
        """Test uninstalling a package."""
        # Setup mocks
        mock_client = MagicMock()
        mock_client.get_current_config.return_value = {
            "servers": {"test-package": {"type": "stdio", "command": "npx"}}
        }
        mock_client.update_config.return_value = True
        mock_create_client.return_value = mock_client
        mock_get_default_client.return_value = "vscode"

        # Test uninstall
        result = self.package_manager.uninstall("test-package")
        self.assertTrue(result)
        mock_client.update_config.assert_called_once()

    @patch("apm_cli.factory.ClientFactory.create_client")
    @patch("apm_cli.config.get_default_client")
    def test_list_installed(self, mock_get_default_client, mock_create_client):
        """Test listing installed packages."""
        # Setup mocks
        mock_client = MagicMock()
        mock_client.get_current_config.return_value = {"servers": {"server1": {}, "server2": {}}}
        mock_create_client.return_value = mock_client
        mock_get_default_client.return_value = "vscode"

        # Test list_installed
        packages = self.package_manager.list_installed()
        self.assertIsInstance(packages, list)
        self.assertEqual(set(packages), {"server1", "server2"})

    @patch("apm_cli.registry.integration.RegistryIntegration.search_packages")
    def test_search(self, mock_search_packages):
        """Test searching for packages."""
        # Setup mocks
        mock_search_packages.return_value = [
            {"id": "id1", "name": "test-package-1"},
            {"id": "id2", "name": "another-test"},
        ]

        # Test search
        results = self.package_manager.search("test")
        self.assertIsInstance(results, list)
        self.assertEqual(results, ["id1", "id2"])


if __name__ == "__main__":
    unittest.main()
