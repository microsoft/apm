"""Unit tests for the MCP registry integration."""

import unittest
from unittest import mock

import requests

from apm_cli.registry.integration import RegistryIntegration
from apm_cli.utils import github_host


class TestRegistryIntegration(unittest.TestCase):
    """Test cases for the MCP registry integration."""

    def setUp(self):
        """Set up test fixtures."""
        self.integration = RegistryIntegration()

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.list_servers")
    def test_list_available_packages(self, mock_list_servers):
        """Test listing available packages."""
        # Mock response
        mock_list_servers.return_value = (
            [
                {
                    "id": "123",
                    "name": "server1",
                    "description": "Description 1",
                    "repository": {"url": f"https://{github_host.default_host()}/test/server1"},
                },
                {
                    "id": "456",
                    "name": "server2",
                    "description": "Description 2",
                    "repository": {"url": f"https://{github_host.default_host()}/test/server2"},
                },
            ],
            None,
        )

        # Call the method
        packages = self.integration.list_available_packages()

        # Assertions
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[0]["name"], "server1")
        self.assertEqual(packages[0]["id"], "123")
        self.assertEqual(
            packages[0]["repository"]["url"], f"https://{github_host.default_host()}/test/server1"
        )
        self.assertEqual(packages[1]["name"], "server2")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    def test_search_packages(self, mock_search_servers):
        """Test searching for packages."""
        # Mock response
        mock_search_servers.return_value = [
            {"id": "123", "name": "test-server", "description": "Test description"}
        ]

        # Call the method
        results = self.integration.search_packages("test")

        # Assertions
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "test-server")
        mock_search_servers.assert_called_once_with("test")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_get_package_info(self, mock_find_server_by_reference):
        """Test getting package information by ID."""
        # Mock response
        mock_find_server_by_reference.return_value = {
            "id": "123",
            "name": "test-server",
            "description": "Test server description",
            "repository": {
                "url": f"https://{github_host.default_host()}/test/test-server",
                "source": "github",
            },
            "version_detail": {
                "version": "1.0.0",
                "release_date": "2025-05-16T19:13:21Z",
                "is_latest": True,
            },
            "packages": [{"registry_name": "npm", "name": "test-package", "version": "1.0.0"}],
        }

        # Call the method
        package_info = self.integration.get_package_info("123")

        # Assertions
        self.assertEqual(package_info["name"], "test-server")
        self.assertEqual(package_info["description"], "Test server description")
        self.assertEqual(
            package_info["repository"]["url"],
            f"https://{github_host.default_host()}/test/test-server",
        )
        self.assertEqual(package_info["version_detail"]["version"], "1.0.0")
        self.assertEqual(package_info["packages"][0]["name"], "test-package")
        self.assertEqual(len(package_info["versions"]), 1)
        self.assertEqual(package_info["versions"][0]["version"], "1.0.0")
        mock_find_server_by_reference.assert_called_once_with("123")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_get_package_info_by_name(self, mock_find_server_by_reference):
        """Test getting package information by name when ID fails."""
        # Mock find_server_by_reference to return server info
        mock_find_server_by_reference.return_value = {
            "id": "123",
            "name": "test-server",
            "description": "Test description",
            "version_detail": {"version": "1.0.0"},
        }

        # Call the method
        result = self.integration.get_package_info("test-server")

        # Assertions
        self.assertEqual(result["name"], "test-server")
        mock_find_server_by_reference.assert_called_once_with("test-server")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.find_server_by_reference")
    def test_get_package_info_not_found(self, mock_find_server_by_reference):
        """Test error handling when package is not found."""
        # Mock find_server_by_reference to return None
        mock_find_server_by_reference.return_value = None

        # Call the method and assert it raises a ValueError
        with self.assertRaises(ValueError):
            self.integration.get_package_info("non-existent")

    @mock.patch("apm_cli.registry.integration.RegistryIntegration.get_package_info")
    def test_get_latest_version(self, mock_get_package_info):
        """Test getting the latest version of a package."""
        # Test with version_detail
        mock_get_package_info.return_value = {
            "version_detail": {"version": "2.0.0", "is_latest": True}
        }

        version = self.integration.get_latest_version("test-package")
        self.assertEqual(version, "2.0.0")

        # Test with packages list
        mock_get_package_info.return_value = {"packages": [{"name": "test", "version": "1.5.0"}]}

        version = self.integration.get_latest_version("test-package")
        self.assertEqual(version, "1.5.0")

        # Test with versions list (backward compatibility)
        mock_get_package_info.return_value = {
            "versions": [{"version": "1.0.0"}, {"version": "1.1.0"}]
        }

        version = self.integration.get_latest_version("test-package")
        self.assertEqual(version, "1.1.0")

        # Test with no versions
        mock_get_package_info.return_value = {}
        with self.assertRaises(ValueError):
            self.integration.get_latest_version("test-package")


