"""Backend-specific download delegates for APM packages.

Encapsulates HTTP resilient-get, GitHub API file download, Azure DevOps
file download, and Artifactory archive download logic.  The owning
:class:`~apm_cli.deps.github_downloader.GitHubPackageDownloader` creates
a single :class:`DownloadDelegate` instance and delegates download
operations to it (Facade/Delegate pattern).
"""

import random
import time
from dataclasses import dataclass

import requests

from ...models.apm_package import DependencyReference
from ...utils.github_host import (
    build_https_clone_url,
    build_ssh_url,
    default_host,
)
from ..host_backends import backend_for
from .class_ import _debug


def _check_rate_limited_by_403(response: requests.Response) -> bool:
    """Return True if a 403 response signals primary-rate-limit exhaustion."""
    if response.status_code != 403:
        return False
    try:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) == 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _calc_rate_limit_wait(response: requests.Response, attempt: int) -> float:
    """Compute how long to wait (seconds) when rate-limited."""
    retry_after = response.headers.get("Retry-After")
    reset_at = response.headers.get("X-RateLimit-Reset")
    if retry_after:
        try:
            return min(float(retry_after), 60)
        except (TypeError, ValueError):
            pass
    elif reset_at:
        try:
            return max(0, min(int(reset_at) - time.time(), 60))
        except (TypeError, ValueError):
            pass
    return min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311


def _log_rate_limit_proximity(response: requests.Response) -> None:
    """Log a debug warning when GitHub API quota is nearly exhausted."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    try:
        if remaining and int(remaining) < 10:
            _debug(f"GitHub API rate limit low: {remaining} requests remaining")
    except (TypeError, ValueError):
        pass


def resilient_get(
    self,
    url: str,
    headers: dict[str, str],
    timeout: int = 30,
    max_retries: int = 3,
) -> requests.Response:
    """HTTP GET with retry on 429/503 and rate-limit header awareness.

    Args:
        url: Request URL
        headers: HTTP headers
        timeout: Request timeout in seconds
        max_retries: Maximum retry attempts for transient failures

    Returns:
        requests.Response (caller should call .raise_for_status() as needed)

    Raises:
        requests.exceptions.RequestException: After all retries exhausted
    """
    last_exc = None
    last_response = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)

            is_rate_limited = response.status_code in (429, 503)
            if not is_rate_limited:
                is_rate_limited = _check_rate_limited_by_403(response)

            if is_rate_limited:
                last_response = response
                wait = _calc_rate_limit_wait(response, attempt)
                _debug(
                    f"Rate limited ({response.status_code}), retry in "
                    f"{wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait)
                continue

            _log_rate_limit_proximity(response)
            return response
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                _debug(
                    f"Connection error, retry in {wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(wait)
        except requests.exceptions.Timeout as e:
            last_exc = e
            if attempt < max_retries - 1:
                _debug(f"Timeout, retrying (attempt {attempt + 1}/{max_retries})")

    if last_response is not None:
        return last_response

    if last_exc:
        raise last_exc
    raise requests.exceptions.RequestException(f"All {max_retries} attempts failed for {url}")


@dataclass(frozen=True, slots=True)
class _UrlOpts:
    use_ssh: bool
    is_insecure: bool
    is_github_family: bool
    effective_token: str | None = None


def _build_no_dep_ref_url(host: str, repo_ref: str, opts: _UrlOpts) -> str:
    """Build clone URL for legacy callers that provide no dep_ref.

    Constructs the URL directly from *host* and *repo_ref*, preserving
    the behaviour of the original if/elif ladder.
    """
    port = None
    if opts.use_ssh:
        return build_ssh_url(host, repo_ref, port=port)
    if opts.is_insecure:
        return f"http://{host}/{repo_ref}.git"
    if opts.is_github_family and opts.effective_token:
        return build_https_clone_url(host, repo_ref, token=opts.effective_token, port=port)
    return build_https_clone_url(host, repo_ref, token=None, port=port)


@dataclass(frozen=True, slots=True)
class _CloneConf:
    use_ssh: bool = False
    auth_scheme: str = "basic"


def _resolve_effective_token(host_intf, token, backend, dep_ref) -> str | None:
    """Resolve the effective clone token from the supplied overrides and backend config."""
    if token == "":
        return ""
    if token is not None:
        return token
    if backend.kind == "ado":
        return host_intf.ado_token
    if backend.is_github_family:
        return host_intf.github_token
    if backend.kind == "gitlab" and dep_ref is not None:
        return host_intf.auth_resolver.resolve_for_dep(dep_ref).token
    return None


def build_repo_url(
    self,
    repo_ref: str,
    dep_ref: DependencyReference | None = None,
    token: str | None = None,
    conf: _CloneConf | None = None,
) -> str:
    """Build the appropriate repository URL for cloning.

    Supports both GitHub and Azure DevOps URL formats:
    - GitHub: https://github.com/owner/repo.git
    - ADO: https://dev.azure.com/org/project/_git/repo

    Args:
        repo_ref: Repository reference in format "owner/repo" or
            "org/project/repo" for ADO
        dep_ref: Optional DependencyReference for ADO-specific URL building
        token: Optional per-dependency token override
        conf: Optional clone configuration (use_ssh, auth_scheme)

    Returns:
        str: Repository URL suitable for git clone operations
    """
    use_ssh = conf.use_ssh if conf is not None else False
    auth_scheme = conf.auth_scheme if conf is not None else "basic"

    # Resolve host (used for token-routing and as a fallback when
    # ``dep_ref`` is missing for legacy callers).
    if dep_ref and dep_ref.host:
        host = dep_ref.host
    else:
        host = getattr(self._host, "github_host", None) or default_host()

    # Pick the vendor-specific backend via ``classify_host`` -- this
    # replaces the in-line ``if is_ado / elif is_github / else`` ladder
    # with a single dispatch.
    backend = backend_for(
        dep_ref,
        self._host.auth_resolver,
        fallback_host=host,
    )

    is_ado = backend.kind == "ado"
    is_insecure = bool(getattr(dep_ref, "is_insecure", False)) if dep_ref is not None else False

    effective_token = _resolve_effective_token(self._host, token, backend, dep_ref)

    _debug(
        f"build_repo_url: host={host}, kind={backend.kind}, "
        f"dep_ref={'present' if dep_ref else 'None'}, "
        f"ado_org={dep_ref.ado_organization if dep_ref else None}"
    )

    # ADO without a parsed ``ado_organization`` cannot use the ADO
    # builders (they need org/project/repo). Fall through to the
    # generic GitHub-style URL the way the previous ladder did.
    if is_ado and not (dep_ref and dep_ref.ado_organization):
        backend = backend_for(
            None,
            self._host.auth_resolver,
            fallback_host=host,
        )

    if dep_ref is None:
        # Legacy no-dep_ref callers: preserve historical behaviour.
        # Build URL directly from ``repo_ref`` + ``host`` since the
        # backends require a dep_ref to read host/port/etc.
        return _build_no_dep_ref_url(
            host,
            repo_ref,
            _UrlOpts(
                use_ssh=use_ssh,
                is_insecure=is_insecure,
                is_github_family=backend.is_github_family,
                effective_token=effective_token,
            ),
        )

    if use_ssh:
        return backend.build_clone_ssh_url(dep_ref)
    if is_insecure:
        return backend.build_clone_http_url(dep_ref)
    return backend.build_clone_https_url(dep_ref, token=effective_token, auth_scheme=auth_scheme)
