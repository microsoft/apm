"""Simple MCP Registry client for server discovery."""

import os
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401, UP035
from urllib.parse import urlparse

import requests

_DEFAULT_REGISTRY_URL = "https://api.mcp.github.com"

# Network timeouts for registry HTTP calls. ``connect`` bounds the TCP
# handshake (typo in --registry / unreachable host) so ``apm install``
# never hangs in CI; ``read`` bounds slow registries / proxies.
# Exposed via ``MCP_REGISTRY_CONNECT_TIMEOUT`` / ``MCP_REGISTRY_READ_TIMEOUT``
# for enterprise tuning, with sane defaults otherwise.
_DEFAULT_CONNECT_TIMEOUT = 10.0
_DEFAULT_READ_TIMEOUT = 30.0


def _resolve_timeout() -> tuple:
    """Return the ``(connect, read)`` timeout tuple for registry HTTP calls."""

    def _read_float(env_key: str, default: float) -> float:
        raw = os.environ.get(env_key)
        if not raw:
            return default
        try:
            value = float(raw)
            if value <= 0:
                return default
            return value
        except (TypeError, ValueError):
            return default

    return (
        _read_float("MCP_REGISTRY_CONNECT_TIMEOUT", _DEFAULT_CONNECT_TIMEOUT),
        _read_float("MCP_REGISTRY_READ_TIMEOUT", _DEFAULT_READ_TIMEOUT),
    )