class TestMCPServerOperationsValidation(unittest.TestCase):
    """Tests for MCPServerOperations.validate_servers_exist resilience."""

    def _make_ops(self):
        """Create an MCPServerOperations with a mocked registry client.

        Defaults ``_is_custom_url`` to False so existing assume-valid tests
        do not accidentally trip the new fail-closed path. Tests that need
        the override behaviour set it explicitly.
        """
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = mock.MagicMock()
        ops.registry_client._is_custom_url = False
        return ops

    def test_valid_server(self):
        """Server found in registry → valid."""
        ops = self._make_ops()
        ops.registry_client.find_server_by_reference.return_value = {"id": "abc", "name": "srv"}

        valid, invalid = ops.validate_servers_exist(["io.github.test/srv"])
        self.assertEqual(valid, ["io.github.test/srv"])
        self.assertEqual(invalid, [])

    def test_missing_server(self):
        """Server not in registry (None) → invalid."""
        ops = self._make_ops()
        ops.registry_client.find_server_by_reference.return_value = None

        valid, invalid = ops.validate_servers_exist(["io.github.test/no-such"])
        self.assertEqual(valid, [])
        self.assertEqual(invalid, ["io.github.test/no-such"])

    def test_network_error_assumes_valid(self):
        """Transient network error on default registry → assume server valid (not invalid)."""
        ops = self._make_ops()
        # Default registry: not a user-configured override.
        ops.registry_client._is_custom_url = False
        ops.registry_client.find_server_by_reference.side_effect = requests.ConnectionError("flaky")

        valid, invalid = ops.validate_servers_exist(["io.github.test/flaky-srv"])
        self.assertEqual(valid, ["io.github.test/flaky-srv"])
        self.assertEqual(invalid, [])

    def test_network_error_fatal_on_custom_registry(self):
        """When MCP_REGISTRY_URL override is active, network errors are fatal (#814)."""
        ops = self._make_ops()
        ops.registry_client._is_custom_url = True
        ops.registry_client.registry_url = "https://internal.example.com"
        ops.registry_client.find_server_by_reference.side_effect = requests.ConnectionError("boom")

        with self.assertRaises(RuntimeError) as cm:
            ops.validate_servers_exist(["io.github.test/srv"])
        msg = str(cm.exception)
        self.assertIn("internal.example.com", msg)
        self.assertIn("MCP_REGISTRY_URL", msg)

    def test_mixed_results(self):
        """Mix of found, missing, and errored servers."""
        ops = self._make_ops()
        ops.registry_client._is_custom_url = False

        def side_effect(ref):
            if ref == "found":
                return {"id": "1", "name": "found"}
            if ref == "missing":
                return None
            raise requests.Timeout("timeout")

        ops.registry_client.find_server_by_reference.side_effect = side_effect

        valid, invalid = ops.validate_servers_exist(["found", "missing", "flaky"])
        self.assertEqual(sorted(valid), ["flaky", "found"])
        self.assertEqual(invalid, ["missing"])

    def test_check_servers_needing_installation_reads_each_runtime_once(self):
        """Installed server IDs are cached per runtime across server checks."""
        ops = self._make_ops()

        def find_server(ref):
            return {"id": f"id-{ref}", "name": ref}

        ops.registry_client.find_server_by_reference.side_effect = find_server
        ops._get_installed_server_ids = mock.MagicMock(
            side_effect=[
                {"id-s1"},
                set(),
            ]
        )

        result = ops.check_servers_needing_installation(
            ["codex", "cursor"],
            ["s1", "s2"],
        )

        self.assertEqual(sorted(result), ["s1", "s2"])
        self.assertEqual(ops._get_installed_server_ids.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in ops._get_installed_server_ids.call_args_list],
            [["codex"], ["cursor"]],
        )

    @mock.patch("apm_cli.factory.ClientFactory.create_client")
    def test_get_installed_server_ids_reads_vscode_servers_key(self, mock_create_client):
        """VS Code installed IDs should be read from .vscode/mcp.json's servers key."""
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations.__new__(MCPServerOperations)
        mock_client = mock.MagicMock()
        mock_client.get_current_config.return_value = {
            "servers": {
                "example": {"id": "server-123"},
            }
        }
        mock_create_client.return_value = mock_client

        installed = ops._get_installed_server_ids(["vscode"])

        self.assertEqual(installed, {"server-123"})


