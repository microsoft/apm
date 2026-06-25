"""Fetch, parse, and cache marketplace.json from Git hosting repositories.

Dispatches over a ``_FETCHERS`` table keyed by ``source.kind``:

- ``github`` / ``gitlab`` -> host file API via ``_fetch_via_api`` (auth-routed
  through ``AuthResolver.try_with_fallback`` and the JSON sidecar cache).
- ``ado`` -> Azure DevOps REST items API (``_fetch_ado``, auth-routed through
  ``AuthResolver.try_with_fallback`` with the JSON sidecar cache), falling back
  to the generic-git path on any REST/transport failure.
- ``git`` -> generic git URL (Gitea, self-hosted, etc.) via subprocess
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

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from .errors import MarketplaceError, MarketplaceFetchError
from .models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
    parse_marketplace_json,
)
from .registry import get_registered_marketplaces

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    """Cache-layer DTO for a direct remote marketplace.json fetch."""

    data: dict
    digest: str
    etag: str = ""
    last_modified: str = ""


_CACHE_TTL_SECONDS = 3600  # 1 hour
_MAX_MARKETPLACE_JSON_BYTES = 10 * 1024 * 1024
_HTTP_CHUNK_BYTES = 1024 * 1024
_CACHE_DIR_NAME = os.path.join("cache", "marketplace")
_HTTP_SESSION = requests.Session()
_HTTP_SESSION.max_redirects = 5

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


def _cache_dir() -> str:
    """Return the cache directory, creating it if needed."""
    from ..config import CONFIG_DIR

    d = os.path.join(CONFIG_DIR, _CACHE_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _sanitize_cache_name(name: str) -> str:
    """Sanitize marketplace name for safe use in file paths."""
    from ..utils.path_security import PathTraversalError, validate_path_segments

    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    # Prevent path traversal even after sanitization
    safe = safe.strip(".").strip("_") or "unnamed"
    # Defense-in-depth: validate with centralized path security
    try:
        validate_path_segments(safe, context="cache name")
    except PathTraversalError:
        safe = "unnamed"
    return safe


def _cache_key(source: MarketplaceSource) -> str:
    """Cache key that includes kind+host to avoid collisions across hosts."""
    kind = source.kind
    if kind == "url":
        return f"url__{hashlib.sha256(source.url.encode()).hexdigest()[:16]}"
    if kind == "local":
        return f"local__{_sanitize_cache_name(source.name)}"
    if kind in ("git", "ado"):
        # Generic git / ADO: include host so a.com/o/r vs b.com/o/r never
        # collapse, and prefix by kind so the same host on the two paths keeps
        # distinct sidecar files.
        host = _host_from_url(source.url) or source.host or "unknown"
        return f"{kind}__{_sanitize_cache_name(host)}__{_sanitize_cache_name(source.name)}"
    normalized_host = (source.host or "github.com").lower()
    if normalized_host == "github.com":
        return source.name
    return f"{_sanitize_cache_name(normalized_host)}__{source.name}"


def _cache_data_path(name: str) -> str:
    return os.path.join(_cache_dir(), f"{_sanitize_cache_name(name)}.json")


def _cache_meta_path(name: str) -> str:
    return os.path.join(_cache_dir(), f"{_sanitize_cache_name(name)}.meta.json")


def _read_cache(name: str) -> dict | None:
    """Read cached marketplace data if valid (not expired)."""
    data_path = _cache_data_path(name)
    meta_path = _cache_meta_path(name)
    if not os.path.exists(data_path) or not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        fetched_at = meta.get("fetched_at", 0)
        ttl = meta.get("ttl_seconds", _CACHE_TTL_SECONDS)
        if time.time() - fetched_at > ttl:
            return None  # Expired
        with open(data_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, KeyError) as exc:
        logger.debug("Cache read failed for '%s': %s", name, exc)
        return None


def _read_stale_cache(name: str) -> dict | None:
    """Read cached data even if expired (stale-while-revalidate)."""
    data_path = _cache_data_path(name)
    if not os.path.exists(data_path):
        return None
    try:
        with open(data_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(
    name: str,
    data: dict,
    *,
    index_digest: str = "",
    etag: str = "",
    last_modified: str = "",
) -> None:
    """Write marketplace data and metadata to cache."""
    data_path = _cache_data_path(name)
    meta_path = _cache_meta_path(name)
    try:
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        meta = {"fetched_at": time.time(), "ttl_seconds": _CACHE_TTL_SECONDS}
        if index_digest:
            meta["index_digest"] = index_digest
        if etag:
            meta["etag"] = etag
        if last_modified:
            meta["last_modified"] = last_modified
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
    except OSError as exc:
        logger.debug("Cache write failed for '%s': %s", name, exc)


def _clear_cache(name: str) -> None:
    """Remove cached data for a marketplace."""
    for path in (_cache_data_path(name), _cache_meta_path(name)):
        with contextlib.suppress(OSError):
            os.remove(path)


def _read_stale_meta(name: str) -> dict | None:
    """Read cache metadata even when the data cache is expired."""
    meta_path = _cache_meta_path(name)
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Network fetch -- direct remote marketplace.json URL
# ---------------------------------------------------------------------------


def _http_get(url: str, **kwargs: object):
    """Issue HTTP GET through a shared session without persisting cookies."""
    cookies = getattr(_HTTP_SESSION, "cookies", None)
    if cookies is not None:
        cookies.clear()
    response = _HTTP_SESSION.get(url, **kwargs)
    if cookies is not None:
        cookies.clear()
    return response


def _read_bounded_response_bytes(resp, url: str, max_bytes: int) -> bytes:
    """Read response body from streaming chunks, enforcing *max_bytes*."""
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=_HTTP_CHUNK_BYTES):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise MarketplaceFetchError(url, f"marketplace.json exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_url_direct(
    url: str,
    *,
    etag: str = "",
    last_modified: str = "",
    expected_digest: str = "",
) -> FetchResult | None:
    """Fetch a remote marketplace.json URL over HTTPS.

    Returns ``None`` for HTTP 304 so callers can serve cached data.
    """
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise MarketplaceFetchError(url, "remote marketplace.json URLs must use HTTPS")

    headers = {"User-Agent": "apm-cli"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    resp = None
    try:
        resp = _http_get(url, headers=headers, timeout=30, stream=True)
    except requests.exceptions.RequestException as exc:
        raise MarketplaceFetchError(url, str(exc)) from exc

    try:
        final_url = getattr(resp, "url", url)
        if isinstance(final_url, str) and urlsplit(final_url).scheme.lower() != "https":
            raise MarketplaceFetchError(url, "redirect to non-HTTPS URL rejected")

        if resp.status_code == 304:
            return None
        if resp.status_code == 404:
            raise MarketplaceFetchError(url, "404 Not Found")

        try:
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            raise MarketplaceFetchError(url, str(exc)) from exc

        content_length = resp.headers.get("Content-Length", "")
        if content_length:
            with contextlib.suppress(ValueError):
                if int(content_length) > _MAX_MARKETPLACE_JSON_BYTES:
                    raise MarketplaceFetchError(
                        url,
                        f"marketplace.json exceeds {_MAX_MARKETPLACE_JSON_BYTES} bytes",
                    )

        raw = _read_bounded_response_bytes(resp, url, _MAX_MARKETPLACE_JSON_BYTES)
    finally:
        if resp is not None:
            close = getattr(resp, "close", None)
            if callable(close):
                close()

    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if expected_digest and digest != expected_digest:
        raise MarketplaceFetchError(
            url, f"digest mismatch: expected {expected_digest}, got {digest}"
        )

    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MarketplaceFetchError(url, f"invalid JSON response: {exc}") from exc
    if not isinstance(data, dict):
        raise MarketplaceFetchError(url, "marketplace.json root must be an object")

    return FetchResult(
        data=data,
        digest=digest,
        etag=resp.headers.get("ETag", ""),
        last_modified=resp.headers.get("Last-Modified", ""),
    )


# ---------------------------------------------------------------------------
# Network fetch -- API path (GitHub / GitLab)
# ---------------------------------------------------------------------------


def _try_proxy_fetch_raw(
    owner: str,
    repo: str,
    file_path: str,
    ref: str,
) -> bytes | None:
    """Try to fetch a file as raw bytes via the registry proxy."""
    from ..deps.registry_proxy import RegistryConfig

    cfg = RegistryConfig.from_env()
    if cfg is None:
        return None

    from ..deps.artifactory_entry import fetch_entry_from_archive

    return fetch_entry_from_archive(
        host=cfg.host,
        prefix=cfg.prefix,
        owner=owner,
        repo=repo,
        file_path=file_path,
        ref=ref,
        scheme=cfg.scheme,
        headers=cfg.get_headers(),
    )


def _try_proxy_fetch(
    source: MarketplaceSource,
    file_path: str,
) -> dict | None:
    """Try to fetch marketplace JSON via the registry proxy.

    Returns parsed JSON dict on success, ``None`` when no proxy is
    configured or the entry download fails.
    """
    content = _try_proxy_fetch_raw(source.owner, source.repo, file_path, source.ref)
    if content is None:
        return None

    try:
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        logger.debug(
            "Proxy returned non-JSON for %s/%s %s",
            source.owner,
            source.repo,
            file_path,
        )
        return None


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


def _parse_json_text(resp) -> dict:
    try:
        return json.loads(resp.text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Invalid JSON in marketplace file: {exc}") from exc


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
# Network fetch -- Azure DevOps REST items API (fast path, git fallback)
# ---------------------------------------------------------------------------


class _AdoItemNotFound(Exception):
    """Sentinel: the ADO items API returned a confirmed 404 for the path.

    Distinguishes "the file is definitively absent at this ref" (map to
    ``None`` so ``_auto_detect_path`` can probe the next candidate) from a
    transport/auth failure (fall back to the generic-git clone path).
    """


def _ado_auth_header(token: str | None, git_env: dict | None) -> dict[str, str]:
    """Build the Azure DevOps ``Authorization`` header for a resolved token.

    ``AuthResolver.try_with_fallback`` hands the operation a ``(token, git_env)``
    pair but not the auth scheme. Bearer contexts carry the full
    ``Authorization: Bearer <jwt>`` header in ``GIT_CONFIG_VALUE_0`` (see
    ``AuthResolver._build_git_env``); detect that and emit the Bearer scheme.
    Otherwise treat the token as an ADO PAT and use HTTP Basic with
    ``base64(":" + PAT)`` per ADO's convention (empty username, PAT as
    password). Returns an empty dict for an anonymous request.

    The returned dict carries the credential -- callers MUST NOT log it.
    """
    if not token:
        return {}
    extra_header = (git_env or {}).get("GIT_CONFIG_VALUE_0", "").strip()
    if extra_header.lower().startswith("authorization: bearer "):
        return {"Authorization": f"Bearer {token}"}
    encoded = base64.b64encode(f":{token}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


def _fetch_ado_rest(
    source: MarketplaceSource,
    file_path: str,
    *,
    org: str,
    project: str,
    repo: str,
    host: str,
    auth_resolver,
) -> dict | None:
    """Read a single metadata file from Azure DevOps via the REST items API.

    Routes auth through ``AuthResolver.try_with_fallback`` for the ADO host so
    a resolved PAT (``ADO_APM_PAT``) is tried first and an AAD bearer (``az``)
    is the runtime fallback -- the same auth posture as the clone path. The
    token is never logged. Raises on any failure (network, auth, non-JSON,
    sign-in page) so the caller can fall back to the generic-git path; raises
    ``_AdoItemNotFound`` for a confirmed 404.
    """
    from ..utils.github_host import build_ado_api_url

    url = build_ado_api_url(org, project, repo, file_path, source.ref, host)

    def _do_fetch(token, git_env):
        headers = {"User-Agent": "apm-cli"}
        headers.update(_ado_auth_header(token, git_env))
        resp = _http_get(url, headers=headers, timeout=30)
        if resp.status_code == 404:
            # No message: this sentinel flows through ``try_with_fallback`` ->
            # ``is_ado_auth_failure_signal(str(exc))``; an empty string never
            # trips an auth-failure keyword, so a 404 never wastes a bearer
            # retry.
            raise _AdoItemNotFound
        # ADO answers an unauthenticated/under-scoped request with HTTP 200 +
        # an HTML sign-in page rather than a 401 (#1671). Treat that as an auth
        # failure so try_with_fallback can attempt the AAD bearer before we
        # give up and clone. The word "unauthorized" is load-bearing: it makes
        # ``is_ado_auth_failure_signal(str(exc))`` match, which is the gate the
        # PAT->bearer fallback checks (see AuthResolver._try_ado_bearer_fallback).
        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise MarketplaceFetchError(
                    source.name,
                    "Azure DevOps returned a sign-in page (unauthorized: authentication required)",
                )
        resp.raise_for_status()
        return _parse_json_text(resp)

    return auth_resolver.try_with_fallback(
        host,
        _do_fetch,
        org=org,
        path=f"{org}/{project}/{repo}",
        unauth_first=False,
    )


def _fetch_ado(
    source: MarketplaceSource,
    file_path: str,
    *,
    host_info,
    auth_resolver,
) -> dict | None:
    """Fetch marketplace.json from Azure DevOps, REST-first with git fallback.

    Optional latency optimisation over the generic-git path: ADO single-file
    metadata reads go through ``GET .../_apis/git/repositories/{repo}/items``
    instead of a subprocess clone, matching the GitHub/GitLab fast path.

    Falls back to ``_fetch_git`` (the subprocess clone) on any REST/transport
    failure or offline condition so there is no regression vs. the prior
    behaviour. A confirmed 404 returns ``None`` (the file is absent at this
    path) so ``_auto_detect_path`` can probe the next candidate without paying
    for a clone that would also miss.
    """
    from ..utils.github_host import parse_ado_repo_url

    parsed = parse_ado_repo_url(source.url)
    if parsed is None:
        # URL does not decompose into org/project/repo (unusual ADO shape) --
        # nothing to REST against, so use the generic-git path directly.
        return _fetch_git(source, file_path, host_info=host_info, auth_resolver=auth_resolver)

    org, project, repo = parsed
    host = host_info.host if host_info is not None else "dev.azure.com"
    try:
        return _fetch_ado_rest(
            source,
            file_path,
            org=org,
            project=project,
            repo=repo,
            host=host,
            auth_resolver=auth_resolver,
        )
    except _AdoItemNotFound:
        return None
    except Exception as exc:
        # REST failed (network, auth exhausted, sign-in page, malformed JSON,
        # 5xx, ...). Fall back to the clone path so offline/unusual repos keep
        # working. Sanitize exception text because requests exceptions can
        # include URLs with query parameters.
        from ..cache.git_cache import _sanitize_url

        logger.info(
            "ADO REST metadata fetch unavailable for '%s'; falling back to git.", source.name
        )
        logger.debug(
            "ADO REST metadata fetch failed for '%s'; falling back to generic-git: %s",
            source.name,
            _sanitize_url(str(exc)),
        )
        return _fetch_git(source, file_path, host_info=host_info, auth_resolver=auth_resolver)


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
    "ado": _fetch_ado,
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
    elif kind in ("git", "ado"):
        # For ADO and generic git, classify the host extracted from the URL so
        # each gets a correctly-typed auth context (ADO PAT/bearer routing).
        host = _host_from_url(source.url)
        host_info = AuthResolver.classify_host(host) if host else None

    return fetcher(source, file_path, host_info=host_info, auth_resolver=auth_resolver)


def _host_from_url(url: str) -> str:
    """Extract host from a URL (handles SCP-like SSH URLs too)."""
    if not url:
        return ""
    # SCP-like: git@host:path
    if "@" in url and not url.startswith(("http", "git://", "ssh://", "file://")):
        try:
            return url.split("@", 1)[1].split(":", 1)[0]
        except (IndexError, ValueError):
            return ""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


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

    Uses the JSON sidecar cache for ``kind in ("github", "gitlab", "ado",
    "url")``. Generic-git fetches rely on ``GitCache`` + ``git ls-remote`` for
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
    use_sidecar_cache = source.kind in ("github", "gitlab", "ado", "url")

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
