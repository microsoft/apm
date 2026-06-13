"""HTTP client for the dedicated registry API.

Implements docs/proposals/registry-api.md §5:

- ``GET /v1/packages/{owner}/{repo}/versions`` — list versions
- ``GET /v1/packages/{owner}/{repo}/versions/{version}/download`` — fetch archive

- ``PUT /v1/packages/{owner}/{repo}/versions/{version}`` — publish

Design notes:

- All endpoints use ``Authorization: Bearer <token>`` when an env-var token is
  configured for the registry. Anonymous fetch is the fallback (§6.2 rule 2).
- Errors surface as ``RegistryError`` carrying the HTTP status and a parsed
  RFC 7807 Problem Details body when present. The install path turns 401/403
  into the §6.2 remediation message at a higher level.
- No HTTP cache layer — ``Cache-Control: max-age=60`` from the server is
  advisory only. In-process memoization can be added later.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
        status: int | None = None,
        problem: dict[str, Any] | None = None,
        url: str | None = None,
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
    published_at: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> VersionEntry:
        version = payload.get("version")
        digest = payload.get("digest")
        if not isinstance(version, str) or not version:
            raise RegistryError(f"malformed version entry (missing 'version'): {payload!r}")
        if not isinstance(digest, str) or not digest:
            raise RegistryError(f"malformed version entry (missing 'digest') for {version!r}")
        # ``published_at`` is the spec-canonical key (snake_case throughout
        # — see registry-http-api.md §3.1). The client is intentionally
        # strict: a server that emits ``publishedAt`` (camelCase) is non-
        # conformant. Accepting both would mask spec drift; reject silently
        # by reading only the canonical name.
        published = payload.get("published_at")
        if not isinstance(published, str) or not published:
            raise RegistryError(f"malformed version entry (missing 'published_at') for {version!r}")
        return cls(
            version=version,
            digest=digest,
            published_at=published,
        )


@dataclass(frozen=True)
class PublishResult:
    """Response from ``PUT /v1/packages/.../versions/{version}``."""

    package: str  # "owner/repo"
    version: str
    digest: str  # "sha256:abc123..."
    published_at: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PublishResult:
        package = payload.get("package")
        version = payload.get("version")
        digest = payload.get("digest")
        if not isinstance(package, str) or not package:
            raise RegistryError(f"malformed publish response (missing 'package'): {payload!r}")
        if not isinstance(version, str) or not version:
            raise RegistryError(f"malformed publish response (missing 'version'): {payload!r}")
        if not isinstance(digest, str) or not digest:
            raise RegistryError(f"malformed publish response (missing 'digest'): {payload!r}")
        published = payload.get("published_at")
        return cls(
            package=package,
            version=version,
            digest=digest,
            published_at=published if isinstance(published, str) else None,
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
        session: requests.Session | None = None,
        timeout: tuple[float, float] | None = None,
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

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
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
            problem: dict[str, Any] = {}
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

    def _response_json(self, response: requests.Response, endpoint: str) -> Any:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RegistryError(
                f"registry returned non-JSON for {endpoint}: {exc}",
                url=response.url,
            ) from exc

    # ------------------------------------------------------------------ §5.1
    def list_versions(self, owner: str, repo: str) -> list[VersionEntry]:
        """``GET /v1/packages/{owner}/{repo}/versions``."""
        path = f"/v1/packages/{_quote(owner)}/{_quote(repo)}/versions"
        response = self._request("GET", path)
        payload = self._response_json(response, "/versions")
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
        path = f"/v1/packages/{_quote(owner)}/{_quote(repo)}/versions/{_quote(version)}/download"
        # Accept both archive types; v1 doesn't constrain via Accept (the
        # publisher chose the format at upload time).
        response = self._request("GET", path, accept="application/gzip, application/zip")
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        return response.content, content_type

    def archive_url(self, owner: str, repo: str, version: str) -> str:
        """The canonical ``resolved_url`` for a given (owner, repo, version)."""
        return self._url(
            f"/v1/packages/{_quote(owner)}/{_quote(repo)}/versions/{_quote(version)}/download"
        )

    def fetch_from_url(self, url: str) -> tuple[bytes, str]:
        """Fetch an absolute URL with auth headers (lockfile replay path).

        Unlike ``_request``, this takes a fully-qualified URL (not a path
        relative to ``_base_url``), so the caller controls the exact endpoint.
        Used by ``RegistryPackageResolver.download_from_lockfile`` to fetch
        from the URL recorded in the lockfile without re-querying ``/versions``.
        """
        try:
            response = self._session.request(
                "GET",
                url=url,
                headers=self._headers(accept="application/gzip, application/zip"),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise RegistryError(f"transport error fetching {url}: {exc}", url=url) from exc
        if response.status_code >= 400:
            problem: dict[str, Any] = {}
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
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        return response.content, content_type

    # ------------------------------------------------------------------ §5.3
    def publish_version(
        self,
        owner: str,
        repo: str,
        version: str,
        archive_bytes: bytes,
    ) -> PublishResult:
        """``PUT /v1/packages/{owner}/{repo}/versions/{version}`` — publish.

        Uploads *archive_bytes* (an ``application/zip`` archive produced by
        ``apm publish``) and returns the registry's 201 response as a
        ``PublishResult``.

        Errors surface as ``RegistryError`` with the HTTP status set:

        - 403 -- caller lacks publish permission for this owner/repo
        - 409 -- version already exists (immutable; republish is rejected)
        - 422 -- server-side lint/validation failed
        """
        path = f"/v1/packages/{_quote(owner)}/{_quote(repo)}/versions/{_quote(version)}"
        url = self._url(path)
        headers = self._headers(accept="application/json")
        headers["Content-Type"] = "application/zip"
        try:
            response = self._session.request(
                "PUT",
                url=url,
                headers=headers,
                data=archive_bytes,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise RegistryError(
                f"transport error talking to registry: {exc}",
                url=url,
            ) from exc
        if response.status_code >= 400:
            problem: dict[str, Any] = {}
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
        body = (response.content or b"").strip()
        if not body:
            # Some registries (e.g. JFrog) return 201 with an empty body.
            import hashlib

            digest = f"sha256:{hashlib.sha256(archive_bytes).hexdigest()}"
            return PublishResult(
                package=f"{owner}/{repo}",
                version=version,
                digest=digest,
                published_at=None,
            )
        payload = self._response_json(response, "PUT /versions")
        return PublishResult.from_dict(payload)


def _quote(s: str) -> str:
    """Percent-encode a path segment, allowing ``.``, ``-``, and ``_`` raw.

    ``_`` is an RFC 3986 unreserved character and must not be percent-encoded;
    omitting it caused owner/repo names containing underscores to be serialised
    as ``%5F`` in registry URLs.
    """
    return urllib.parse.quote(s, safe=".-_")


def _format_error(status: int, problem: Mapping[str, Any], url: str) -> str:
    title = problem.get("title") if isinstance(problem, Mapping) else None
    detail = problem.get("detail") if isinstance(problem, Mapping) else None
    body = " - ".join(part for part in (title, detail) if part)
    if body:
        return f"registry HTTP {status} from {url}: {body}"
    return f"registry HTTP {status} from {url}"
