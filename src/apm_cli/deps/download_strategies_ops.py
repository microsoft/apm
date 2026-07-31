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


def _download_public_github_file_anonymous_first_impl(
    delegate,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str,
    verbose_callback=None,
) -> bytes:
    """Fetch one github.com file anonymously, resolving auth only on 4xx."""
    from apm_cli.deps import download_strategies as _ds

    host = dep_ref.host or _ds.default_host()
    owner, repo = dep_ref.repo_url.split("/", 1)
    refs = [ref]
    if ref in ("main", "master"):
        refs.append("master" if ref == "main" else "main")

    for candidate_ref in refs:
        content = delegate.try_raw_download(owner, repo, candidate_ref, file_path)
        if content is not None:
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return content

    def _contents_request(
        token: str | None,
        _git_env: dict[str, str],
    ) -> bytes:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github.v3.raw",
        }
        if token:
            headers["Authorization"] = f"token {token}"
        last_response = None
        for candidate_ref in refs:
            api_url = delegate._build_contents_api_urls(
                host,
                owner,
                repo,
                file_path,
                candidate_ref,
                is_github_host=True,
            )[0]
            response = delegate._host._resilient_get(
                api_url,
                headers=headers,
                timeout=30,
                retry_throttles=False,
            )
            if response.status_code == 200:
                if verbose_callback:
                    verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
                return delegate._extract_contents_api_payload(
                    response,
                    is_github_host=True,
                )
            throttle = _ds.github_throttle_error(response, host)
            if throttle is not None:
                raise throttle
            last_response = response
            if response.status_code != 404:
                response.raise_for_status()
        if last_response is not None:
            last_response.raise_for_status()
        raise RuntimeError(f"No GitHub Contents API response for {file_path}")

    try:
        return delegate._host.auth_resolver.try_with_fallback(
            host,
            _contents_request,
            org=owner,
            port=dep_ref.port,
            path=dep_ref.repo_url,
            host_type=dep_ref.host_type,
            unauth_first=True,
            verbose_callback=verbose_callback,
        )
    except _ds.GitHubThrottleError:
        raise
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status in (401, 403):
            raise RuntimeError(
                f"Authentication failed for {dep_ref.repo_url} "
                f"(file: {file_path}, ref: {ref}). "
                "The repository may be private or the resolved credential "
                "may lack access."
            ) from exc
        if status == 404:
            raise RuntimeError(
                delegate._build_unsupported_or_missing_error(
                    host,
                    dep_ref.repo_url,
                    file_path,
                    ref,
                    delegate._build_contents_api_urls(
                        host,
                        owner,
                        repo,
                        file_path,
                        ref,
                        is_github_host=True,
                    ),
                    is_github_host=True,
                    fallback_ref=refs[-1] if len(refs) > 1 else None,
                )
            ) from exc
        raise RuntimeError(
            delegate._build_download_http_error(
                host,
                file_path,
                status,
                "Contents API",
            )
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(
            delegate._build_download_network_error(
                host,
                file_path,
                "Contents API",
                exc,
            )
        ) from exc


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

    if (
        delegate._host.auth_resolver.uses_public_github_anonymous_first(
            host,
            port=dep_ref.port,
            host_type=dep_ref.host_type,
        )
        is True
    ):
        return delegate._download_public_github_file_anonymous_first(
            dep_ref,
            file_path,
            ref,
            verbose_callback,
        )

    # Parse owner/repo from repo_url
    owner, repo = dep_ref.repo_url.split("/", 1)

    # Resolve auth once through the same per-dependency boundary used by
    # clone URLs. Generic hosts intentionally return None here so APM
    # does not attach managed PATs to ad-hoc HTTP requests.
    file_ctx = delegate._host._resolve_dep_auth_ctx(dep_ref)
    token = file_ctx.token if file_ctx else None

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
    except (requests.RequestException, OSError) as raw_err:
        raise RuntimeError(
            delegate._build_download_network_error(host, file_path, "raw URL", raw_err)
        ) from raw_err
    if response.status_code == 200:
        if verbose_callback:
            verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
        return response.content
    if response.status_code != 404:
        raise RuntimeError(
            delegate._build_download_http_error(host, file_path, response.status_code, "raw URL")
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
        response = delegate._host._resilient_get(
            api_url, headers=headers, timeout=30, retry_throttles=not is_github_host
        )
        response.raise_for_status()
        if verbose_callback:
            verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
        return delegate._extract_contents_api_payload(response, is_github_host)
    except requests.exceptions.HTTPError as e:
        if is_github_host and e.response is not None:
            from apm_cli.deps.github_rate_limit import github_throttle_error as _gte

            _throttle = _gte(e.response, host)
            if _throttle is not None:
                raise _throttle from e
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
        raise RuntimeError(
            delegate._build_download_http_error(
                host, file_path, e.response.status_code, "Contents API"
            )
        ) from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            delegate._build_download_network_error(host, file_path, "Contents API", e)
        ) from e


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
            status = ce.response.status_code if ce.response is not None else "unknown"
            if status != 404:
                raise RuntimeError(  # noqa: B904
                    delegate._build_download_http_error(host, file_path, status, "Contents API")
                )
        except requests.exceptions.RequestException as ce:
            raise RuntimeError(  # noqa: B904
                delegate._build_download_network_error(host, file_path, "Contents API", ce)
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
            response = delegate._host._resilient_get(
                fallback_url, headers=headers, timeout=30, retry_throttles=not is_github_host
            )
            response.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return delegate._extract_contents_api_payload(response, is_github_host)
        except requests.exceptions.HTTPError as fe:
            if is_github_host and fe.response is not None:
                from apm_cli.deps.github_rate_limit import github_throttle_error as _gte

                _throttle = _gte(fe.response, host)
                if _throttle is not None:
                    raise _throttle from fe
            if fe.response.status_code != 404:
                raise RuntimeError(  # noqa: B904
                    delegate._build_download_http_error(
                        host, file_path, fe.response.status_code, "Contents API"
                    )
                )
        except requests.exceptions.RequestException as fe:
            raise RuntimeError(  # noqa: B904
                delegate._build_download_network_error(host, file_path, "Contents API", fe)
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


def _download_gitlab_file_via_git_impl(
    delegate,
    dep_ref: "DependencyReference",
    file_path: str,
    ref: str,
) -> bytes:
    """Implementation of GitLab path: file fetch via reusable sparse checkout."""
    import os

    from apm_cli.deps import download_strategies as _ds

    key = delegate._git_file_transport_key(dep_ref, ref)
    with delegate._git_file_transports_lock:
        transport = delegate._git_file_transports.get(key)
        if transport is None:
            git_env = {**os.environ, **(delegate._host.git_env or {})}
            transport_factory = delegate._git_file_transport_factory or _ds.GitSparseFileTransport
            transport = transport_factory(
                dep_ref,
                ref,
                build_repo_url_fn=delegate.build_repo_url,
                git_env=git_env,
            )
            delegate._git_file_transports[key] = transport
    try:
        return transport.fetch_file(file_path)
    except _ds.GitFileTransportError:
        delegate._discard_git_file_transport(key)
        raise


def _download_github_file_via_git_impl(
    delegate,
    dep_ref: "DependencyReference",
    file_path: str,
    ref: str,
):
    """Fetch a GitHub virtual file via sparse-Git transport (throttle fallback path)."""
    from apm_cli.deps import download_strategies as _ds
    from apm_cli.utils.github_host import set_authorization_header_git_env

    key = delegate._git_file_transport_key(dep_ref, ref)
    host = dep_ref.host or _ds.default_host()
    if (
        delegate._host.auth_resolver.uses_public_github_anonymous_first(
            host,
            port=dep_ref.port,
            host_type=dep_ref.host_type,
        )
        is True
    ):

        def _fetch(token: str | None, git_env: dict[str, str]):
            attempt_env = dict(git_env)
            if token:
                set_authorization_header_git_env(attempt_env, "Bearer", token)
                attempt_env.pop("GIT_TOKEN", None)

            def _anon_repo_url(repo_ref: str, *, dep_ref: "DependencyReference") -> str:
                return delegate.build_repo_url(
                    repo_ref,
                    dep_ref=dep_ref,
                    token="",
                    auth_scheme="basic",
                )

            transport_factory = delegate._git_file_transport_factory or _ds.GitSparseFileTransport
            transport = transport_factory(
                dep_ref,
                ref,
                build_repo_url_fn=_anon_repo_url,
                git_env=attempt_env,
            )
            try:
                return transport.fetch_file_with_commit(file_path)
            finally:
                transport.close()

        return delegate._host.auth_resolver.try_with_fallback(
            host,
            _fetch,
            org=dep_ref.repo_url.split("/", 1)[0],
            port=dep_ref.port,
            path=dep_ref.repo_url,
            host_type=dep_ref.host_type,
            unauth_first=True,
        )

    auth_ctx = delegate._host.auth_resolver.resolve_for_dep(dep_ref)
    if auth_ctx.token:
        # AuthResolver owns credential resolution. Convert its resolved
        # GitHub credential into Git's header channel so the token remains
        # out of the remote URL and is actually consumed by git.
        git_env = dict(auth_ctx.git_env)
        set_authorization_header_git_env(git_env, "Bearer", auth_ctx.token)
        git_env.pop("GIT_TOKEN", None)
        auth_scheme = "basic"
    else:
        # Public repositories use normal non-interactive Git. Do not
        # inherit an unrelated downloader token into this fallback.
        git_env = delegate._host._build_noninteractive_git_env()
        auth_scheme = "basic"

    def _tokenless_repo_url(repo_ref: str, *, dep_ref: "DependencyReference") -> str:
        return delegate.build_repo_url(
            repo_ref,
            dep_ref=dep_ref,
            token="",
            auth_scheme=auth_scheme,
        )

    with delegate._git_file_transports_lock:
        transport = delegate._git_file_transports.get(key)
        if transport is None:
            transport_factory = delegate._git_file_transport_factory or _ds.GitSparseFileTransport
            transport = transport_factory(
                dep_ref,
                ref,
                build_repo_url_fn=_tokenless_repo_url,
                git_env=git_env,
            )
            delegate._git_file_transports[key] = transport
    try:
        return transport.fetch_file_with_commit(file_path)
    except _ds.GitFileTransportError:
        delegate._discard_git_file_transport(key)
        raise


def download_github_file_via_throttle_fallback_impl(
    delegate,
    dep_ref: "DependencyReference",
    file_path: str,
    ref: str,
    throttle,
):
    """Sparse-Git fallback when a confirmed GitHub throttle fires."""
    from apm_cli.deps import download_strategies as _ds

    if dep_ref.is_insecure:
        raise RuntimeError(
            f"{throttle}; sparse Git fallback is unavailable for insecure HTTP dependencies"
        )
    try:
        return delegate._download_github_file_via_git(dep_ref, file_path, ref)
    except _ds.GitFileTransportError as exc:
        raise RuntimeError(
            f"{throttle}; sparse Git transport failed: {exc}. "
            "Retry after GitHub quota recovery; for private repositories, "
            "verify GITHUB_APM_PAT can read the repository."
        ) from exc


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
    from apm_cli.deps.github_rate_limit import github_throttle_error

    throttle = github_throttle_error(e.response, "GitHub API") if is_github_host else None
    if throttle is not None:
        raise throttle from e

    # Retry without auth -- the repo might be public. GHES/GHE-DR don't
    # support unauthenticated org-scoped retries.
    if token and is_github_host and not host.lower().endswith(".ghe.com"):
        try:
            unauth_headers: dict[str, str] = {"Accept": "application/vnd.github.v3.raw"}
            response = delegate._host._resilient_get(
                api_url, headers=unauth_headers, timeout=30, retry_throttles=False
            )
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
        if is_github_host:
            error_msg += delegate._host.auth_resolver.build_error_context(
                host,
                "download",
                org=owner,
                port=dep_ref.port if dep_ref else None,
                dep_url=dep_ref.repo_url if dep_ref else None,
            )
        else:
            error_msg += (
                "No APM-managed token was sent for generic host file download. "
                "Use a whole-repo git dependency for full clone auth support. "
                "For platform-specific HTTP file reads, use object-form type: gitlab "
                f"for GitLab-compatible hosts or set GITHUB_HOST={host} for GitHub "
                "Enterprise Server. Re-run with --verbose to see attempted URLs."
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
    if is_github_host:
        error_msg += " Re-run with --verbose to see attempted URLs."
    return error_msg
