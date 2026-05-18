"""HTTP error-handling helpers for GitHub file downloads.

Extracted from :mod:`_git_github_download` to keep that module under
400 lines.  All names are private to the ``download_strategies`` package;
the public API surface lives in :mod:`git_strategy`.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from ...models.apm_package import DependencyReference
from ...utils.github_host import default_host
from ._git_host_utils import _MissingFileCtx


@dataclass
class _ContentsApi404Ctx:
    """Context bundle for :func:`_handle_contents_api_404`."""

    host: str
    owner: str
    repo: str
    dep_ref: object
    file_path: str
    ref: str
    headers: dict
    api_url_candidates: list
    is_github_host: bool
    verbose_callback: object


def _check_is_rate_limit_by_header(e: requests.exceptions.HTTPError, is_github_host: bool) -> bool:
    """Return True when a 403 response carries GitHub rate-limit headers."""
    if not is_github_host:
        return False
    try:
        rl_remaining = e.response.headers.get("X-RateLimit-Remaining")
        if rl_remaining is not None and int(rl_remaining) == 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


@dataclass
class _HttpDownloadContext:
    dep_ref: DependencyReference
    file_path: str
    ref: str
    token: str | None
    is_github_host: bool
    api_url: str
    verbose_callback: object


def _can_retry_unauth(token, is_github_host: bool, host: str) -> bool:
    """Return True when an unauthenticated public-repo retry is worth attempting.

    GHES/GHE-DR clusters don't support unauthenticated org-scoped fetches.
    """
    return bool(token and is_github_host and not host.lower().endswith(".ghe.com"))


def _handle_http_401_or_403(
    self,
    e: requests.exceptions.HTTPError,
    ctx: _HttpDownloadContext,
) -> bytes:
    """Handle a 401/403 from the GitHub Contents API.

    Extracted from :func:`download_github_file` to reduce its statement
    count within the configured Ruff thresholds.  Raises ``RuntimeError``
    on auth failure; returns ``bytes`` when the unauthenticated public-repo
    retry succeeds.
    """
    dep_ref = ctx.dep_ref
    file_path = ctx.file_path
    ref = ctx.ref
    token = ctx.token
    is_github_host = ctx.is_github_host
    api_url = ctx.api_url
    verbose_callback = ctx.verbose_callback
    host = dep_ref.host or default_host()
    owner = dep_ref.repo_url.split("/", 1)[0]

    # Distinguish rate limiting from auth failure.
    is_rate_limit = _check_is_rate_limit_by_header(e, is_github_host)

    if is_rate_limit:
        error_msg = f"GitHub API rate limit exceeded for {dep_ref.repo_url}. "
        if not token:
            error_msg += (
                "Unauthenticated requests are limited to "
                "60/hour (shared per IP). "
                + self._host.auth_resolver.build_error_context(
                    host,
                    "API request (rate limited)",
                    org=owner,
                    port=(dep_ref.port if dep_ref else None),
                    dep_url=(dep_ref.repo_url if dep_ref else None),
                )
            )
        else:
            error_msg += (
                "Authenticated rate limit exhausted. "
                "Wait a few minutes or check your token's "
                "rate-limit quota."
            )
        raise RuntimeError(error_msg) from e

    # Retry without auth -- the repo might be public.
    # GHES/GHE-DR don't support unauthenticated org-scoped retries.
    if _can_retry_unauth(token, is_github_host, host):
        try:
            unauth_headers: dict[str, str] = {"Accept": "application/vnd.github.v3.raw"}
            response = self._host._resilient_get(api_url, headers=unauth_headers, timeout=30)
            response.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return self._extract_contents_api_payload(response, is_github_host)
        except requests.exceptions.HTTPError:
            pass  # Fall through to the original error

    error_msg = f"Authentication failed for {dep_ref.repo_url} (file: {file_path}, ref: {ref}). "
    if not token:
        error_msg += self._host.auth_resolver.build_error_context(
            host,
            "download",
            org=owner,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
    elif is_github_host and not host.lower().endswith(".ghe.com"):
        error_msg += (
            "Both authenticated and unauthenticated access "
            "were attempted. The repository may be private, "
            "or your token may lack SSO/SAML authorization "
            "for this organization."
        )
    elif is_github_host:
        error_msg += "Please check your GitHub token permissions."
    else:
        # Generic host: don't claim SSO/SAML or "GitHub token".
        error_msg += (
            f"Host {host} rejected the request. "
            "Verify the repository exists and that the token has "
            "access. Tokens are sourced from your git credential "
            "helper, a per-org GITHUB_APM_PAT_<ORG> env var, or "
            f"GITHUB_HOST={host} when this host is your GitHub "
            "Enterprise Server."
        )
    raise RuntimeError(error_msg)  # noqa: B904


def _try_default_branch_swap(self, ctx: _ContentsApi404Ctx, fallback_ref: str) -> bytes:
    """Retry with the other default branch (main<->master) and raise if all candidates 404."""
    fallback_url_candidates = self._build_contents_api_urls(
        ctx.host, ctx.owner, ctx.repo, ctx.file_path, fallback_ref
    )
    for fallback_url in fallback_url_candidates:
        try:
            response = self._host._resilient_get(fallback_url, headers=ctx.headers, timeout=30)
            response.raise_for_status()
            if ctx.verbose_callback:
                ctx.verbose_callback(
                    f"Downloaded file: {ctx.host}/{ctx.dep_ref.repo_url}/{ctx.file_path}"
                )
            return self._extract_contents_api_payload(response, ctx.is_github_host)
        except requests.exceptions.HTTPError as fe:
            if fe.response.status_code != 404:
                raise RuntimeError(
                    f"Failed to download {ctx.file_path}: HTTP {fe.response.status_code}"
                ) from fe
    raise RuntimeError(  # noqa: B904
        self._build_unsupported_or_missing_error(
            _MissingFileCtx(
                host=ctx.host,
                repo_url=ctx.dep_ref.repo_url,
                file_path=ctx.file_path,
                ref=ctx.ref,
                api_url_candidates=ctx.api_url_candidates,
                is_github_host=ctx.is_github_host,
                fallback_ref=fallback_ref,
            )
        )
    )


def _handle_contents_api_404(self, ctx: _ContentsApi404Ctx) -> bytes:
    """Handle a 404 from the primary Contents-API URL candidate.

    For generic hosts, works through remaining API-version candidates
    (v1 -> v3) before attempting a main/master branch swap.  Raises a
    descriptive ``RuntimeError`` when all candidates and both default
    branches are exhausted.

    Extracted from :func:`download_github_file` to reduce its branch count
    below the configured PLR0912/C901 thresholds.
    """
    # For generic hosts, try remaining API version candidates before ref fallback.
    for candidate_url in ctx.api_url_candidates[1:]:
        try:
            if ctx.verbose_callback:
                ctx.verbose_callback(f"Contents API 404; trying next candidate: {candidate_url}")
            candidate_resp = self._host._resilient_get(
                candidate_url, headers=ctx.headers, timeout=30
            )
            candidate_resp.raise_for_status()
            if ctx.verbose_callback:
                ctx.verbose_callback(
                    f"Downloaded file: {ctx.host}/{ctx.dep_ref.repo_url}/{ctx.file_path}"
                )
            return self._extract_contents_api_payload(candidate_resp, ctx.is_github_host)
        except requests.exceptions.HTTPError as ce:
            if ce.response.status_code != 404:
                raise RuntimeError(  # noqa: B904
                    f"Failed to download {ctx.file_path}: HTTP {ce.response.status_code}"
                )

    # Non-default refs have no branch-swap fallback.
    if ctx.ref not in ("main", "master"):
        raise RuntimeError(  # noqa: B904
            self._build_unsupported_or_missing_error(
                _MissingFileCtx(
                    host=ctx.host,
                    repo_url=ctx.dep_ref.repo_url,
                    file_path=ctx.file_path,
                    ref=ctx.ref,
                    api_url_candidates=ctx.api_url_candidates,
                    is_github_host=ctx.is_github_host,
                )
            )
        )

    # Try the other default branch (main <-> master).
    fallback_ref = "master" if ctx.ref == "main" else "main"
    return _try_default_branch_swap(self, ctx, fallback_ref)
