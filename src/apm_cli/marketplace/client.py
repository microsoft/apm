"""Fetch, parse, and cache marketplace.json from Git hosting repositories.

Dispatches over a ``_FETCHERS`` table keyed by ``source.kind``:

- ``github`` / ``gitlab`` -> host file API via ``_fetch_via_api`` (auth-routed
  through ``AuthResolver.try_with_fallback`` and the JSON sidecar cache).
- ``git`` -> generic git URL (ADO, Gitea, self-hosted, etc.) via subprocess
  through ``GitCache``; ``git ls-remote`` is the freshness check, no JSON
  sidecar cache.
- ``local`` -> bare repo (``git --git-dir=... show <ref>:<file>``), working
  directory, or direct manifest file (path-containment guard); no cache.
- ``url`` -> direct hosted ``marketplace.json`` over HTTPS with digest and
  ETag/Last-Modified cache metadata.

When ``PROXY_REGISTRY_URL`` is set, GitHub/GHES fetches go through the
Artifactory Archive Entry Download proxy first. Cache lives at
``~/.apm/cache/marketplace/`` with a 1-hour TTL.
"""

import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from ._client_cache import (
    _cache_data_path as _cache_data_path,
)
from ._client_cache import (
    _cache_key as _cache_key,
)
from ._client_cache import (
    _cache_meta_path as _cache_meta_path,
)
from ._client_cache import (
    _clear_cache as _clear_cache,
)
from ._client_cache import (
    _host_from_url as _host_from_url,
)
from ._client_cache import (
    _read_cache as _read_cache,
)
from ._client_cache import (
    _read_stale_cache as _read_stale_cache,
)
from ._client_cache import (
    _read_stale_meta as _read_stale_meta,
)
from ._client_cache import (
    _sanitize_cache_name as _sanitize_cache_name,
)
from ._client_cache import (
    _write_cache as _write_cache,
)
from ._client_http import (
    _HTTP_SESSION as _HTTP_SESSION,
)
from ._client_http import (
    _MAX_MARKETPLACE_JSON_BYTES as _MAX_MARKETPLACE_JSON_BYTES,
)
from ._client_http import (
    FetchResult as FetchResult,
)
from ._client_http import (
    _fetch_url_direct as _fetch_url_direct,
)
from ._client_http import (
    _http_get as _http_get,
)
from ._client_http import (
    _parse_json_text as _parse_json_text,
)
from ._client_http import (
    _read_bounded_response_bytes as _read_bounded_response_bytes,
)
from ._client_http import (
    _try_proxy_fetch as _try_proxy_fetch,
)
from ._client_http import (
    _try_proxy_fetch_raw as _try_proxy_fetch_raw,
)
from .errors import MarketplaceError, MarketplaceFetchError
from .models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
    parse_marketplace_json,
)
from .registry import get_registered_marketplaces

logger = logging.getLogger(__name__)

# Candidate locations for marketplace.json in a repository (priority order)
_MARKETPLACE_PATHS = [
    "marketplace.json",
    ".github/plugin/marketplace.json",
    ".claude-plugin/marketplace.json",
]

# Safe ref pattern: letters, digits, dot, slash, hyphen, underscore.
# Rejects empty, leading "-", colons, spaces, and shell metacharacters.
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


def _validate_ref(ref: str, source_name: str) -> str:
    """Return *ref* unchanged if it matches the safe pattern; raise otherwise.

    Defends both ``_fetch_local`` (subprocess ``git show <ref>:<file>``) and
    ``_fetch_git`` (passes ref through to ``GitCache.get_checkout``) against
    ref-injection in user-supplied marketplace registrations.
    """
    if not _SAFE_REF_RE.match(ref or ""):
        raise MarketplaceFetchError(
            source_name,
            f"Invalid git ref '{ref}': refs must match {_SAFE_REF_RE.pattern}",
        )
    return ref


# ---------------------------------------------------------------------------
# Network fetch -- API path (GitHub / GitLab)
# ---------------------------------------------------------------------------


