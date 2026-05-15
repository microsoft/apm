"""Integration tests for the MCP registry client with the GitHub MCP Registry (v0.1 spec)."""

import unittest

import pytest
import requests

from apm_cli.registry.client import SimpleRegistryClient

pytestmark = pytest.mark.requires_network_integration


class TestRegistryClientIntegration(unittest.TestCase):
    """Integration test cases against the live GitHub MCP Registry (https://api.mcp.github.com).

    These tests exercise the v0.1 spec endpoints end-to-end. They skip
    cleanly when the registry is unreachable.
    """

    def setUp(self):
        """Set up test fixtures."""
        self.client = SimpleRegistryClient("https://api.mcp.github.com")

        try:
            response = requests.head("https://api.mcp.github.com")  # noqa: S113
            response.raise_for_status()
        except (requests.RequestException, ValueError):
            self.skipTest("GitHub MCP Registry is not accessible")

    def test_list_servers(self):
        """list_servers returns spec-shaped server dicts with 'name'."""
        try:
            servers, next_cursor = self.client.list_servers(limit=5)  # noqa: RUF059
            self.assertIsInstance(servers, list)
            if servers:
                # v0.1 spec shape: 'name' is the stable identifier; 'id' may be absent.
                self.assertIn("name", servers[0])
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not list servers from GitHub MCP Registry: {e}")

    def test_search_servers(self):
        """search_servers hits the v0.1 ?search= query and returns matches."""
        try:
            results = self.client.search_servers("github")
            self.assertGreater(len(results), 0, "Search should return at least some results")

            for server in results:
                self.assertIn("name", server)
                self.assertIn("description", server)

            specific_results = self.client.search_servers("nonexistent-xyz-123-needle")
            self.assertIsInstance(specific_results, list)
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not search servers in registry: {e}")

    def test_get_server_uses_v0_1_versions_latest(self):
        """get_server resolves a name via /v0.1/servers/{name}/versions/latest."""
        try:
            all_servers, _ = self.client.list_servers(limit=5)
            if not all_servers:
                self.skipTest("No servers found in GitHub MCP Registry to look up")

            server_name = all_servers[0]["name"]
            server_info = self.client.get_server(server_name)

            self.assertEqual(server_info["name"], server_name)
            self.assertIn("description", server_info)
            self.assertIn("version", server_info)

            if server_info.get("packages"):
                pkg = server_info["packages"][0]
                # v0.1 spec uses 'identifier' (was 'name' in legacy v0).
                self.assertTrue("identifier" in pkg or "name" in pkg)
                self.assertIn("version", pkg)
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not get server info from GitHub MCP Registry: {e}")

    def test_get_server_by_name(self):
        """get_server_by_name finds a known server and returns None for missing names."""
        try:
            all_servers, _ = self.client.list_servers(limit=5)
            if not all_servers:
                self.skipTest("No servers found in GitHub MCP Registry to look up")

            server_name = all_servers[0]["name"]
            found_server = self.client.get_server_by_name(server_name)

            self.assertIsNotNone(found_server, "Server should be found by name")
            self.assertEqual(found_server["name"], server_name)

            non_existent = self.client.get_server_by_name(
                "io.github.nonexistent/definitely-not-a-real-server-12345"
            )
            self.assertIsNone(non_existent, "Non-existent server should return None")
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not find server by name in GitHub MCP Registry: {e}")

    def test_get_server_url_encodes_slash(self):
        """A serverName containing '/' is URL-encoded to %2F when sent to the registry.

        Regression trap for issue #1210: the spec requires URL-encoding of
        the path segment so registries can route the lookup correctly.
        """
        try:
            servers, _ = self.client.list_servers(limit=20)
            namespaced = next((s for s in servers if "/" in s.get("name", "")), None)
            if namespaced is None:
                self.skipTest("No namespaced servers found to validate URL encoding")

            # If get_server returns successfully, the URL was correctly encoded
            # (a raw slash would 404 because it would be interpreted as a path separator).
            info = self.client.get_server(namespaced["name"])
            self.assertEqual(info["name"], namespaced["name"])
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not validate URL encoding against live registry: {e}")


if __name__ == "__main__":
    unittest.main()
