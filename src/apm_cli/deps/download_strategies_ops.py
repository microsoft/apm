"""GitHub file-download ops for
:class:`~apm_cli.deps.download_strategies.DownloadDelegate`.

Moved body of ``download_github_file`` plus its cohesive ``_gh_*`` helpers
(CDN fast-path, generic-host raw attempt, Contents-API request, 404 / auth
handling, message builders). Names that tests patch on
``apm_cli.deps.download_strategies`` are referenced through a function-level
``_ds`` alias so the patch still applies.
"""

import requests

from ..models.apm_package import DependencyReference


def download_github_file(
    delegate,
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
    from apm_cli.deps import download_strategies as _ds

    host = dep_ref.host or _ds.default_host()

    # Parse owner/repo from repo_url
    owner, repo = dep_ref.repo_url.split("/", 1)

    # Resolve token via AuthResolver for CDN fast-path decision
    org = None
    if dep_ref and dep_ref.repo_url:
        parts = dep_ref.repo_url.split("/")
        if parts:
            org = parts[0]
    file_ctx = delegate._host.auth_resolver.resolve(host, org, port=dep_ref.port)
    token = file_ctx.token

    # --- CDN fast-path for github.com without a token ---
    if host.lower() == "github.com" and not token:
        content = _gh_cdn_fastpath(
            delegate, host, owner, repo, ref, file_path, dep_ref, verbose_callback
        )
        if content is not None:
            return content
        # All raw attempts failed -- fall through to API path which handles
        # private repos, rate-limit messaging, and SAML errors.

    # --- Generic host: raw URL first, then API version negotiation ---
    is_github_host = _ds.is_github_hostname(host) or delegate._is_configured_ghes(host)
    if not is_github_host:
        content = _gh_generic_raw_attempt(
            delegate, host, owner, repo, ref, file_path, dep_ref, file_ctx, verbose_callback
        )
        if content is not None:
            return content

    # --- Contents API path (authenticated, enterprise, or raw fallback) ---
    return _gh_contents_api(
        delegate,
        host,
        owner,
        repo,
        file_path,
        ref,
        dep_ref,
        token,
        file_ctx,
        is_github_host,
        verbose_callback,
    )


def _gh_cdn_fastpath(
    delegate, host, owner, repo, ref, file_path, dep_ref, verbose_callback
) -> bytes | None:
    """Try raw.githubusercontent.com (CDN) for github.com, both default branches."""
    content = delegate.try_raw_download(owner, repo, ref, file_path)
    if content is not None:
        if verbose_callback:
            verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
        return content
    # raw download returned 404 -- could be wrong default branch.
    if ref in ("main", "master"):
        fallback_ref = "master" if ref == "main" else "main"
        content = delegate.try_raw_download(owner, repo, fallback_ref, file_path)
        if content is not None:
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return content
    return None


def _gh_generic_raw_attempt(
    delegate, host, owner, repo, ref, file_path, dep_ref, file_ctx, verbose_callback
) -> bytes | None:
    """Try the raw URL path on a generic (non-GitHub) host before the Contents API."""
    raw_url = f"https://{host}/{owner}/{repo}/raw/{ref}/{file_path}"
    raw_headers = delegate._build_generic_host_auth_headers(host, file_ctx, accept=None)
    if verbose_callback:
        verbose_callback(f"Trying raw URL on generic host {host}: {raw_url}")
    try:
        response = delegate._host._resilient_get(raw_url, headers=raw_headers, timeout=30)
        if response.status_code == 200:
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return response.content
    except (requests.RequestException, OSError) as raw_err:
        if verbose_callback:
            verbose_callback(
                f"Raw URL on {host} failed for {file_path}@{ref}: "
                f"{type(raw_err).__name__}; falling back to Contents API."
            )
    return None


def _gh_contents_api(
    delegate,
    host,
    owner,
    repo,
    file_path,
    ref,
    dep_ref,
    token,
    file_ctx,
    is_github_host,
    verbose_callback,
) -> bytes:
    """Fetch a file via the GitHub/GHES Contents API, with 404/auth handling."""
    api_url_candidates = delegate._build_contents_api_urls(
        host, owner, repo, file_path, ref, is_github_host=is_github_host
    )
    api_url = api_url_candidates[0]

    # GitHub family: use GitHub raw-media accept header. Generic hosts
    # ignore it and may return JSON envelopes -- handle that on read.
    accept = "application/vnd.github.v3.raw" if is_github_host else "application/json"
    if is_github_host:
        headers: dict[str, str] = {"Accept": accept}
        if token:
            headers["Authorization"] = f"token {token}"
    else:
        headers = delegate._build_generic_host_auth_headers(host, file_ctx, accept=accept)

    try:
        if verbose_callback and not is_github_host:
            verbose_callback(f"Trying Contents API on {host}: {api_url}")
        response = delegate._host._resilient_get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        if verbose_callback:
            verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
        return delegate._extract_contents_api_payload(response, is_github_host)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return _gh_handle_404(
                delegate,
                host,
                owner,
                repo,
                file_path,
                ref,
                dep_ref,
                headers,
                api_url_candidates,
                is_github_host,
                verbose_callback,
            )
        if e.response.status_code in (401, 403):
            return _gh_handle_auth_error(
                delegate,
                e,
                host,
                owner,
                file_path,
                ref,
                dep_ref,
                api_url,
                token,
                is_github_host,
                verbose_callback,
            )
        raise RuntimeError(f"Failed to download {file_path}: HTTP {e.response.status_code}") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error downloading {file_path}: {e}")  # noqa: B904


def _gh_handle_404(
    delegate,
    host,
    owner,
    repo,
    file_path,
    ref,
    dep_ref,
    headers,
    api_url_candidates,
    is_github_host,
    verbose_callback,
) -> bytes:
    """Handle a Contents-API 404: try remaining candidates, then ref fallback."""
    # For generic hosts, try remaining API version candidates before ref fallback
    for candidate_url in api_url_candidates[1:]:
        try:
            if verbose_callback:
                verbose_callback(f"Contents API 404; trying next candidate: {candidate_url}")
            candidate_resp = delegate._host._resilient_get(
                candidate_url, headers=headers, timeout=30
            )
            candidate_resp.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return delegate._extract_contents_api_payload(candidate_resp, is_github_host)
        except requests.exceptions.HTTPError as ce:
            if ce.response.status_code != 404:
                raise RuntimeError(  # noqa: B904
                    f"Failed to download {file_path}: HTTP {ce.response.status_code}"
                )

    # Try fallback branches if the specified ref fails
    if ref not in ["main", "master"]:
        raise RuntimeError(
            delegate._build_unsupported_or_missing_error(
                host,
                dep_ref.repo_url,
                file_path,
                ref,
                api_url_candidates,
                is_github_host=is_github_host,
            )
        )

    # Try the other default branch
    fallback_ref = "master" if ref == "main" else "main"
    fallback_url_candidates = delegate._build_contents_api_urls(
        host, owner, repo, file_path, fallback_ref
    )

    for fallback_url in fallback_url_candidates:
        try:
            response = delegate._host._resilient_get(fallback_url, headers=headers, timeout=30)
            response.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return delegate._extract_contents_api_payload(response, is_github_host)
        except requests.exceptions.HTTPError as fe:
            if fe.response.status_code != 404:
                raise RuntimeError(  # noqa: B904
                    f"Failed to download {file_path}: HTTP {fe.response.status_code}"
                )

    raise RuntimeError(
        delegate._build_unsupported_or_missing_error(
            host,
            dep_ref.repo_url,
            file_path,
            ref,
            api_url_candidates,
            is_github_host=is_github_host,
            fallback_ref=fallback_ref,
        )
    )


def _gh_handle_auth_error(
    delegate,
    e,
    host,
    owner,
    file_path,
    ref,
    dep_ref,
    api_url,
    token,
    is_github_host,
    verbose_callback,
) -> bytes:
    """Handle a Contents-API 401/403: rate-limit vs auth, with unauth retry."""
    # Distinguish rate limiting from auth failure. X-RateLimit-* headers are
    # GitHub-specific; treat as rate-limit only when host is GitHub family.
    is_rate_limit = False
    if is_github_host:
        try:
            rl_remaining = e.response.headers.get("X-RateLimit-Remaining")
            if rl_remaining is not None and int(rl_remaining) == 0:
                is_rate_limit = True
        except (TypeError, ValueError):
            pass

    if is_rate_limit:
        raise RuntimeError(_gh_rate_limit_msg(delegate, host, owner, dep_ref, token)) from e

    # Retry without auth -- the repo might be public. GHES/GHE-DR don't
    # support unauthenticated org-scoped retries.
    if token and is_github_host and not host.lower().endswith(".ghe.com"):
        try:
            unauth_headers: dict[str, str] = {"Accept": "application/vnd.github.v3.raw"}
            response = delegate._host._resilient_get(api_url, headers=unauth_headers, timeout=30)
            response.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return delegate._extract_contents_api_payload(response, is_github_host)
        except requests.exceptions.HTTPError:
            pass  # Fall through to the original error

    raise RuntimeError(
        _gh_auth_failed_msg(delegate, host, owner, file_path, ref, dep_ref, token, is_github_host)
    )


def _gh_rate_limit_msg(delegate, host, owner, dep_ref, token) -> str:
    """Build the rate-limit error message for a GitHub 403."""
    error_msg = f"GitHub API rate limit exceeded for {dep_ref.repo_url}. "
    if not token:
        error_msg += (
            "Unauthenticated requests are limited to 60/hour (shared per IP). "
            + delegate._host.auth_resolver.build_error_context(
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
            "Wait a few minutes or check your token's rate-limit quota."
        )
    return error_msg


def _gh_auth_failed_msg(
    delegate, host, owner, file_path, ref, dep_ref, token, is_github_host
) -> str:
    """Build the auth-failure error message for a GitHub 401/403."""
    error_msg = f"Authentication failed for {dep_ref.repo_url} (file: {file_path}, ref: {ref}). "
    if not token:
        error_msg += delegate._host.auth_resolver.build_error_context(
            host,
            "download",
            org=owner,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
    elif is_github_host and not host.lower().endswith(".ghe.com"):
        error_msg += (
            "Both authenticated and unauthenticated access were attempted. "
            "The repository may be private, or your token may lack SSO/SAML "
            "authorization for this organization."
        )
    elif is_github_host:
        error_msg += "Please check your GitHub token permissions."
    else:
        # Generic host: don't claim SSO/SAML or "GitHub token".
        error_msg += (
            f"Host {host} rejected the request. "
            "Verify the repository exists and that the token has access. "
            "Tokens are sourced from your git credential helper, a per-org "
            f"GITHUB_APM_PAT_<ORG> env var, or GITHUB_HOST={host} when this "
            "host is your GitHub Enterprise Server."
        )
    return error_msg
