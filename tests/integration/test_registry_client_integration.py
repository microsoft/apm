"""Integration tests for the MCP registry client with GitHub MCP Registry."""

import os  # noqa: F401
import unittest

import requests

from apm_cli.registry.client import SimpleRegistryClient


class TestRegistryClientIntegration(unittest.TestCase):
    """Integration test cases for the MCP registry client with the GitHub MCP Registry."""

    def setUp(self):
        """Set up test fixtures."""
        # Use the GitHub MCP Registry for integration tests
        self.client = SimpleRegistryClient("https://api.mcp.github.com")

        # Skip tests if we can't reach the registry
        try:
            response = requests.head("https://api.mcp.github.com")  # noqa: S113
            response.raise_for_status()
        except (requests.RequestException, ValueError):
            self.skipTest("GitHub MCP Registry is not accessible")

    def test_list_servers(self):
        """Test listing servers from the GitHub MCP Registry."""
        try:
            servers, next_cursor = self.client.list_servers()  # noqa: RUF059
            self.assertIsInstance(servers, list)
            # We don't know exactly what servers will be in the demo registry,
            # but we can check that the structure is correct
            if servers:
                self.assertIn("name", servers[0])
                self.assertIn("id", servers[0])
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not list servers from GitHub MCP Registry: {e}")

    def test_search_servers(self):
        """Test searching for servers in the registry using the search API."""
        try:
            # Test search with a common term like "github"
            results = self.client.search_servers("github")

            # We should find some results
            self.assertGreater(len(results), 0, "Search should return at least some results")

            # Each result should have basic server structure
            for server in results:
                self.assertIn("name", server)
                self.assertIn("id", server)
                self.assertIn("description", server)

            # Test that search actually filters (empty search should return fewer or different results)
            # Note: We can't guarantee behavior of empty searches, so we test with a specific term
            specific_results = self.client.search_servers("universal")
            # The results should be a list (could be empty if no matches)
            self.assertIsInstance(specific_results, list)

        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not search servers in registry: {e}")

    def test_get_server_info(self):
        """Test getting server information from the GitHub MCP Registry."""
        try:
            # First, get all servers to find one to get info about
            all_servers, _ = self.client.list_servers()
            if not all_servers:
                self.skipTest("No servers found in GitHub MCP Registry to get info about")

            # Get info about the first server
            server_id = all_servers[0]["id"]
            server_info = self.client.get_server_info(server_id)

            # Check that we got the expected server info
            self.assertIn("name", server_info)
            self.assertEqual(server_info["id"], server_id)
            self.assertIn("description", server_info)

            # Check for version_detail
            self.assertIn("version_detail", server_info)
            if "version_detail" in server_info:
                self.assertIn("version", server_info["version_detail"])

            # Check for packages if available
            if server_info.get("packages"):
                pkg = server_info["packages"][0]
                self.assertIn("name", pkg)
                self.assertIn("version", pkg)
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not get server info from GitHub MCP Registry: {e}")

    def test_get_server_by_name(self):
        """Test finding a server by name."""
        try:
            # First, get all servers to find one to look up
            all_servers, _ = self.client.list_servers()
            if not all_servers:
                self.skipTest("No servers found in GitHub MCP Registry to look up")

            # Try to find the first server by name
            server_name = all_servers[0]["name"]
            found_server = self.client.get_server_by_name(server_name)

            # Check that we found the expected server
            self.assertIsNotNone(found_server, "Server should be found by name")
            self.assertEqual(found_server["name"], server_name)

            # Try with a non-existent name
            non_existent = self.client.get_server_by_name("non-existent-server-name-12345")
            self.assertIsNone(non_existent, "Non-existent server should return None")
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not find server by name in GitHub MCP Registry: {e}")

    def test_specific_real_servers(self):
        """Test integration with specific real servers from the GitHub MCP Registry."""
        # Test specific server IDs from the GitHub MCP Registry
        universal_server_id = "cb84de60-6710-40eb-8cb3-a350ce27c34e"  # Universal MCP (pip runtime)
        docker_server_id = "52dd9765-6aea-476a-9338-5ffe1ddbefc5"  # it-tools (docker runtime)
        npx_server_id = "f3432bd2-9c05-4b27-b0f4-f4e7a83dbf66"  # deepseek-mcp-server (npx runtime)

        # Set to collect different runtime types we encounter
        runtime_types = set()

        # Test the Universal MCP server (pip runtime)
        try:
            universal_server = self.client.get_server_info(universal_server_id)

            # Validate basic server information
            self.assertEqual(universal_server["id"], universal_server_id)
            self.assertIn("name", universal_server)
            self.assertIn("description", universal_server)

            # Validate repository information
            self.assertIn("repository", universal_server)
            self.assertIn("url", universal_server["repository"])
            self.assertIn("source", universal_server["repository"])

            # Validate version details
            self.assertIn("version_detail", universal_server)
            self.assertIn("version", universal_server["version_detail"])

            # Validate it has package information
            self.assertIn("packages", universal_server)
            self.assertGreater(len(universal_server["packages"]), 0)

            # Validate package details
            package = universal_server["packages"][0]
            self.assertIn("name", package)
            self.assertIn("version", package)

            if "runtime_hint" in package:
                runtime_types.add(package["runtime_hint"])

            # Test finding by name
            universal_name = universal_server["name"]
            found_by_name = self.client.get_server_by_name(universal_name)
            self.assertIsNotNone(found_by_name)
            self.assertEqual(found_by_name["id"], universal_server_id)
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not test Universal MCP server: {e}")

        # Test the Docker server (docker runtime)
        try:
            docker_server = self.client.get_server_info(docker_server_id)

            # Validate basic server information
            self.assertEqual(docker_server["id"], docker_server_id)
            self.assertIn("name", docker_server)
            self.assertIn("description", docker_server)

            # Validate repository information
            self.assertIn("repository", docker_server)
            self.assertIn("url", docker_server["repository"])

            # Validate it has package information if available
            if docker_server.get("packages"):
                package = docker_server["packages"][0]
                self.assertIn("name", package)
                self.assertIn("version", package)

                if "runtime_hint" in package:
                    runtime_types.add(package["runtime_hint"])
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not test Docker MCP server: {e}")

        # Test the NPX server (npx runtime)
        try:
            npx_server = self.client.get_server_info(npx_server_id)

            # Validate basic server information
            self.assertEqual(npx_server["id"], npx_server_id)
            self.assertIn("name", npx_server)
            self.assertIn("description", npx_server)

            # Validate it has package information if available
            if npx_server.get("packages"):
                package = npx_server["packages"][0]
                self.assertIn("name", package)
                self.assertIn("version", package)

                if "runtime_hint" in package:
                    runtime_types.add(package["runtime_hint"])
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not test NPX MCP server: {e}")

        # Try to find a server with another runtime type for more diversity
        try:
            # Search for servers with different runtime types
            servers, _ = self.client.list_servers(limit=30)

            for server in servers:
                server_id = server["id"]
                if server_id not in [universal_server_id, docker_server_id, npx_server_id]:
                    try:
                        server_info = self.client.get_server_info(server_id)

                        if server_info.get("packages"):
                            for package in server_info["packages"]:
                                if (
                                    "runtime_hint" in package
                                    and package["runtime_hint"] not in runtime_types
                                ):
                                    runtime_types.add(package["runtime_hint"])

                                    # Validate we can get basic info for this server type
                                    self.assertIn("name", server_info)
                                    self.assertIn("description", server_info)
                                    self.assertIn("id", server_info)

                                    # If we found at least 3 different runtime types, we've validated enough diversity
                                    if len(runtime_types) >= 3:
                                        break
                    except (requests.RequestException, ValueError):
                        # Skip servers that can't be accessed
                        continue

                    # If we found at least 3 different runtime types, we've validated enough diversity
                    if len(runtime_types) >= 3:
                        break

            # We should have found at least 2 different runtime types from our test servers
            self.assertGreaterEqual(
                len(runtime_types),
                2,
                f"Expected to find at least 2 different runtime types, found: {runtime_types}",
            )
        except (requests.RequestException, ValueError) as e:
            self.skipTest(f"Could not test servers with different runtime types: {e}")


if __name__ == "__main__":
    unittest.main()