def _github_contents_url(source: MarketplaceSource, file_path: str, host_info) -> str:
    """Build the GitHub Contents API URL for a file (GitHub / GHES / generic)."""
    api_base = host_info.api_base.rstrip("/")
    encoded_ref = quote(source.ref, safe="")
    return f"{api_base}/repos/{source.owner}/{source.repo}/contents/{file_path}?ref={encoded_ref}"


def _gitlab_file_raw_url(source: MarketplaceSource, file_path: str, host_info) -> str:
    """Build the GitLab REST v4 repository file raw URL."""
    project_path = f"{source.owner}/{source.repo}"
    encoded_project = quote(project_path, safe="")
    encoded_file = quote(file_path, safe="")
    encoded_ref = quote(source.ref, safe="")
    api_base = host_info.api_base.rstrip("/")
    return (
        f"{api_base}/projects/{encoded_project}/repository/files/"
        f"{encoded_file}/raw?ref={encoded_ref}"
    )


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3.raw", "User-Agent": "apm-cli"}
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _gitlab_headers(token: str | None) -> dict[str, str]:
    from ..core.auth import AuthResolver

    headers = {"User-Agent": "apm-cli"}
    headers.update(AuthResolver.gitlab_rest_headers(token))
    return headers


def _fetch_via_api(
    source: MarketplaceSource,
    file_path: str,
    *,
    url_builder: Callable,
    header_builder: Callable[[str | None], dict[str, str]],
    parse_response: Callable,
    host_info,
    auth_resolver,
) -> dict | None:
    """Shared API-fetch helper for github/gitlab kinds.

    Owns the common boilerplate: build URL, build headers, run
    ``try_with_fallback``, map 404 -> None, raise ``MarketplaceFetchError``
    on unexpected errors. Specialised callers pass kind-specific URL and
    header builders.
    """
    url = url_builder(source, file_path, host_info)

    def _do_fetch(token, _git_env):
        resp = _http_get(url, headers=header_builder(token), timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return parse_response(resp)

    try:
        return auth_resolver.try_with_fallback(
            source.host,
            _do_fetch,
            org=source.owner,
            path=f"{source.owner}/{source.repo}",
            unauth_first=False,
        )
    except Exception as exc:
        logger.debug("API fetch failed for '%s'", source.name, exc_info=True)
        raise MarketplaceFetchError(source.name, str(exc)) from exc


def _fetch_github(
    source: MarketplaceSource,
    file_path: str,
    *,
    host_info,
    auth_resolver,
) -> dict | None:
    """Fetch marketplace.json from GitHub / GHE Cloud / GHES via Contents API."""
    return _fetch_via_api(
        source,
        file_path,
        url_builder=_github_contents_url,
        header_builder=_github_headers,
        parse_response=lambda r: r.json(),
        host_info=host_info,
        auth_resolver=auth_resolver,
    )


def _fetch_gitlab(
    source: MarketplaceSource,
    file_path: str,
    *,
    host_info,
    auth_resolver,
) -> dict | None:
    """Fetch marketplace.json from GitLab via REST v4 raw file API.

    ``_fetch_via_api`` already runs ``auth_resolver.try_with_fallback`` and
    handles 404/auth fallback internally, so there is no separate retry layer
    here -- adding one would double-wrap ``try_with_fallback`` and risk
    re-entering the fallback logic.
    """
    return _fetch_via_api(
        source,
        file_path,
        url_builder=_gitlab_file_raw_url,
        header_builder=_gitlab_headers,
        parse_response=_parse_json_text,
        host_info=host_info,
        auth_resolver=auth_resolver,
    )


# ---------------------------------------------------------------------------
# Network fetch -- generic git path (ADO / Gitea / Bitbucket / self-hosted)
# ---------------------------------------------------------------------------


def _fetch_git(
    source: MarketplaceSource,
    file_path: str,
    *,
    host_info,
    auth_resolver,
) -> dict | None:
    """Fetch marketplace.json from a generic git URL via subprocess + GitCache.

    Sparse-cone clones only the requested manifest path. Uses
    ``AuthResolver.resolve(host, org).git_env`` to build the git env; for
    hosts APM doesn't recognise, the env passes through to the user's
    credential helpers (matches ``apm install`` posture).
    """
    _validate_ref(source.ref, source.name)

    from ..cache.git_cache import GitCache, _sanitize_url
    from ..cache.paths import get_cache_root

    org = source.owner or None
    auth_ctx = auth_resolver.resolve(host_info.host, org)
    git_env = auth_ctx.git_env

    cache = GitCache(get_cache_root(), refresh=False)
    try:
        # Sparse-cone clone -- only the marketplace.json directory tree is fetched.
        checkout_dir = cache.get_checkout(
            source.url,
            source.ref,
            env=git_env,
            sparse_paths=[file_path] if "/" in file_path else None,
        )
    except subprocess.CalledProcessError as exc:
        # Map "object not found" / "couldn't find remote ref" to None so the
        # caller's _auto_detect_path probe can try the next candidate path.
        # Sanitize stderr in case a custom credential helper echoed a secret
        # back through git's stderr stream.
        stderr_raw = (getattr(exc, "stderr", b"") or b"").decode("utf-8", errors="replace")
        if "not found" in stderr_raw.lower() or "does not exist" in stderr_raw.lower():
            return None
        stderr = _sanitize_url(stderr_raw)
        raise MarketplaceFetchError(source.name, f"git fetch failed: {stderr or exc}") from exc
    except Exception as exc:
        logger.debug("Generic-git fetch failed for '%s'", source.name, exc_info=True)
        raise MarketplaceFetchError(source.name, _sanitize_url(str(exc))) from exc

    target = Path(checkout_dir) / file_path
    if not target.exists():
        return None
    try:
        with open(target, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise MarketplaceFetchError(source.name, f"failed to read {file_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Local fetch (filesystem path or file://)
# ---------------------------------------------------------------------------


def _fetch_local(
    source: MarketplaceSource,
    file_path: str,
    *,
    host_info=None,
    auth_resolver=None,
) -> dict | None:
    """Read marketplace.json from a local filesystem path.

    Supports two topologies:

    1. **Bare repository** (``.git`` dir with no working tree) -> use
       ``git --git-dir=<path> show <ref>:<file>`` to extract blob content.
       Symlink-safe because git addresses blobs by content hash.
    2. **Working directory** (regular checkout or unpacked tree) -> read the
       file directly after resolving the path and validating it stays within
       the repo root (``ensure_path_within``).
    """
    _validate_ref(source.ref, source.name)
    repo_path = Path(source.local_path or source.url).expanduser()
    try:
        repo_path = repo_path.resolve(strict=False)
    except OSError as exc:
        raise MarketplaceFetchError(source.name, f"failed to resolve local path: {exc}") from exc

    if not repo_path.exists():
        raise MarketplaceFetchError(
            source.name, f"local marketplace path does not exist: {repo_path}"
        )

    if repo_path.is_file():
        return _fetch_local_file(source, repo_path)

    # Detect bare repo: it's either a directory with HEAD + objects/ (bare layout)
    # or it ends in .git, or it has a .git subdirectory (worktree).
    is_bare = (repo_path / "HEAD").is_file() and (repo_path / "objects").is_dir()
    git_dir = (
        repo_path if is_bare else (repo_path / ".git" if (repo_path / ".git").exists() else None)
    )

    if git_dir is not None:
        return _fetch_local_via_git_show(source, file_path, git_dir)

    # Plain directory: read the file directly with symlink-escape guard.
    return _fetch_local_direct_read(source, file_path, repo_path)


def _fetch_local_file(source: MarketplaceSource, manifest_file: Path) -> dict | None:
    """Read an explicit local marketplace.json file.

    The parent directory is the containment boundary by design: unlike a
    directory source, a direct file source is a single user-selected file, so
    there is no broader marketplace root to enforce.
    """
    from ..utils.path_security import PathTraversalError, ensure_path_within

    try:
        safe_file = ensure_path_within(manifest_file, manifest_file.parent)
    except PathTraversalError as exc:
        raise MarketplaceFetchError(
            source.name, "local marketplace file escapes its parent"
        ) from exc

    try:
        with open(safe_file, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise MarketplaceFetchError(source.name, f"failed to read {safe_file}: {exc}") from exc


def _fetch_local_via_git_show(
    source: MarketplaceSource, file_path: str, git_dir: Path
) -> dict | None:
    """Use ``git show <ref>:<file>`` against a bare repo or .git directory."""
    from ..utils.git_env import git_subprocess_env

    cmd = [
        "git",
        "--git-dir",
        str(git_dir),
        "-c",
        "core.hooksPath=/dev/null",
        "show",
        f"{source.ref}:{file_path}",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=30,
            env=git_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise MarketplaceFetchError(source.name, f"git show failed for {file_path}: {exc}") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        # Missing path or ref -> None so _auto_detect_path can probe next candidate
        if (
            "does not exist" in stderr.lower()
            or "exists on disk, but not in" in stderr.lower()
            or "fatal: path" in stderr.lower()
        ):
            return None
        raise MarketplaceFetchError(source.name, f"git show failed: {stderr}")

    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MarketplaceFetchError(source.name, f"invalid JSON in {file_path}: {exc}") from exc


def _fetch_local_direct_read(
    source: MarketplaceSource, file_path: str, repo_root: Path
) -> dict | None:
    """Read a file directly from a working-dir local marketplace.

    Symlink-escape guard: resolves the target through ``Path.resolve`` and
    asserts it stays within ``repo_root`` via ``ensure_path_within``.
    """
    from ..utils.path_security import PathTraversalError, ensure_path_within

    candidate = (repo_root / file_path).resolve(strict=False)
    try:
        ensure_path_within(candidate, repo_root)
    except PathTraversalError as exc:
        raise MarketplaceFetchError(
            source.name, f"path escapes marketplace root: {file_path}"
        ) from exc

    if not candidate.exists():
        return None
    try:
        with open(candidate, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise MarketplaceFetchError(source.name, f"failed to read {file_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


_FETCHERS: dict[str, Callable] = {
    "github": _fetch_github,
    "gitlab": _fetch_gitlab,
    "git": _fetch_git,
    "local": _fetch_local,
}


def _github_raw_contents_url(host_info, owner: str, repo: str, file_path: str, ref: str) -> str:
    """Build the GitHub Contents API URL for an arbitrary file."""
    api_base = host_info.api_base.rstrip("/")
    encoded_ref = quote(ref, safe="")
    return f"{api_base}/repos/{owner}/{repo}/contents/{file_path}?ref={encoded_ref}"


def fetch_raw(
    host: str,
    owner: str,
    repo: str,
    file_path: str,
    ref: str,
    *,
    auth_resolver: object | None = None,
) -> bytes | None:
    """Fetch a file as raw bytes from a GitHub-compatible host.

    Used by ``apm marketplace audit`` to read plugin ``apm.yml`` files at
    their pinned refs while preserving the marketplace proxy and auth
    semantics. Returns ``None`` only for a confirmed 404.
    """
    proxy_bytes = _try_proxy_fetch_raw(owner, repo, file_path, ref)
    if proxy_bytes is not None:
        return proxy_bytes

    from ..deps.registry_proxy import RegistryConfig

    cfg = RegistryConfig.from_env()
    if cfg is not None and cfg.enforce_only:
        raise MarketplaceError(
            f"cannot verify {owner}/{repo}/{file_path}@{ref}: "
            "PROXY_REGISTRY_ONLY blocks the GitHub fallback after a proxy miss"
        )

    from ..core.auth import AuthResolver

    host_info = AuthResolver.classify_host(host)
    if host_info.kind not in ("github", "ghe_cloud", "ghes"):
        raise MarketplaceError(
            f"cannot verify {owner}/{repo}/{file_path}@{ref}: "
            f"host {host!r} is not a supported plugin source. Only GitHub, "
            "GitHub Enterprise Cloud (*.ghe.com), and GHES (GITHUB_HOST) "
            "are supported; refusing to fetch to avoid forwarding GitHub "
            "credentials to a non-GitHub host."
        )

    url = _github_raw_contents_url(host_info, owner, repo, file_path, ref)

    def _do_fetch(token, _git_env):
        headers = _github_headers(token)
        resp = _http_get(url, headers=headers, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    if auth_resolver is None:
        auth_resolver = AuthResolver()

    try:
        return auth_resolver.try_with_fallback(
            host,
            _do_fetch,
            org=owner,
            unauth_first=False,
        )
    except Exception as exc:
        raise MarketplaceError(f"fetching {owner}/{repo}/{file_path}@{ref}: {exc}") from exc


def _fetch_file(
    source: MarketplaceSource,
    file_path: str,
    auth_resolver: object | None = None,
) -> dict | None:
    """Dispatch to the right fetcher based on ``source.kind``.

    Returns parsed JSON dict, or ``None`` when the file does not exist.
    Raises ``MarketplaceFetchError`` on unexpected failures.
    """
    from ..core.auth import AuthResolver

    if auth_resolver is None:
        auth_resolver = AuthResolver()

    kind = source.kind
    fetcher = _FETCHERS.get(kind)
    if fetcher is None:
        raise MarketplaceFetchError(source.name, f"Unsupported marketplace source kind: {kind!r}")

    # Proxy-aware path for github/gitlab kinds: try registry proxy first, then
    # honour PROXY_REGISTRY_ONLY enforcement.
    if kind in ("github", "gitlab"):
        proxy_result = _try_proxy_fetch(source, file_path)
        if proxy_result is not None:
            return proxy_result
        from ..deps.registry_proxy import RegistryConfig

        cfg = RegistryConfig.from_env()
        if cfg is not None and cfg.enforce_only:
            logger.debug(
                "PROXY_REGISTRY_ONLY blocks direct fetch for %s/%s %s",
                source.owner,
                source.repo,
                file_path,
            )
            return None

    host_info = None
    if kind in ("github", "gitlab"):
        host_info = AuthResolver.classify_host(source.host)
    elif kind == "git":
        # For generic git, classify the host extracted from the URL so ADO etc.
        # get correctly-typed auth contexts.
        host = _host_from_url(source.url)
        host_info = AuthResolver.classify_host(host) if host else None

    return fetcher(source, file_path, host_info=host_info, auth_resolver=auth_resolver)


def _auto_detect_path(
    source: MarketplaceSource,
    auth_resolver: object | None = None,
) -> str | None:
    """Probe candidate locations and return the first that exists.

    Returns ``None`` if no location contains a marketplace.json.
    Raises ``MarketplaceFetchError`` on non-404 failures (auth errors, etc.).
    """
    for candidate in _MARKETPLACE_PATHS:
        data = _fetch_file(source, candidate, auth_resolver=auth_resolver)
        if data is not None:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_marketplace(
    source: MarketplaceSource,
    *,
    force_refresh: bool = False,
    auth_resolver: object | None = None,
) -> MarketplaceManifest:
    """Fetch and parse a marketplace manifest.

    Uses the JSON sidecar cache for ``kind in ("github", "gitlab", "url")``.
    Generic-git fetches rely on ``GitCache`` + ``git ls-remote`` for
    freshness; local fetches read directly without caching.

    Args:
        source: Marketplace source to fetch.
        force_refresh: Skip cache and re-fetch from network.
        auth_resolver: Optional ``AuthResolver`` instance (created if None).

    Returns:
        MarketplaceManifest: Parsed manifest.

    Raises:
        MarketplaceFetchError: If fetch fails and no cache is available.
    """
    cache_name = _cache_key(source)
    use_sidecar_cache = source.kind in ("github", "gitlab", "url")

    # Try fresh cache first (API kinds only)
    if use_sidecar_cache and not force_refresh:
        cached = _read_cache(cache_name)
        if cached is not None:
            logger.debug("Using cached marketplace data for '%s'", source.name)
            meta = _read_stale_meta(cache_name) or {}
            return parse_marketplace_json(
                cached,
                source.name,
                source_url=source.url if source.kind == "url" else "",
                source_digest=meta.get("index_digest", "") if source.kind == "url" else "",
            )

    # Fetch from source
    try:
        if source.kind == "url":
            stale_meta = _read_stale_meta(cache_name) or {}
            result = _fetch_url_direct(
                source.url,
                etag=stale_meta.get("etag", ""),
                last_modified=stale_meta.get("last_modified", ""),
            )
            if result is None:
                stale = _read_stale_cache(cache_name)
                if stale is None:
                    raise MarketplaceFetchError(
                        source.name, "got 304 Not Modified but no cached data is available"
                    )
                _write_cache(
                    cache_name,
                    stale,
                    index_digest=stale_meta.get("index_digest", ""),
                    etag=stale_meta.get("etag", ""),
                    last_modified=stale_meta.get("last_modified", ""),
                )
                return parse_marketplace_json(
                    stale,
                    source.name,
                    source_url=source.url,
                    source_digest=stale_meta.get("index_digest", ""),
                )
            _write_cache(
                cache_name,
                result.data,
                index_digest=result.digest,
                etag=result.etag,
                last_modified=result.last_modified,
            )
            return parse_marketplace_json(
                result.data,
                source.name,
                source_url=source.url,
                source_digest=result.digest,
            )

        data = _fetch_file(source, source.path, auth_resolver=auth_resolver)
        if data is None:
            raise MarketplaceFetchError(
                source.name,
                f"marketplace.json not found at '{source.path}' in {source.display_source}",
            )
        if use_sidecar_cache:
            _write_cache(cache_name, data)
        return parse_marketplace_json(data, source.name)
    except MarketplaceFetchError:
        # Stale-while-revalidate (API kinds only): serve expired cache on network error
        if use_sidecar_cache:
            stale = _read_stale_cache(cache_name)
            if stale is not None:
                logger.warning("Network error fetching '%s'; using stale cache", source.name)
                meta = _read_stale_meta(cache_name) or {}
                return parse_marketplace_json(
                    stale,
                    source.name,
                    source_url=source.url if source.kind == "url" else "",
                    source_digest=meta.get("index_digest", "") if source.kind == "url" else "",
                )
        raise


def fetch_or_cache(
    source: MarketplaceSource,
    *,
    auth_resolver: object | None = None,
) -> MarketplaceManifest:
    """Convenience wrapper -- same as ``fetch_marketplace`` with defaults."""
    return fetch_marketplace(source, auth_resolver=auth_resolver)


def search_marketplace(
    query: str,
    source: MarketplaceSource,
    *,
    auth_resolver: object | None = None,
) -> list[MarketplacePlugin]:
    """Search a single marketplace for plugins matching *query*."""
    manifest = fetch_marketplace(source, auth_resolver=auth_resolver)
    return manifest.search(query)


def search_all_marketplaces(
    query: str,
    *,
    auth_resolver: object | None = None,
) -> list[MarketplacePlugin]:
    """Search across all registered marketplaces.

    Returns plugins matching the query, annotated with their source marketplace.
    """
    results: list[MarketplacePlugin] = []
    for source in get_registered_marketplaces():
        try:
            manifest = fetch_marketplace(source, auth_resolver=auth_resolver)
            results.extend(manifest.search(query))
        except MarketplaceFetchError as exc:
            logger.warning("Skipping marketplace '%s': %s", source.name, exc)
    return results


def clear_marketplace_cache(
    name: str | None = None,
    host: str = "github.com",
    source: MarketplaceSource | None = None,
) -> int:
    """Clear cached data for one or all marketplaces.

    Returns the number of caches cleared.
    """
    if source is not None:
        _clear_cache(_cache_key(source))
        return 1
    if name:
        # Build a minimal source to derive the cache key
        _src = MarketplaceSource(name=name, owner="", repo="", host=host)
        _clear_cache(_cache_key(_src))
        return 1
    count = 0
    for registered_source in get_registered_marketplaces():
        _clear_cache(_cache_key(registered_source))
        count += 1
    return count
