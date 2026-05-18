"""GitHub-specific file-download helpers for APM packages.

Implements the CDN fast-path (raw.githubusercontent.com), the Contents-API
download path, rate-limit/auth error handling, and the public
``download_github_file`` entry point.
All names are private to the ``download_strategies`` package; the public
API surface lives in :mod:`git_strategy` which re-exports everything.
"""

from dataclasses import dataclass

import requests

from ...models.apm_package import DependencyReference
from ...utils.github_host import build_raw_content_url, default_host, is_github_hostname
from ._git_github_errors import (
    _can_retry_unauth,
    _check_is_rate_limit_by_header,
    _ContentsApi404Ctx,
    _handle_contents_api_404,
    _handle_http_401_or_403,
    _HttpDownloadContext,
    _try_default_branch_swap,
)


@dataclass
class _RawUrlCtx:
    """Context bundle for :func:`_try_generic_host_raw_url`."""

    host: str
    owner: str
    repo: str
    ref: str
    file_path: str
    file_ctx: object
    dep_ref: object
    verbose_callback: object


@dataclass
class _CdnCtx:
    """Context bundle for :func:`_try_raw_cdn_download`."""

    owner: str
    repo: str
    ref: str
    file_path: str
    verbose_callback: object
    host: str
    dep_ref: DependencyReference


def try_raw_download(self, owner: str, repo: str, ref: str, file_path: str) -> bytes | None:
    """Attempt to fetch a file via raw.githubusercontent.com."""
    raw_url = build_raw_content_url(owner, repo, ref, file_path)
    try:
        response = requests.get(raw_url, timeout=30)
        if response.status_code == 200:
            return response.content
    except requests.exceptions.RequestException:
        pass
    return None


def _try_raw_cdn_download(self, ctx: _CdnCtx) -> bytes | None:
    """Try raw.githubusercontent.com first, then a default-branch fallback."""
    content = try_raw_download(self, ctx.owner, ctx.repo, ctx.ref, ctx.file_path)
    if content is not None:
        if ctx.verbose_callback:
            ctx.verbose_callback(
                f"Downloaded file: {ctx.host}/{ctx.dep_ref.repo_url}/{ctx.file_path}"
            )
        return content
    if ctx.ref in ("main", "master"):
        fallback_ref = "master" if ctx.ref == "main" else "main"
        content = try_raw_download(self, ctx.owner, ctx.repo, fallback_ref, ctx.file_path)
        if content is not None:
            if ctx.verbose_callback:
                ctx.verbose_callback(
                    f"Downloaded file: {ctx.host}/{ctx.dep_ref.repo_url}/{ctx.file_path}"
                )
            return content
    return None


def _try_generic_host_raw_url(self, ctx: _RawUrlCtx) -> bytes | None:
    """Try the raw URL path for a non-GitHub host before the API fallback."""
    raw_url = f"https://{ctx.host}/{ctx.owner}/{ctx.repo}/raw/{ctx.ref}/{ctx.file_path}"
    raw_headers = self._build_generic_host_auth_headers(ctx.host, ctx.file_ctx, accept=None)
    if ctx.verbose_callback:
        ctx.verbose_callback(f"Trying raw URL on generic host {ctx.host}: {raw_url}")
    try:
        response = self._host._resilient_get(raw_url, headers=raw_headers, timeout=30)
        if response.status_code == 200:
            if ctx.verbose_callback:
                ctx.verbose_callback(
                    f"Downloaded file: {ctx.host}/{ctx.dep_ref.repo_url}/{ctx.file_path}"
                )
            return response.content
    except (requests.RequestException, OSError) as raw_err:
        if ctx.verbose_callback:
            ctx.verbose_callback(
                f"Raw URL on {ctx.host} failed for {ctx.file_path}@{ctx.ref}: "
                f"{type(raw_err).__name__}; falling back to Contents API."
            )
    return None


def _build_contents_api_auth_headers(
    self, host: str, file_ctx, is_github_host: bool, token
) -> dict[str, str]:
    """Build HTTP request headers for the Contents API call."""
    accept = "application/vnd.github.v3.raw" if is_github_host else "application/json"
    if is_github_host:
        headers: dict[str, str] = {"Accept": accept}
        if token:
            headers["Authorization"] = f"token {token}"
        return headers
    return self._build_generic_host_auth_headers(host, file_ctx, accept=accept)


