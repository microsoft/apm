"""Unit tests for the MCP registry client."""

import os
import unittest
import warnings
from unittest import mock
from urllib.parse import urlparse

import requests

from apm_cli.registry.client import SimpleRegistryClient
from apm_cli.utils import github_host


class TestSimpleRegistryClient(unittest.TestCase):
    """Test cases for the MCP registry client."""

    def setUp(self):
        """Set up test fixtures."""
        self.client = SimpleRegistryClient()

    @mock.patch("requests.Session.get")
    def test_list_servers(self, mock_get):
        """Test listing servers from the registry under the v0.1 spec."""
        mock_response = mock.Mock()
        mock_response.json.return_value = {
            "servers": [
                {"server": {"name": "io.github.foo/server1", "description": "Description 1"}},
                {"server": {"name": "io.github.foo/server2", "description": "Description 2"}},
            ],
            "metadata": {"nextCursor": "next-page-token", "count": 2},
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        servers, next_cursor = self.client.list_servers()

        self.assertEqual(len(servers), 2)
        self.assertEqual(servers[0]["name"], "io.github.foo/server1")
        self.assertEqual(servers[1]["name"], "io.github.foo/server2")
        self.assertEqual(next_cursor, "next-page-token")
        mock_get.assert_called_once_with(
            f"{self.client.registry_url}/v0.1/servers",
            params={"limit": 100},
            timeout=self.client._timeout,
        )

    @mock.patch("requests.Session.get")
    def test_list_servers_with_pagination(self, mock_get):
        """Test listing servers with pagination parameters under the v0.1 spec."""
        mock_response = mock.Mock()
        mock_response.json.return_value = {"servers": [], "metadata": {}}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        self.client.list_servers(limit=10, cursor="page-token")

        mock_get.assert_called_once_with(
            f"{self.client.registry_url}/v0.1/servers",
            params={"limit": 10, "cursor": "page-token"},
            timeout=self.client._timeout,
        )

    @mock.patch("requests.Session.get")
    def test_list_servers_reads_nextCursor_camelCase(self, mock_get):
        """Regression trap (#1210): metadata.nextCursor is the spec key."""
        mock_response = mock.Mock()
        mock_response.json.return_value = {
            "servers": [],
            "metadata": {"nextCursor": "spec-cursor"},
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        _, next_cursor = self.client.list_servers()
        self.assertEqual(next_cursor, "spec-cursor")

    @mock.patch("requests.Session.get")
    def test_list_servers_uses_v0_1_endpoint(self, mock_get):
        """Regression trap (#1210): URL must be /v0.1/, never /v0/."""
        mock_response = mock.Mock()
        mock_response.json.return_value = {"servers": [], "metadata": {}}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        self.client.list_servers()
        called_url = mock_get.call_args[0][0]
        parsed_path = urlparse(called_url).path
        self.assertEqual(parsed_path, "/v0.1/servers")
        self.assertNotEqual(parsed_path, "/v0/servers")

    @mock.patch("requests.Session.get")
    def test_search_servers(self, mock_get):
        """Test searching for servers under the v0.1 spec (?search= on /v0.1/servers)."""
        mock_response = mock.Mock()
        mock_response.json.return_value = {
            "servers": [
                {
                    "server": {
                        "name": "io.github.foo/test-server",
                        "description": "Test description",
                    }
                },
                {"server": {"name": "io.github.foo/server2", "description": "Another test"}},
            ]
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        results = self.client.search_servers("test")

        mock_get.assert_called_once_with(
            f"{self.client.registry_url}/v0.1/servers",
            params={"search": "test"},
            timeout=self.client._timeout,
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["name"], "io.github.foo/test-server")
        self.assertEqual(results[1]["name"], "io.github.foo/server2")

    @mock.patch("requests.Session.get")
    def test_search_servers_passes_full_reference(self, mock_get):
        """search_servers passes the full reference (no slug pre-trim) to the spec ?search="""
        mock_response = mock.Mock()
        mock_response.json.return_value = {"servers": []}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        self.client.search_servers("io.github.foo/bar")

        mock_get.assert_called_once_with(
            f"{self.client.registry_url}/v0.1/servers",
            params={"search": "io.github.foo/bar"},
            timeout=self.client._timeout,
        )

    @mock.patch("requests.Session.get")
    def test_get_server(self, mock_get):
        """Test getting server info under the v0.1 spec."""
        mock_response = mock.Mock()
        server_data = {
            "server": {
                "name": "io.github.foo/test-server",
                "description": "Test server description",
                "repository": {
                    "url": f"https://{github_host.default_host()}/foo/test-server",
                    "source": "github",
                    "id": "12345",
                },
                "version": "1.0.0",
                "packages": [
                    {
                        "registry_name": "npm",
                        "name": "test-package",
                        "version": "1.0.0",
                        "runtime_hint": "npx",
                    }
                ],
            }
        }
        mock_response.json.return_value = server_data
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        server_info = self.client.get_server("io.github.foo/test-server")

        self.assertEqual(server_info["name"], "io.github.foo/test-server")
        self.assertEqual(server_info["version"], "1.0.0")
        self.assertEqual(server_info["packages"][0]["name"], "test-package")
        mock_get.assert_called_once_with(
            f"{self.client.registry_url}/v0.1/servers/io.github.foo%2Ftest-server/versions/latest",
            timeout=self.client._timeout,
        )

    @mock.patch("requests.Session.get")
    def test_get_server_url_encodes_slash_in_name(self, mock_get):
        """Regression trap (#1210): slash in serverName must be URL-encoded as %2F."""
        mock_response = mock.Mock()
        mock_response.json.return_value = {"server": {"name": "io.github.github/github-mcp-server"}}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        self.client.get_server("io.github.github/github-mcp-server")

        called_url = mock_get.call_args[0][0]
        parsed_path = urlparse(called_url).path
        # serverName slash must be encoded as %2F so the registry routes the
        # name as a single path segment, not as nested servers/<owner>/<repo>.
        self.assertEqual(
            parsed_path,
            "/v0.1/servers/io.github.github%2Fgithub-mcp-server/versions/latest",
        )

    def test_get_server_rejects_invalid_name_shape(self):
        """get_server rejects names that don't match the MCP spec shape."""
        for bad in ["", "../etc/passwd", "https://evil/", "name with space", "a/b/c"]:
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    self.client.get_server(bad)

    @mock.patch("requests.Session.get")
    def test_get_server_404_raises_server_not_found(self, mock_get):
        """get_server wraps 404s in ServerNotFoundError carrying the registry URL."""
        from apm_cli.registry.client import ServerNotFoundError

        mock_response = mock.Mock()
        mock_response.status_code = 404
        http_error = requests.HTTPError("404 Not Found")
        http_error.response = mock_response
        mock_response.raise_for_status.side_effect = http_error
        mock_get.return_value = mock_response

        with self.assertRaises(ServerNotFoundError) as cm:
            self.client.get_server("io.github.foo/missing")
        msg = str(cm.exception)
        self.assertIn("io.github.foo/missing", msg)
        # Validate the registry URL appears in the error by parsing it out of
        # the message rather than substring-matching the raw URL (CodeQL rule
        # `py/incomplete-url-substring-sanitization`). Find the URL token that
        # carries a scheme and assert hostname / scheme equality.
        registry_parsed = urlparse(self.client.registry_url)
        url_tokens = [tok.strip(".,;'\")(") for tok in msg.split() if "://" in tok]
        parsed_tokens = [urlparse(tok) for tok in url_tokens]
        self.assertTrue(
            any(
                p.scheme == registry_parsed.scheme and p.hostname == registry_parsed.hostname
                for p in parsed_tokens
            ),
            f"Registry URL not present in error message: {msg!r}",
        )
        # ServerNotFoundError must be a ValueError subclass for legacy callers
        self.assertIsInstance(cm.exception, ValueError)

    def test_get_server_info_is_deprecated_shim(self):
        """get_server_info is a one-minor deprecation shim that forwards to get_server."""
        with mock.patch.object(self.client, "get_server") as mock_get_server:
            mock_get_server.return_value = {"name": "io.github.foo/bar"}
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = self.client.get_server_info("io.github.foo/bar")
            self.assertEqual(result, {"name": "io.github.foo/bar"})
            mock_get_server.assert_called_once_with("io.github.foo/bar")
            self.assertTrue(
                any(issubclass(w.category, DeprecationWarning) for w in caught),
                f"Expected DeprecationWarning, got {[w.category for w in caught]}",
            )

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.get_server")
    def test_get_server_by_name(self, mock_get_server, mock_search_servers):
        """Test finding a server by name using search API (v0.1 shape: name, no id)."""
        mock_search_servers.return_value = [
            {"name": "test-server"},
            {"name": "other-server"},
        ]
        server_data = {"name": "test-server", "description": "Test server"}
        mock_get_server.return_value = server_data

        result = self.client.get_server_by_name("test-server")

        self.assertEqual(result, server_data)
        mock_search_servers.assert_called_once_with("test-server")
        mock_get_server.assert_called_once_with("test-server")

        # Reset mocks for non-existent test
        mock_get_server.reset_mock()
        mock_search_servers.reset_mock()
        mock_search_servers.return_value = []

        result = self.client.get_server_by_name("non-existent")
        self.assertIsNone(result)
        mock_search_servers.assert_called_once_with("non-existent")
        mock_get_server.assert_not_called()

    def test_get_server_by_name_does_not_require_top_level_id(self):
        """Regression trap (#1210): lookup chain must work without top-level 'id'."""
        with (
            mock.patch.object(self.client, "search_servers") as mock_search,
            mock.patch.object(self.client, "get_server") as mock_get,
        ):
            mock_search.return_value = [{"name": "io.github.foo/bar"}]  # NO 'id' key
            mock_get.return_value = {"name": "io.github.foo/bar"}
            result = self.client.get_server_by_name("io.github.foo/bar")
            self.assertEqual(result, {"name": "io.github.foo/bar"})
            mock_get.assert_called_once_with("io.github.foo/bar")

    @mock.patch.dict(os.environ, {"MCP_REGISTRY_URL": "https://custom-registry.example.com"})
    def test_environment_variable_override(self):
        """Test overriding the registry URL with an environment variable."""
        client = SimpleRegistryClient()
        self.assertEqual(client.registry_url, "https://custom-registry.example.com")

        # Test explicit URL takes precedence over environment variable
        client = SimpleRegistryClient("https://explicit-url.example.com")
        self.assertEqual(client.registry_url, "https://explicit-url.example.com")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    def test_find_server_by_reference_uuid_input_returns_none(self, mock_search_servers):
        """The legacy UUID strategy is removed; UUID-shaped refs route to search and miss."""
        mock_search_servers.return_value = []
        result = self.client.find_server_by_reference("123e4567-e89b-12d3-a456-426614174000")
        self.assertIsNone(result)
        mock_search_servers.assert_called_once_with("123e4567-e89b-12d3-a456-426614174000")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.get_server")
    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    def test_find_server_by_reference_name_match(self, mock_search_servers, mock_get_server):
        """Test finding a server by exact name match (v0.1 shape)."""
        mock_search_servers.return_value = [
            {"name": "io.github.owner/repo-name"},
            {"name": "other-server"},
        ]
        server_data = {"name": "io.github.owner/repo-name", "description": "Test server"}
        mock_get_server.return_value = server_data

        result = self.client.find_server_by_reference("io.github.owner/repo-name")

        self.assertEqual(result, server_data)
        mock_search_servers.assert_called_once_with("io.github.owner/repo-name")
        mock_get_server.assert_called_once_with("io.github.owner/repo-name")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    def test_find_server_by_reference_name_not_found(self, mock_search_servers):
        """Test finding a server by name that doesn't exist in registry."""
        mock_search_servers.return_value = [
            {"name": "io.github.owner/different-repo"},
            {"name": "other-server"},
        ]

        result = self.client.find_server_by_reference("ghcr.io/github/github-mcp-server")

        self.assertIsNone(result)
        mock_search_servers.assert_called_once_with("ghcr.io/github/github-mcp-server")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.get_server")
    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    def test_find_server_by_reference_name_match_get_server_fails(
        self, mock_search_servers, mock_get_server
    ):
        """When get_server raises ValueError (e.g. ServerNotFoundError), return None."""
        mock_search_servers.return_value = [{"name": "test-server"}]
        mock_get_server.side_effect = ValueError("Server not found")

        result = self.client.find_server_by_reference("test-server")

        self.assertIsNone(result)
        mock_search_servers.assert_called_once_with("test-server")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.get_server")
    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    def test_find_server_by_reference_name_match_network_error_propagates(
        self, mock_search_servers, mock_get_server
    ):
        """Test that network errors in get_server propagate to the caller."""
        mock_search_servers.return_value = [{"name": "test-server"}]
        mock_get_server.side_effect = requests.ConnectionError("Network error")

        with self.assertRaises(requests.ConnectionError):
            self.client.find_server_by_reference("test-server")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    def test_find_server_by_reference_invalid_format(self, mock_search_servers):
        """Test finding a server with various invalid/edge case formats."""
        mock_search_servers.return_value = []

        test_cases = [
            "",
            "short",
            "123e4567-e89b-12d3-a456-426614174000-extra",
            "not-a-uuid-but-36-chars-long-string",
            "registry.io/very/long/path/name",
        ]

        for test_case in test_cases:
            with self.subTest(reference=test_case):
                result = self.client.find_server_by_reference(test_case)
                self.assertIsNone(result)

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.get_server")
    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    def test_find_server_by_reference_no_slug_collision(self, mock_search_servers, mock_get_server):
        """Test that qualified names don't collide on shared slugs (bug #165)."""
        mock_search_servers.return_value = [
            {"name": "com.supabase/mcp"},
            {"name": "microsoftdocs/mcp"},
        ]
        server_data = {"name": "microsoftdocs/mcp", "description": "MS Docs"}
        mock_get_server.return_value = server_data

        result = self.client.find_server_by_reference("microsoftdocs/mcp")

        self.assertEqual(result, server_data)
        mock_get_server.assert_called_once_with("microsoftdocs/mcp")

    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.get_server")
    @mock.patch("apm_cli.registry.client.SimpleRegistryClient.search_servers")
    def test_find_server_by_reference_qualified_no_match(
        self, mock_search_servers, mock_get_server
    ):
        """Test that a qualified name with no exact match returns None."""
        mock_search_servers.return_value = [
            {"name": "com.supabase/mcp"},
        ]

        result = self.client.find_server_by_reference("microsoftdocs/mcp")

        self.assertIsNone(result)
        mock_get_server.assert_not_called()

    def test_is_server_match_qualified_prevents_collision(self):
        """Test _is_server_match rejects different namespaces with same slug."""
        self.assertFalse(self.client._is_server_match("microsoftdocs/mcp", "com.supabase/mcp"))
        self.assertFalse(self.client._is_server_match("owner-a/server", "owner-b/server"))

    def test_is_server_match_unqualified_allows_slug(self):
        """Test _is_server_match still works for simple unqualified names."""
        self.assertTrue(
            self.client._is_server_match("github-mcp-server", "io.github.github/github-mcp-server")
        )

    def test_is_server_match_exact(self):
        """Test _is_server_match accepts exact full-name match."""
        self.assertTrue(self.client._is_server_match("microsoftdocs/mcp", "microsoftdocs/mcp"))

    def test_is_server_match_qualified_suffix_at_namespace_boundary(self):
        """Test that a qualified ref matches when it's a namespace-boundary suffix."""
        self.assertTrue(
            self.client._is_server_match(
                "github/github-mcp-server",
                "io.github.github/github-mcp-server",
            )
        )

    def test_is_server_match_qualified_suffix_no_boundary(self):
        """Qualified ref must NOT match when the suffix isn't at a '.' boundary."""
        # 'xgithub/server' ends with 'github/server' but not at a '.' boundary
        self.assertFalse(
            self.client._is_server_match(
                "github/server",
                "xgithub/server",
            )
        )


class TestSimpleRegistryClientValidation(unittest.TestCase):
    """URL validation at construction (#814).

    SimpleRegistryClient must reject malformed registry URLs at startup so
    misconfiguration surfaces immediately instead of producing cryptic HTTP
    failures later. Plaintext http:// is rejected by default; opt in via
    MCP_REGISTRY_ALLOW_HTTP=1.
    """

    def setUp(self):
        # Snapshot env vars touched by these tests so we always restore them.
        self._saved = {
            k: os.environ.get(k) for k in ("MCP_REGISTRY_URL", "MCP_REGISTRY_ALLOW_HTTP")
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_url_passes(self):
        c = SimpleRegistryClient()
        self.assertEqual(c.registry_url, "https://api.mcp.github.com")
        self.assertFalse(c._is_custom_url)

    def test_explicit_https_url_passes(self):
        c = SimpleRegistryClient("https://mcp.example.com")
        self.assertEqual(c.registry_url, "https://mcp.example.com")
        self.assertTrue(c._is_custom_url)

    def test_trailing_slash_and_whitespace_stripped(self):
        c = SimpleRegistryClient("  https://mcp.example.com/  ")
        self.assertEqual(c.registry_url, "https://mcp.example.com")

    def test_schemeless_url_rejected(self):
        with self.assertRaises(ValueError) as cm:
            SimpleRegistryClient("mcp.example.com")
        self.assertIn("MCP_REGISTRY_URL", str(cm.exception))
        self.assertIn("scheme://host", str(cm.exception))

    def test_http_url_rejected_without_opt_in(self):
        with self.assertRaises(ValueError) as cm:
            SimpleRegistryClient("http://mcp.example.com")
        self.assertIn("MCP_REGISTRY_ALLOW_HTTP", str(cm.exception))

    def test_http_url_accepted_with_allow_env(self):
        os.environ["MCP_REGISTRY_ALLOW_HTTP"] = "1"
        c = SimpleRegistryClient("http://mcp.example.com")
        self.assertEqual(c.registry_url, "http://mcp.example.com")
        self.assertTrue(c._is_custom_url)

    def test_unsupported_scheme_rejected(self):
        with self.assertRaises(ValueError) as cm:
            SimpleRegistryClient("ftp://mcp.example.com")
        self.assertIn("ftp", str(cm.exception))
        self.assertIn("only https://", str(cm.exception))

    def test_empty_env_var_treated_as_unset(self):
        os.environ["MCP_REGISTRY_URL"] = ""
        c = SimpleRegistryClient()
        self.assertEqual(c.registry_url, "https://api.mcp.github.com")
        self.assertFalse(c._is_custom_url)

    def test_whitespace_only_env_var_treated_as_unset(self):
        os.environ["MCP_REGISTRY_URL"] = "   "
        c = SimpleRegistryClient()
        self.assertEqual(c.registry_url, "https://api.mcp.github.com")
        self.assertFalse(c._is_custom_url)

    def test_env_var_override_marks_custom(self):
        os.environ["MCP_REGISTRY_URL"] = "https://internal.example.com/"
        c = SimpleRegistryClient()
        self.assertEqual(c.registry_url, "https://internal.example.com")
        self.assertTrue(c._is_custom_url)

    def test_env_var_invalid_rejected(self):
        os.environ["MCP_REGISTRY_URL"] = "not-a-url"
        with self.assertRaises(ValueError) as cm:
            SimpleRegistryClient()
        self.assertIn("MCP_REGISTRY_URL", str(cm.exception))

    def test_userinfo_stripped_from_registry_url(self):
        """SimpleRegistryClient must strip user:pass@ from the stored URL.

        Regression trap for the credential-leak path: if userinfo survives
        into ``self.registry_url``, ``ServerNotFoundError`` interpolates it
        into terminal output and CI logs.
        """
        c = SimpleRegistryClient("https://token:x-oauth@registry.corp.example.com/")
        parsed = urlparse(c.registry_url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "registry.corp.example.com")
        self.assertIsNone(parsed.username)
        self.assertIsNone(parsed.password)
        self.assertEqual(c.registry_url, "https://registry.corp.example.com")

    def test_userinfo_stripped_preserves_explicit_port(self):
        c = SimpleRegistryClient("https://user:pass@registry.corp.example.com:8443/")
        parsed = urlparse(c.registry_url)
        self.assertEqual(parsed.hostname, "registry.corp.example.com")
        self.assertEqual(parsed.port, 8443)
        self.assertIsNone(parsed.username)
        self.assertIsNone(parsed.password)


class TestNormalizeV01Package(unittest.TestCase):
    """Unit tests for the v0.1 -> snake_case package shape normalizer.

    Regression trap for #1210: registry returns camelCase keys per the
    v0.1 spec, but adapters in ``src/apm_cli/adapters/client/`` consume
    snake_case keys (``name``, ``runtime_hint``, ``package_arguments``,
    ...). Without the boundary normalizer, ``apm install`` produced
    ``npx -y None`` because the resolved package dict had no ``name``.
    """

    def test_normalize_v0_1_package_backfills_all_aliases(self):
        """Every v0.1 camelCase alias backfills its snake_case counterpart."""
        v0_1_package = {
            "identifier": "@modelcontextprotocol/server-fetch",
            "registryType": "npm",
            "registryBaseUrl": "https://registry.npmjs.org",
            "runtimeHint": "npx",
            "packageArguments": [{"value": "-y"}],
            "runtimeArguments": [{"value": "--no-install"}],
            "environmentVariables": [{"name": "DEBUG", "value": "1"}],
            "version": "1.0.0",
        }
        normalized = SimpleRegistryClient._normalize_v0_1_package(v0_1_package)

        self.assertEqual(normalized["name"], "@modelcontextprotocol/server-fetch")
        self.assertEqual(normalized["registry_name"], "npm")
        self.assertEqual(normalized["registry_base_url"], "https://registry.npmjs.org")
        self.assertEqual(normalized["runtime_hint"], "npx")
        self.assertEqual(normalized["package_arguments"], [{"value": "-y"}])
        self.assertEqual(normalized["runtime_arguments"], [{"value": "--no-install"}])
        self.assertEqual(normalized["environment_variables"], [{"name": "DEBUG", "value": "1"}])
        self.assertEqual(normalized["version"], "1.0.0")

        # camelCase keys are preserved (one-way bridge, not a rewrite).
        self.assertEqual(normalized["identifier"], "@modelcontextprotocol/server-fetch")
        self.assertEqual(normalized["runtimeHint"], "npx")

    def test_normalize_v0_1_package_preserves_existing_legacy_keys(self):
        """When both camelCase and snake_case are present, snake_case wins."""
        package = {
            "identifier": "v0.1-name",
            "name": "legacy-name",
            "runtimeHint": "v0.1-hint",
            "runtime_hint": "legacy-hint",
        }
        normalized = SimpleRegistryClient._normalize_v0_1_package(package)
        self.assertEqual(normalized["name"], "legacy-name")
        self.assertEqual(normalized["runtime_hint"], "legacy-hint")

    def test_normalize_v0_1_package_does_not_mutate_input(self):
        """Normalizer returns a shallow copy; the input dict is unchanged."""
        package = {"identifier": "foo", "runtimeHint": "npx"}
        SimpleRegistryClient._normalize_v0_1_package(package)
        self.assertNotIn("name", package)
        self.assertNotIn("runtime_hint", package)

    def test_normalize_v0_1_package_handles_partial_aliases(self):
        """Aliases backfill independently; missing v0.1 keys are no-ops."""
        package = {"identifier": "foo", "version": "1.0.0"}
        normalized = SimpleRegistryClient._normalize_v0_1_package(package)
        self.assertEqual(normalized["name"], "foo")
        self.assertNotIn("runtime_hint", normalized)
        self.assertNotIn("registry_name", normalized)

    def test_normalize_v0_1_package_returns_non_dict_unchanged(self):
        self.assertIsNone(SimpleRegistryClient._normalize_v0_1_package(None))
        self.assertEqual(SimpleRegistryClient._normalize_v0_1_package("not-a-dict"), "not-a-dict")

    def test_normalize_v0_1_server_normalizes_packages_list(self):
        """Server-level normalizer applies package normalization to each entry."""
        server = {
            "name": "io.github.foo/bar",
            "version": "1.0.0",
            "packages": [
                {"identifier": "pkg-a", "runtimeHint": "npx"},
                {"identifier": "pkg-b", "registryType": "pypi"},
            ],
        }
        normalized = SimpleRegistryClient._normalize_v0_1_server(server)
        self.assertEqual(normalized["packages"][0]["name"], "pkg-a")
        self.assertEqual(normalized["packages"][0]["runtime_hint"], "npx")
        self.assertEqual(normalized["packages"][1]["name"], "pkg-b")
        self.assertEqual(normalized["packages"][1]["registry_name"], "pypi")

    def test_normalize_v0_1_server_does_not_mutate_input(self):
        """Server-level normalizer matches sibling copy semantics (no mutation)."""
        server = {
            "name": "io.github.foo/bar",
            "packages": [{"identifier": "pkg-a", "runtimeHint": "npx"}],
        }
        SimpleRegistryClient._normalize_v0_1_server(server)
        self.assertNotIn("name", server["packages"][0])
        self.assertNotIn("runtime_hint", server["packages"][0])

    def test_normalize_v0_1_server_handles_missing_packages(self):
        """Server with no packages list passes through unchanged."""
        server = {"name": "io.github.foo/bar", "version": "1.0.0"}
        normalized = SimpleRegistryClient._normalize_v0_1_server(server)
        self.assertEqual(normalized["name"], "io.github.foo/bar")
        self.assertNotIn("packages", normalized)

    def test_normalize_v0_1_server_returns_non_dict_unchanged(self):
        self.assertIsNone(SimpleRegistryClient._normalize_v0_1_server(None))


if __name__ == "__main__":
    unittest.main()
