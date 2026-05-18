"""Server-search helpers extracted from ``registry.client``.

Extracted to keep ``SimpleRegistryClient`` under 400 LOC.
``get_server_by_name`` and ``find_server_by_reference`` take a ``client``
argument (the ``SimpleRegistryClient`` instance) so they can call its
``search_servers`` / ``get_server`` methods without subclassing.
``_extract_repository_name`` and ``_is_server_match`` are pure helpers
that do not need ``client``.
"""

from __future__ import annotations


def _extract_repository_name(reference: str) -> str:
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


def _is_server_match(reference: str, server_name: str) -> bool:
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
    ref_repo = _extract_repository_name(reference)
    server_repo = _extract_repository_name(server_name)

    return ref_repo == server_repo


def get_server_by_name(client, name: str):
    """Find a server by its name using the search API.

    Args:
        client: ``SimpleRegistryClient`` instance.
        name (str): Name of the server to find.

    Returns:
        Optional[Dict[str, Any]]: Server metadata dictionary or None if not found.

    Raises:
        requests.RequestException: If the registry API request fails.
    """
    # Use search API to find by name - more efficient than listing all servers
    search_results = client.search_servers(name)

    # Look for an exact match in search results
    for server in search_results:
        if server.get("name") == name:
            try:
                return client.get_server(server["name"])
            except ValueError:
                continue

    return None


def find_server_by_reference(client, reference: str):
    """Find a server by exact name match.

    The legacy UUID strategy was removed because the MCP Registry v0.1
    spec keys per-server lookup on serverName, not UUID. Old UUID-style
    references silently route through search and produce no match,
    which is acceptable per design ratification.

    Args:
        client: ``SimpleRegistryClient`` instance.
        reference (str): Server reference (exact name or unqualified slug).

    Returns:
        Optional[Dict[str, Any]]: Server metadata dictionary or None if not found.

    Raises:
        requests.RequestException: If the registry API request fails.
    """
    # Use search API to find by name
    search_results = client.search_servers(reference)

    # Pass 1: exact full-name match (prevents slug collisions)
    for server in search_results:
        server_name = server.get("name", "")
        if server_name == reference:
            try:
                return client.get_server(server_name)
            except ValueError:
                continue

    # Pass 2: fuzzy slug match (only when reference has no namespace)
    for server in search_results:
        server_name = server.get("name", "")
        if _is_server_match(reference, server_name):
            try:
                return client.get_server(server_name)
            except ValueError:
                continue

    # If not found by name, server is not in registry
    return None