class SimpleRegistryClient:
    """Simple client for querying MCP registries for server discovery."""

    def __init__(self, registry_url: str | None = None):
        """Initialize the registry client.

        Args:
            registry_url (str, optional): URL of the MCP registry.
                If not provided, uses the MCP_REGISTRY_URL environment variable
                or falls back to the default public registry.

        Raises:
            ValueError: If the resolved URL is missing a scheme/netloc, uses an
                unsupported scheme, or uses ``http://`` without
                ``MCP_REGISTRY_ALLOW_HTTP=1`` opt-in.
        """
        env_override = os.environ.get("MCP_REGISTRY_URL")
        # Treat empty-string env var as unset (common shell idiom: ``export FOO=``).
        if env_override is not None and env_override.strip() == "":
            env_override = None

        resolved = registry_url or env_override or _DEFAULT_REGISTRY_URL
        # Normalise: strip whitespace and trailing slashes so path joins
        # never produce double-slash URLs (e.g. ``https://host//v0/servers``).
        resolved = resolved.strip().rstrip("/")

        parsed = urlparse(resolved)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                f"Invalid MCP registry URL {resolved!r}: expected scheme://host "
                f"(e.g. https://mcp.example.com). Check MCP_REGISTRY_URL if set."
            )
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Unsupported scheme {parsed.scheme!r} in MCP registry URL "
                f"{resolved!r}: only https:// is supported (http:// requires "
                f"MCP_REGISTRY_ALLOW_HTTP=1). Check MCP_REGISTRY_URL if set."
            )
        if parsed.scheme == "http" and not os.environ.get("MCP_REGISTRY_ALLOW_HTTP"):
            raise ValueError(
                f"Insecure MCP registry URL {resolved!r}: http:// is not allowed "
                f"by default. Set MCP_REGISTRY_ALLOW_HTTP=1 to opt in to plaintext "
                f"HTTP (not recommended for production). "
                f"Check MCP_REGISTRY_URL if set."
            )

        self.registry_url = resolved
        # True when the URL came from an explicit caller arg or MCP_REGISTRY_URL env var.
        # Consumed by validate_servers_exist() to fail-closed on overrides.
        self._is_custom_url = registry_url is not None or env_override is not None
        self.session = requests.Session()
        self._timeout = _resolve_timeout()

    def list_servers(
        self, limit: int = 100, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List all available servers in the registry.

        Args:
            limit (int, optional): Maximum number of entries to return. Defaults to 100.
            cursor (str, optional): Pagination cursor for retrieving next set of results.

        Returns:
            Tuple[List[Dict[str, Any]], Optional[str]]: List of server metadata dictionaries and the next cursor if available.

        Raises:
            requests.RequestException: If the request fails.
        """
        url = f"{self.registry_url}/v0/servers"
        params = {}

        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor

        response = self.session.get(url, params=params, timeout=self._timeout)
        response.raise_for_status()
        data = response.json()

        # Extract servers - they're nested under "server" key in each item
        raw_servers = data.get("servers", [])
        servers = []
        for item in raw_servers:
            if "server" in item:
                servers.append(item["server"])
            else:
                servers.append(item)  # Fallback for different structure

        metadata = data.get("metadata", {})
        next_cursor = metadata.get("next_cursor")

        return servers, next_cursor

    def search_servers(self, query: str) -> list[dict[str, Any]]:
        """Search for servers in the registry using the API search endpoint.

        Args:
            query (str): Search query string.

        Returns:
            List[Dict[str, Any]]: List of matching server metadata dictionaries.

        Raises:
            requests.RequestException: If the request fails.
        """
        # The MCP Registry API now only accepts repository names (e.g., "github-mcp-server")
        # If the query looks like a full identifier (e.g., "io.github.github/github-mcp-server"),
        # extract the repository name for the search
        search_query = self._extract_repository_name(query)

        url = f"{self.registry_url}/v0/servers/search"
        params = {"q": search_query}

        response = self.session.get(url, params=params, timeout=self._timeout)
        response.raise_for_status()
        data = response.json()

        # Extract servers - they're nested under "server" key in each item
        raw_servers = data.get("servers", [])
        servers = []
        for item in raw_servers:
            if "server" in item:
                servers.append(item["server"])
            else:
                servers.append(item)  # Fallback for different structure

        return servers

    def get_server_info(self, server_id: str) -> dict[str, Any]:
        """Get detailed information about a specific server.

        Args:
            server_id (str): ID of the server.

        Returns:
            Dict[str, Any]: Server metadata dictionary.

        Raises:
            requests.RequestException: If the request fails.
            ValueError: If the server is not found.
        """
        url = f"{self.registry_url}/v0/servers/{server_id}"
        response = self.session.get(url, timeout=self._timeout)
        response.raise_for_status()
        data = response.json()

        # Return the complete response including x-github and other metadata
        # but ensure the main server info is accessible at the top level
        if "server" in data:
            # Merge server info to top level while preserving x-github and other sections
            result = data["server"].copy()
            for key, value in data.items():
                if key != "server":
                    result[key] = value

            if not result:
                raise ValueError(f"Server '{server_id}' not found in registry")

            return result
        else:
            if not data:
                raise ValueError(f"Server '{server_id}' not found in registry")
            return data

    def get_server_by_name(self, name: str) -> dict[str, Any] | None:
        """Find a server by its name using the search API.

        Args:
            name (str): Name of the server to find.

        Returns:
            Optional[Dict[str, Any]]: Server metadata dictionary or None if not found.

        Raises:
            requests.RequestException: If the registry API request fails.
        """
        # Use search API to find by name - more efficient than listing all servers
        search_results = self.search_servers(name)

        # Look for an exact match in search results
        for server in search_results:
            if server.get("name") == name:
                try:
                    return self.get_server_info(server["id"])
                except ValueError:
                    continue

        return None

    def find_server_by_reference(self, reference: str) -> dict[str, Any] | None:
        """Find a server by exact name match or server ID.

        This is an efficient lookup that uses the search API:
        1. Server ID (UUID format) - direct API call
        2. Server name - search API for exact match (automatically handles identifier extraction)

        Args:
            reference (str): Server reference (ID or exact name).

        Returns:
            Optional[Dict[str, Any]]: Server metadata dictionary or None if not found.

        Raises:
            requests.RequestException: If the registry API request fails.
        """
        # Strategy 1: Try as server ID first (direct lookup)
        try:
            # Check if it looks like a UUID (contains hyphens and is 36 chars)
            if len(reference) == 36 and reference.count("-") == 4:
                return self.get_server_info(reference)
        except ValueError:
            pass

        # Strategy 2: Use search API to find by name
        # search_servers now handles extracting repository names internally
        search_results = self.search_servers(reference)

        # Pass 1: exact full-name match (prevents slug collisions)
        for server in search_results:
            server_name = server.get("name", "")
            if server_name == reference:
                try:
                    return self.get_server_info(server["id"])
                except ValueError:
                    continue

        # Pass 2: fuzzy slug match (only when reference has no namespace)
        for server in search_results:
            server_name = server.get("name", "")
            if self._is_server_match(reference, server_name):
                try:
                    return self.get_server_info(server["id"])
                except ValueError:
                    continue

        # If not found by ID or exact name, server is not in registry
        return None

    def _extract_repository_name(self, reference: str) -> str:
        """Extract the repository name from various identifier formats.

        This method handles various naming patterns by extracting the part after
        the last slash, which typically represents the actual server/repository name.

        Examples:
        - "io.github.github/github-mcp-server" -> "github-mcp-server"
        - "abc.dllde.io/some-server" -> "some-server"
        - "adb.ok/another-server" -> "another-server"
        - "github/github-mcp-server" -> "github-mcp-server"
        - "github-mcp-server" -> "github-mcp-server"

        Args:
            reference (str): Server reference in various formats.

        Returns:
            str: Repository name suitable for API search.
        """
        # If there's a slash, extract the part after the last slash
        # This works for any pattern like domain.tld/server, owner/repo, etc.
        if "/" in reference:
            return reference.split("/")[-1]

        # Already a simple repo name
        return reference

    def _is_server_match(self, reference: str, server_name: str) -> bool:
        """Check if a reference matches a server name using common patterns.

        Matching rules:
        1. Exact string match always wins.
        2. Qualified references (contain '/') match if the server name ends
           with the reference (e.g. 'github/github-mcp-server' matches
           'io.github.github/github-mcp-server'). The match must happen at
           a namespace boundary (preceded by '.' or start-of-string) to
           prevent slug collisions like 'microsoftdocs/mcp' matching
           'com.supabase/mcp'.
        3. Unqualified references fall back to slug (last segment) comparison.

        Args:
            reference (str): Original reference from user.
            server_name (str): Server name from registry.

        Returns:
            bool: True if they represent the same server.
        """
        # Direct match
        if reference == server_name:
            return True

        if "/" in reference:
            # Qualified reference: allow suffix match at a namespace boundary.
            # e.g. "github/github-mcp-server" matches "io.github.github/github-mcp-server"
            # but "microsoftdocs/mcp" must NOT match "com.supabase/mcp".
            if server_name.endswith(reference):
                prefix = server_name[: -len(reference)]
                # Valid boundary: empty (exact), or ends with '.' (namespace separator)
                if prefix == "" or prefix.endswith("."):
                    return True
            return False

        # Unqualified reference: fall back to slug comparison
        ref_repo = self._extract_repository_name(reference)
        server_repo = self._extract_repository_name(server_name)

        return ref_repo == server_repo
