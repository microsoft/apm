"""Simple MCP Registry client for server discovery."""

import logging
import os
import re
import warnings
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401, UP035
from urllib.parse import quote, urlparse

import requests

_log = logging.getLogger(__name__)


def _safe_headers(response) -> dict[str, str]:
    """Return response headers as a plain dict, tolerating Mock objects in tests."""
    try:
        return dict(response.headers)
    except (TypeError, AttributeError):
        return {}


_DEFAULT_REGISTRY_URL = "https://api.mcp.github.com"

# MCP Registry API version path prefix. Bumping this here is the
# single grep target the day v0.2 ships. See
# https://github.com/modelcontextprotocol/registry for the spec.
_V0_1_PREFIX = "/v0.1"

# Allowlist for server names used as URL path segments. The MCP spec
# constrains names to reverse-DNS-style identifiers, optionally with a
# single ``/<repo>`` slug suffix. ``quote(name, safe='')`` already
# neutralises traversal/SSRF; this allowlist makes the threat model
# explicit at the call site so a future caller cannot bypass search
# and feed attacker-controlled strings into the path.
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)?$")


class ServerNotFoundError(ValueError):
    """Raised when a server lookup against the registry returns 404.

    Carries the registry URL so the CLI boundary can render an
    actionable hint about MCP Registry v0.1 spec compliance for
    self-hosted registries. Inherits from ``ValueError`` so existing
    ``except ValueError`` callers (e.g. ``find_server_by_reference``)
    keep treating 404s as "not found" without code changes.
    """

    def __init__(self, server_name: str, registry_url: str) -> None:
        self.server_name = server_name
        self.registry_url = registry_url
        super().__init__(
            f"Server '{server_name}' not found in registry {registry_url}. "
            f"If this is a self-hosted registry, verify it implements the "
            f"MCP Registry v0.1 API (apm uses /v0.1/servers/...)."
        )


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

        # Strip any embedded userinfo (``user:pass@``) before storing the URL so
        # ``ServerNotFoundError`` and other diagnostics cannot leak credentials
        # into terminal output or CI logs. Enterprise users sometimes set
        # ``MCP_REGISTRY_URL=https://token:x-oauth@registry.corp/`` -- we still
        # accept the URL (the credentials are passed via Authorization headers
        # elsewhere), but we never echo them back.
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            sanitized_netloc = host + (f":{parsed.port}" if parsed.port else "")
            resolved = parsed._replace(netloc=sanitized_netloc).geturl().rstrip("/")

        self.registry_url = resolved
        # True when the URL came from an explicit caller arg or MCP_REGISTRY_URL env var.
        # Consumed by validate_servers_exist() to fail-closed on overrides.
        self._is_custom_url = registry_url is not None or env_override is not None
        self.session = requests.Session()
        self._timeout = _resolve_timeout()
        self._http_cache = self._init_http_cache()

    @staticmethod
    def _init_http_cache():
        """Resolve the shared HTTP response cache, or ``None`` if disabled.

        Honors ``APM_NO_CACHE`` so users can opt out, and degrades to
        ``None`` on any setup error so registry calls always fall back to
        plain network behavior.
        """
        if os.environ.get("APM_NO_CACHE", "").strip() in ("1", "true", "yes"):
            return None
        try:
            from apm_cli.cache import HttpCache, get_cache_root

            return HttpCache(get_cache_root())
        except Exception as exc:  # pragma: no cover - defensive
            _log.debug("HTTP cache unavailable, falling back to network: %s", exc)
            return None

    def _cached_get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """GET ``url`` honoring the persistent HTTP cache.

        On a fresh cache hit returns the parsed JSON immediately.  On an
        expired entry, sends ``If-None-Match`` for revalidation; on 304 the
        cached body is reused and its TTL refreshed.  Returns
        ``(json_payload, response_headers)``; when there is no payload
        (204 No Content), ``json_payload`` is ``None``.

        Falls back to a plain ``session.get`` when the cache is disabled
        or unavailable.
        """
        # Cache key includes query params so paginated/search URLs are
        # cached independently.
        cache_key = url
        if params:
            from urllib.parse import urlencode

            cache_key = f"{url}?{urlencode(sorted(params.items()))}"

        # Auth bypass: when the request would carry an Authorization
        # header (either on the session or per-request), skip the
        # cache entirely. Caching authenticated responses risks
        # cross-identity body leakage when a different caller hits
        # the same URL with different credentials -- and scoping the
        # cache by hashed token would just recreate the underlying
        # auth-store responsibility. Bypass is the simple safe
        # default; the MCP registry path is anonymous in practice.
        session_auth = bool(self.session.headers.get("Authorization"))
        if session_auth or self._http_cache is None:
            kwargs0: dict[str, Any] = {"timeout": self._timeout}
            if params:
                kwargs0["params"] = params
            response = self.session.get(url, **kwargs0)
            response.raise_for_status()
            return response.json(), _safe_headers(response)

        # Fresh cache hit
        cached = self._http_cache.get(cache_key)
        if cached is not None:
            try:
                import json as _json

                return _json.loads(cached.body.decode("utf-8")), {}
            except (ValueError, UnicodeDecodeError):
                pass  # fall through to network

        # Expired or missing: send conditional headers if we have an ETag
        request_headers = self._http_cache.conditional_headers(cache_key)
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if params:
            kwargs["params"] = params
        if request_headers:
            kwargs["headers"] = request_headers
        response = self.session.get(url, **kwargs)

        if response.status_code == 304:
            self._http_cache.refresh_expiry(cache_key, _safe_headers(response))
            cached = self._http_cache.get(cache_key)
            if cached is not None:
                try:
                    import json as _json

                    return _json.loads(cached.body.decode("utf-8")), _safe_headers(response)
                except (ValueError, UnicodeDecodeError):
                    pass  # fall through to a fresh fetch
            # Stored entry vanished between revalidate and read: refetch
            kwargs2: dict[str, Any] = {"timeout": self._timeout}
            if params:
                kwargs2["params"] = params
            response = self.session.get(url, **kwargs2)

        response.raise_for_status()
        try:
            body = response.content
            self._http_cache.store(
                cache_key,
                body,
                status_code=response.status_code,
                headers=_safe_headers(response),
            )
        except Exception as exc:  # pragma: no cover - defensive
            _log.debug("HTTP cache store failed for %s: %s", cache_key, exc)
        return response.json(), _safe_headers(response)

    def list_servers(
        self, limit: int = 100, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List all available servers in the registry.

        Calls ``GET /v0.1/servers`` per the MCP Registry spec.

        Args:
            limit (int, optional): Maximum number of entries to return. Defaults to 100.
            cursor (str, optional): Pagination cursor for retrieving next set of results.

        Returns:
            Tuple[List[Dict[str, Any]], Optional[str]]: List of server metadata
            dictionaries and the next cursor if available.

        Raises:
            requests.RequestException: If the request fails.
        """
        url = f"{self.registry_url}{_V0_1_PREFIX}/servers"
        params = {}

        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor

        data, _hdrs = self._cached_get_json(url, params=params)
        data = data or {}

        servers = self._unwrap_server_list(data)

        metadata = data.get("metadata", {})
        # Spec is camelCase ``nextCursor``; ``next_cursor`` accepted as a
        # transitional kindness for in-tree mock fixtures only.
        # TODO(v0.1): drop legacy snake_case once fixtures migrate.
        next_cursor = metadata.get("nextCursor") or metadata.get("next_cursor")

        return servers, next_cursor

    def search_servers(self, query: str) -> list[dict[str, Any]]:
        """Search for servers in the registry using the spec ``?search=`` query param.

        Calls ``GET /v0.1/servers?search=<query>`` per the MCP Registry spec
        (case-insensitive substring match on server names).

        Args:
            query (str): Search query string. The full reference is passed
                through to the registry; spec-compliant registries do
                substring matching on names so ``io.github.foo/bar`` and
                ``bar`` both match ``io.github.foo/bar``.

        Returns:
            List[Dict[str, Any]]: List of matching server metadata dictionaries.

        Raises:
            requests.RequestException: If the request fails.
        """
        url = f"{self.registry_url}{_V0_1_PREFIX}/servers"
        params = {"search": query}

        data, _hdrs = self._cached_get_json(url, params=params)
        data = data or {}

        return self._unwrap_server_list(data)

    @staticmethod
    def _unwrap_server_list(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Strict v0.1 unwrap of the ``servers`` array.

        Each entry is expected to carry a nested ``server`` object per
        the spec. We deliberately do NOT fall back to flat shapes -- a
        non-conformant registry should fail loudly here, not silently
        produce half-shaped dicts that explode three call frames away
        in conflict detection.
        """
        raw_servers = data.get("servers", [])
        servers = []
        for item in raw_servers:
            if not isinstance(item, dict) or "server" not in item:
                raise ValueError(
                    "Registry returned a non-spec list entry (missing 'server' key); "
                    "expected MCP Registry v0.1 response shape."
                )
            servers.append(item["server"])
        return servers

    # Map of v0.1 spec package field names (camelCase / renamed) to the
    # legacy snake_case shape that adapters in src/apm_cli/adapters/client/
    # consume (e.g. package.get("name"), package.get("runtime_hint")).
    # The registry boundary normalizes inbound packages so adapters keep
    # working without per-adapter rewrites. See #1210 review feedback:
    # without this, registry-resolved installs produced "npx -y None".
    _V0_1_PACKAGE_FIELD_ALIASES: tuple[tuple[str, str], ...] = (
        ("identifier", "name"),
        ("registryType", "registry_name"),
        ("registryBaseUrl", "registry_base_url"),
        ("runtimeHint", "runtime_hint"),
        ("packageArguments", "package_arguments"),
        ("runtimeArguments", "runtime_arguments"),
        ("environmentVariables", "environment_variables"),
    )

    @classmethod
    def _normalize_v0_1_package(cls, package: dict[str, Any]) -> dict[str, Any]:
        """Backfill legacy snake_case keys from v0.1 camelCase aliases.

        Only writes the legacy key when the package does not already
        carry one, so registries that emit both shapes (or the legacy
        shape only) are unaffected. This is a one-way bridge: the
        camelCase key is preserved so callers that have already migrated
        keep working.
        """
        if not isinstance(package, dict):
            return package
        normalized = dict(package)
        for v01_key, legacy_key in cls._V0_1_PACKAGE_FIELD_ALIASES:
            if legacy_key not in normalized and v01_key in normalized:
                normalized[legacy_key] = normalized[v01_key]
        return normalized

    @classmethod
    def _normalize_v0_1_server(cls, server: dict[str, Any]) -> dict[str, Any]:
        """Apply package-shape normalization to a server detail dict.

        Returns a shallow copy with each entry of ``packages`` normalized
        via :meth:`_normalize_v0_1_package`. The input dict is not mutated,
        matching the copy semantics of the sibling normalizer.
        """
        if not isinstance(server, dict):
            return server
        normalized = dict(server)
        packages = normalized.get("packages")
        if isinstance(packages, list) and packages:
            normalized["packages"] = [cls._normalize_v0_1_package(p) for p in packages]
        return normalized

    def get_server(self, server_name: str, version: str = "latest") -> dict[str, Any]:
        """Get detailed information about a specific server version.

        Calls ``GET /v0.1/servers/{urlencoded-serverName}/versions/{version}``
        per the MCP Registry spec. The default ``version="latest"`` covers
        99% of callers; pin to a specific version string for reproducibility.

        Args:
            server_name (str): Full server name (e.g. ``io.github.foo/bar``).
                Validated against the spec name shape and URL-encoded.
            version (str, optional): Version string or ``"latest"``. Defaults
                to ``"latest"``.

        Returns:
            Dict[str, Any]: Server metadata dictionary (the unwrapped
            contents of the response's ``server`` field, with any
            top-level siblings merged in).

        Raises:
            ValueError: If the server name does not match the spec shape.
            ServerNotFoundError: If the registry returns 404.
            requests.RequestException: If the request fails for other reasons.
        """
        if not _SERVER_NAME_RE.match(server_name or ""):
            raise ValueError(
                f"Invalid server name {server_name!r}: expected MCP spec shape "
                f"(reverse-DNS identifier, optionally with a single '/<repo>' suffix)."
            )

        encoded_name = quote(server_name, safe="")
        encoded_version = quote(version, safe="")
        url = f"{self.registry_url}{_V0_1_PREFIX}/servers/{encoded_name}/versions/{encoded_version}"

        try:
            data, _hdrs = self._cached_get_json(url)
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 404:
                raise ServerNotFoundError(server_name, self.registry_url) from exc
            raise

        data = data or {}

        # Return the complete response including _meta and other top-level
        # metadata, but ensure the main server info is accessible at the top level.
        if "server" in data:
            result = data["server"].copy()
            for key, value in data.items():
                if key != "server":
                    result[key] = value
            if not result:
                raise ServerNotFoundError(server_name, self.registry_url)
            return self._normalize_v0_1_server(result)

        if not data:
            raise ServerNotFoundError(server_name, self.registry_url)
        return self._normalize_v0_1_server(data)

    def get_server_info(self, server_name: str) -> dict[str, Any]:
        """Deprecated alias for :meth:`get_server`.

        Kept for one minor as a transitional shim; emits a
        ``DeprecationWarning`` and forwards to ``get_server``. The
        parameter is now interpreted as a server *name* (per the MCP
        Registry v0.1 spec), not a UUID -- the legacy v0 ``/servers/{id}``
        endpoint no longer exists on spec-compliant registries.
        """
        warnings.warn(
            "SimpleRegistryClient.get_server_info(server_name) is deprecated; "
            "use SimpleRegistryClient.get_server(server_name, version='latest'). "
            "The parameter now means a server name per MCP Registry v0.1.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.get_server(server_name)

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
                    return self.get_server(server["name"])
                except ValueError:
                    continue

        return None

    def find_server_by_reference(self, reference: str) -> dict[str, Any] | None:
        """Find a server by exact name match.

        The legacy UUID strategy was removed because the MCP Registry v0.1
        spec keys per-server lookup on serverName, not UUID. Old UUID-style
        references silently route through search and produce no match,
        which is acceptable per design ratification.

        Args:
            reference (str): Server reference (exact name or unqualified slug).

        Returns:
            Optional[Dict[str, Any]]: Server metadata dictionary or None if not found.

        Raises:
            requests.RequestException: If the registry API request fails.
        """
        # Use search API to find by name
        search_results = self.search_servers(reference)

        # Pass 1: exact full-name match (prevents slug collisions)
        for server in search_results:
            server_name = server.get("name", "")
            if server_name == reference:
                try:
                    return self.get_server(server_name)
                except ValueError:
                    continue

        # Pass 2: fuzzy slug match (only when reference has no namespace)
        for server in search_results:
            server_name = server.get("name", "")
            if self._is_server_match(reference, server_name):
                try:
                    return self.get_server(server_name)
                except ValueError:
                    continue

        # If not found by name, server is not in registry
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
