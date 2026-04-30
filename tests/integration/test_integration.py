"""Integration tests for APM."""

import gc
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch  # noqa: F401

from apm_cli.core.operations import install_package
from apm_cli.factory import ClientFactory, PackageManagerFactory  # noqa: F401


def safe_rmdir(path):
    """Safely remove a directory with retry logic for Windows.

    Args:
        path (str): Path to directory to remove
    """
    try:
        shutil.rmtree(path)
    except PermissionError:
        # On Windows, give time for any lingering processes to release the lock
        time.sleep(0.5)
        gc.collect()  # Force garbage collection to release file handles
        try:
            shutil.rmtree(path)
        except PermissionError as e:
            print(f"Failed to remove directory {path}: {e}")
            # Continue without failing the test
            pass


class TestIntegration(unittest.TestCase):
    """Integration test cases for APM."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = self.temp_dir.name
        self.temp_path = os.path.join(self.temp_dir_path, "settings.json")

        # Create a temporary settings file
        with open(self.temp_path, "w") as f:
            json.dump({}, f)

    def tearDown(self):
        """Tear down test fixtures."""
        # Force garbage collection to release file handles
        gc.collect()

        # Give time for Windows to release locks
        if sys.platform == "win32":
            time.sleep(0.1)

        # First, try the standard cleanup
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            # If standard cleanup fails on Windows, use our safe_rmdir function
            if hasattr(self, "temp_dir_path") and os.path.exists(self.temp_dir_path):
                safe_rmdir(self.temp_dir_path)

    @patch("apm_cli.adapters.client.vscode.VSCodeClientAdapter.get_config_path")
    @patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_install_package_integration(self, mock_find_server, mock_get_path):
        """Test installing a package and updating client configuration."""
        mock_get_path.return_value = self.temp_path

        # Mock the registry client to return a test server configuration
        mock_find_server.return_value = {
            "id": "test-id",
            "name": "test-package",
            "packages": [{"name": "test-package", "runtime_hint": "npx"}],
        }

        # Install a package
        result = install_package("vscode", "test-package", "1.0.0")
        self.assertTrue(result)

        # Verify the client configuration was updated
        with open(self.temp_path) as f:
            config = json.load(f)

        # Should have servers entry and should NOT have the deprecated mcp.package entry
        self.assertIn("servers", config)
        self.assertIn("test-package", config["servers"])
        self.assertFalse("mcp.package.test-package.enabled" in config)


if __name__ == "__main__":
    unittest.main()
