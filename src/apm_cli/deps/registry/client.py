"""HTTP client for the dedicated registry API.

Implements docs/proposals/registry-api.md §5:

- ``GET /v1/packages/{owner}/{repo}/versions`` — list versions
- ``GET /v1/packages/{owner}/{repo}/versions/{version}/tarball`` — fetch tarball
- ``GET /v1/search`` — server-side search

The publish endpoint (``PUT .../versions/{version}``) is documented in the
proposal but intentionally NOT implemented in this PR — operators publish via
``curl`` until ``apm publish`` lands as a follow-up.

Design notes:

- All endpoints use ``Authorization: Bearer <token>`` when an env-var token is
  configured for the registry. Anonymous fetch is the fallback (§6.2 rule 2).
- Errors surface as ``RegistryError`` carrying the HTTP status and a parsed
  RFC 7807 Problem Details body when present. The install path turns 401/403
  into the §6.2 remediation message at a higher level.
- No HTTP cache layer in v1 — ``Cache-Control: max-age=60`` from the server is
  advisory only. In-process memoization can be added later.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import requests

from .auth import RegistryAuthContext


class RegistryError(Exception):
    """A registry HTTP call failed.

    ``status`` is the HTTP status code (or ``None`` for transport-level
    failures); ``problem`` is the parsed RFC 7807 body when available.
    """

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        problem: Optional[Dict[str, Any]] = None,
        url: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.problem = problem or {}
        self.url = url


@dataclass(frozen=True)
class VersionEntry:
    """One row from ``GET /v1/packages/.../versions``."""

    version: str
    digest: str
    published_at: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VersionEntry":
        version = payload.get("version")
        digest = payload.get("digest")
        if not isinstance(version, str) or not version:
            raise RegistryError(
                f"malformed version entry (missing 'version'): {payload!r}"
            )
        if not isinstance(digest, str) or not digest:
            raise RegistryError(
                f"malformed version entry (missing 'digest') for {version!r}"
            )
        published = payload.get("published_at")
        return cls(
            version=version,
            digest=digest,
            published_at=published if isinstance(published, str) else None,
        )


@dataclass(frozen=True)
class SearchResult:
    """One row from ``GET /v1/search``."""

    id: str
    latest_version: Optional[str]
    description: Optional[str]
    author: Optional[str]
    tags: List[str]
    type: Optional[str]
    score: Optional[float]

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SearchResult":
        return cls(
            id=str(payload.get("id", "")),
            latest_version=payload.get("latest_version"),
            description=payload.get("description"),
            author=payload.get("author"),
            tags=list(payload.get("tags") or []),
            type=payload.get("type"),
            score=payload.get("score"),
        )


_DEFAULT_TIMEOUT = (10, 60)  # (connect, read) seconds


class RegistryClient:
    """Minimal HTTP client for the registry API.

    One client per registry URL. Stateless aside from the auth context — safe
    to instantiate per install or share for an entire resolution graph.
    """

    def __init__(
        self,
        base_url: str,
        auth: RegistryAuthContext,
        *,
        session: Optional[requests.Session] = None,
        timeout: Optional[tuple] = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        # Strip trailing slash so we can join cleanly.
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._session = session or requests.Session()
        self._timeout = timeout or _DEFAULT_TIMEOUT

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        headers = {"Accept": accept}
        auth_header = self._auth.auth_header()
        if auth_header:
            headers["Authorization"] = auth_header
        return headers

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/json",
        stream: bool = False,
    ) -> requests.Response:
        url = self._url(path)
        try:
            # Pass ``url`` as a keyword so test doubles can inspect it without
            # depending on positional-argument ordering.
            response = self._session.request(
                method,
                url=url,
                headers=self._headers(accept=accept),
                timeout=self._timeout,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise RegistryError(
                f"transport error talking to registry: {exc}",
                url=url,
            ) from exc
        if response.status_code >= 400:
            problem: Dict[str, Any] = {}
            try:
                ctype = response.headers.get("Content-Type", "")
                if "json" in ctype:
                    problem = response.json()
            except (ValueError, json.JSONDecodeError):
                problem = {}
            raise RegistryError(
                _format_error(response.status_code, problem, url),
                status=response.status_code,
                problem=problem,
                url=url,
            )
        return response

    # ------------------------------------------------------------------ §5.1
    def list_versions(self, owner: str, repo: str) -> List[VersionEntry]:
        """``GET /v1/packages/{owner}/{repo}/versions``."""
        path = f"/v1/packages/{_quote(owner)}/{_quote(repo)}/versions"
        response = self._request("GET", path)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RegistryError(
                f"registry returned non-JSON for /versions: {exc}",
                url=response.url,
            ) from exc
        raw_versions = payload.get("versions") if isinstance(payload, dict) else None
        if not isinstance(raw_versions, list):
            raise RegistryError(
                f"registry response missing 'versions' array: {payload!r}",
                url=response.url,
            )
        return [VersionEntry.from_dict(row) for row in raw_versions]

    # ------------------------------------------------------------------ §5.2
    def download_archive(
        self,
        owner: str,
        repo: str,
        version: str,
    ) -> tuple[bytes, str]:
        """``GET /v1/packages/{owner}/{repo}/versions/{version}/download``.

        Endpoint is format-neutral; the server replies with ``application/gzip``
        (tar.gz) or ``application/zip`` (Anthropic skills format) and the
        client dispatches on content type.

        Returns ``(body_bytes, content_type)``. Caller is responsible for
        sha256 verification (use ``extractor.verify_sha256``) and for picking
        the right extractor (``extractor.extract_archive`` does this for you).
        """
        path = (
            f"/v1/packages/{_quote(owner)}/{_quote(repo)}"
            f"/versions/{_quote(version)}/download"
        )
        # Accept both archive types; v1 doesn't constrain via Accept (the
        # publisher chose the format at upload time).
        response = self._request(
            "GET", path, accept="application/gzip, application/zip"
        )
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        return response.content, content_type

    def archive_url(self, owner: str, repo: str, version: str) -> str:
        """The canonical ``resolved_url`` for a given (owner, repo, version)."""
        return self._url(
            f"/v1/packages/{_quote(owner)}/{_quote(repo)}"
            f"/versions/{_quote(version)}/download"
        )

    # Backwards-compat aliases for the early naming. Will be removed before
    # the feature is released; kept here so any in-flight call sites in tests
    # or experimental scripts don't break during the rename. New code should
    # call ``download_archive`` / ``archive_url``.
    def download_tarball(self, owner: str, repo: str, version: str) -> bytes:
        body, _ = self.download_archive(owner, repo, version)
        return body

    def tarball_url(self, owner: str, repo: str, version: str) -> str:
        return self.archive_url(owner, repo, version)

    # ------------------------------------------------------------------ §5.4
    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        offset: int = 0,
        type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[SearchResult]:
        """``GET /v1/search``."""
        params: Dict[str, Any] = {"q": query, "limit": limit, "offset": offset}
        if type:
            params["type"] = type
        if tag:
            params["tag"] = tag
        path = "/v1/search?" + urllib.parse.urlencode(params)
        response = self._request("GET", path)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RegistryError(
                f"registry returned non-JSON for /search: {exc}",
                url=response.url,
            ) from exc
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        return [SearchResult.from_dict(row) for row in rows]


def _quote(s: str) -> str:
    """Percent-encode a path segment, allowing ``.`` and ``-`` raw."""
    return urllib.parse.quote(s, safe=".-")


def _format_error(status: int, problem: Mapping[str, Any], url: str) -> str:
    title = problem.get("title") if isinstance(problem, Mapping) else None
    detail = problem.get("detail") if isinstance(problem, Mapping) else None
    body = " - ".join(part for part in (title, detail) if part)
    if body:
        return f"registry HTTP {status} from {url}: {body}"
    return f"registry HTTP {status} from {url}"
