"""Low-level HTTP helpers and direct-URL fetch logic for marketplace clients.

Extracted from client.py to keep that module under 800 lines.
All names are re-exported from client.py so existing import paths keep working.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from .errors import MarketplaceFetchError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP session and payload limits
# ---------------------------------------------------------------------------

_MAX_MARKETPLACE_JSON_BYTES = 10 * 1024 * 1024
_HTTP_CHUNK_BYTES = 1024 * 1024
_HTTP_SESSION = requests.Session()
_HTTP_SESSION.max_redirects = 5

# ---------------------------------------------------------------------------
# Data transfer object for direct-URL fetch results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    """Cache-layer DTO for a direct remote marketplace.json fetch."""

    data: dict
    digest: str
    etag: str = ""
    last_modified: str = ""


# ---------------------------------------------------------------------------
# Shared HTTP GET helper
# ---------------------------------------------------------------------------


def _active_http_session():
    """Return the shared session, honouring test overrides on ``client._HTTP_SESSION``.

    ``client.py`` re-exports ``_HTTP_SESSION`` from this module, so test suites
    patch the attribute on the public ``client`` module rather than here.  We
    late-bind through ``client`` so those overrides take effect; the lookup
    falls back to this module's session when ``client`` is unavailable.
    """
    with contextlib.suppress(ImportError):
        from . import client as _client_mod

        return getattr(_client_mod, "_HTTP_SESSION", _HTTP_SESSION)
    return _HTTP_SESSION


def _active_max_json_bytes() -> int:
    """Return the payload cap, honouring test overrides on ``client._MAX_MARKETPLACE_JSON_BYTES``.

    Late-bound through ``client`` for the same reason as :func:`_active_http_session`:
    the constant is re-exported there and suites patch the public module.
    """
    with contextlib.suppress(ImportError):
        from . import client as _client_mod

        return getattr(_client_mod, "_MAX_MARKETPLACE_JSON_BYTES", _MAX_MARKETPLACE_JSON_BYTES)
    return _MAX_MARKETPLACE_JSON_BYTES


def _http_get(url: str, **kwargs: object):
    """Issue HTTP GET through a shared session without persisting cookies."""
    session = _active_http_session()
    cookies = getattr(session, "cookies", None)
    if cookies is not None:
        cookies.clear()
    response = session.get(url, **kwargs)
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


# ---------------------------------------------------------------------------
# Direct remote marketplace.json URL fetch
# ---------------------------------------------------------------------------


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
        max_json_bytes = _active_max_json_bytes()
        if content_length:
            with contextlib.suppress(ValueError):
                if int(content_length) > max_json_bytes:
                    raise MarketplaceFetchError(
                        url,
                        f"marketplace.json exceeds {max_json_bytes} bytes",
                    )

        raw = _read_bounded_response_bytes(resp, url, max_json_bytes)
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
# Registry proxy helpers (used by both GitHub and GitLab API paths)
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
    source,
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


def _parse_json_text(resp) -> dict:
    try:
        return json.loads(resp.text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Invalid JSON in marketplace file: {exc}") from exc