class TestCheckServersNeedingInstallation(unittest.TestCase):
    """Tests for MCPServerOperations.check_servers_needing_installation caching."""

    def _make_ops(self):
        from apm_cli.registry.operations import MCPServerOperations

        ops = MCPServerOperations.__new__(MCPServerOperations)
        ops.registry_client = mock.MagicMock()
        return ops

    def test_caches_runtime_lookups(self):
        """_get_installed_server_ids is called once per runtime, not once per server*runtime."""
        ops = self._make_ops()

        # 3 servers, 2 runtimes → old code would call 6 times, new code 2
        ops.registry_client.find_server_by_reference.side_effect = [
            {"id": "id-a", "name": "srv-a"},
            {"id": "id-b", "name": "srv-b"},
            {"id": "id-c", "name": "srv-c"},
        ]
        # Runtime "r1" has id-a installed, "r2" has none
        ops._get_installed_server_ids = mock.MagicMock(
            side_effect=[
                {"id-a"},  # r1
                set(),  # r2
            ]
        )

        result = ops.check_servers_needing_installation(
            target_runtimes=["r1", "r2"],
            server_references=["srv-a", "srv-b", "srv-c"],
        )

        # _get_installed_server_ids called exactly once per runtime
        self.assertEqual(ops._get_installed_server_ids.call_count, 2)
        ops._get_installed_server_ids.assert_any_call(
            ["r1"],
            project_root=None,
            user_scope=False,
        )
        ops._get_installed_server_ids.assert_any_call(
            ["r2"],
            project_root=None,
            user_scope=False,
        )

        # All three need installation because none are installed in *all* runtimes:
        #   srv-a: installed in r1 but missing from r2 → needs install
        #   srv-b: missing from r1 → needs install
        #   srv-c: missing from r1 → needs install
        self.assertEqual(sorted(result), ["srv-a", "srv-b", "srv-c"])

    def test_server_installed_everywhere_excluded(self):
        """A server installed in every target runtime is NOT returned."""
        ops = self._make_ops()

        ops.registry_client.find_server_by_reference.return_value = {"id": "id-x", "name": "srv-x"}
        ops._get_installed_server_ids = mock.MagicMock(return_value={"id-x"})

        result = ops.check_servers_needing_installation(
            target_runtimes=["r1"],
            server_references=["srv-x"],
        )

        self.assertEqual(result, [])

    def test_server_not_in_registry(self):
        """Server not found in registry is flagged for installation."""
        ops = self._make_ops()

        ops.registry_client.find_server_by_reference.return_value = None
        ops._get_installed_server_ids = mock.MagicMock(return_value=set())

        result = ops.check_servers_needing_installation(
            target_runtimes=["r1"],
            server_references=["unknown-srv"],
        )

        self.assertEqual(result, ["unknown-srv"])

    def test_registry_error_flags_for_installation(self):
        """Exception during registry lookup flags server for installation."""
        ops = self._make_ops()

        ops.registry_client.find_server_by_reference.side_effect = RuntimeError("boom")
        ops._get_installed_server_ids = mock.MagicMock(return_value=set())

        result = ops.check_servers_needing_installation(
            target_runtimes=["r1"],
            server_references=["err-srv"],
        )

        self.assertEqual(result, ["err-srv"])


if __name__ == "__main__":
    unittest.main()