def _verbose_log_api_attempt(
    verbose_callback, host: str, is_github_host: bool, api_url: str
) -> None:
    """Emit a verbose-mode log line before issuing the Contents API request."""
    if verbose_callback and not is_github_host:
        verbose_callback(f"Trying Contents API on {host}: {api_url}")


def download_github_file(
    self,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str = "main",
    verbose_callback=None,
) -> bytes:
    """Download a file from GitHub repository.

    For github.com without a token, tries raw.githubusercontent.com first
    (CDN, no rate limit) before falling back to the Contents API.
    Authenticated requests and non-github.com hosts always use the
    Contents API directly.

    Args:
        dep_ref: Parsed dependency reference
        file_path: Path to file within the repository
        ref: Git reference (branch, tag, or commit SHA)
        verbose_callback: Optional callable for verbose logging

    Returns:
        bytes: File content
    """
    host = dep_ref.host or default_host()

    # Parse owner/repo from repo_url.  ``owner`` doubles as the org for
    # auth resolution -- no separate extraction needed.
    owner, repo = dep_ref.repo_url.split("/", 1)
    file_ctx = self._host.auth_resolver.resolve(host, owner, port=dep_ref.port)
    token = file_ctx.token

    # --- CDN fast-path for github.com without a token ---
    # raw.githubusercontent.com is served from GitHub's CDN and is not
    # subject to the REST API rate limit (60 req/h unauthenticated).
    # Only available for github.com -- GHES/GHE-DR have no equivalent.
    if host.lower() == "github.com" and not token:
        cdn_result = _try_raw_cdn_download(
            self,
            _CdnCtx(
                owner=owner,
                repo=repo,
                ref=ref,
                file_path=file_path,
                verbose_callback=verbose_callback,
                host=host,
                dep_ref=dep_ref,
            ),
        )
        if cdn_result is not None:
            return cdn_result
        # All raw attempts failed -- fall through to API path which
        # handles private repos, rate-limit messaging, and SAML errors.

    # --- Generic host: raw URL first, then API version negotiation ---
    # For non-GitHub non-GHE hosts (Gitea, Gogs, self-hosted git), try the
    # raw URL path first, then negotiate API versions v1 -> v3.
    is_github_host = is_github_hostname(host) or self._is_configured_ghes(host)
    if not is_github_host:
        raw_result = _try_generic_host_raw_url(
            self,
            _RawUrlCtx(
                host=host,
                owner=owner,
                repo=repo,
                ref=ref,
                file_path=file_path,
                file_ctx=file_ctx,
                dep_ref=dep_ref,
                verbose_callback=verbose_callback,
            ),
        )
        if raw_result is not None:
            return raw_result

    # --- Contents API path (authenticated, enterprise, or raw fallback) ---
    # Build API URL candidates - format differs by host type.
    api_url_candidates = self._build_contents_api_urls(host, owner, repo, file_path, ref)
    api_url = api_url_candidates[0]

    headers = _build_contents_api_auth_headers(self, host, file_ctx, is_github_host, token)

    # Issue the Contents API request.
    try:
        _verbose_log_api_attempt(verbose_callback, host, is_github_host, api_url)
        response = self._host._resilient_get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        if verbose_callback:
            verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
        return self._extract_contents_api_payload(response, is_github_host)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return _handle_contents_api_404(
                self,
                _ContentsApi404Ctx(
                    host=host,
                    owner=owner,
                    repo=repo,
                    dep_ref=dep_ref,
                    file_path=file_path,
                    ref=ref,
                    headers=headers,
                    api_url_candidates=api_url_candidates,
                    is_github_host=is_github_host,
                    verbose_callback=verbose_callback,
                ),
            )
        if e.response.status_code in (401, 403):
            return _handle_http_401_or_403(
                self,
                e,
                _HttpDownloadContext(
                    dep_ref=dep_ref,
                    file_path=file_path,
                    ref=ref,
                    token=token,
                    is_github_host=is_github_host,
                    api_url=api_url,
                    verbose_callback=verbose_callback,
                ),
            )
        raise RuntimeError(f"Failed to download {file_path}: HTTP {e.response.status_code}") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error downloading {file_path}: {e}") from e
