"""Simple MCP Registry client for server discovery."""

import logging
import os
import warnings
from typing import Any
from urllib.parse import quote, urlparse

import requests

from . import _client_search as _cs
from ._helpers import (  # noqa: F401 – re-exported; callers import from this module
    _DEFAULT_CONNECT_TIMEOUT,
    _DEFAULT_READ_TIMEOUT,
    _DEFAULT_REGISTRY_URL,
    _SERVER_NAME_RE,
    _V0_1_PREFIX,
    ServerNotFoundError,
    _resolve_timeout,
    _safe_headers,
)
from ._http import _get_json_with_cache

_log = logging.getLogger(__name__)


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
        return _get_json_with_cache(
            self.session, self._http_cache, self._timeout, url, params=params
        )

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
        """Find a server by its name using the search API."""
        return _cs.get_server_by_name(self, name)

    def find_server_by_reference(self, reference: str) -> tuple[dict[str, Any] | None, str]:
        """Find a server from a package reference (name, URL, or org/repo)."""
        return _cs.find_server_by_reference(self, reference)

    @staticmethod
    def _extract_repository_name(reference: str) -> str | None:
        """Extract a repository name from a URL."""
        return _cs._extract_repository_name(reference)

    @staticmethod
    def _is_server_match(reference: str, server_name: str) -> bool:
        """Check whether *reference* matches *server_name*."""
        return _cs._is_server_match(reference, server_name)
