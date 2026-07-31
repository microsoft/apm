"""Backend-specific download delegates for APM packages.

Encapsulates HTTP resilient-get, GitHub API file download, Azure DevOps
file download, and Artifactory archive download logic.  The owning
:class:`~apm_cli.deps.github_downloader.GitHubPackageDownloader` creates
a single :class:`DownloadDelegate` instance and delegates download
operations to it (Facade/Delegate pattern).
"""

import base64
import json
import os
import random
import sys
import threading
import time
import weakref
from pathlib import Path
from urllib.parse import quote as quote

import requests

from ..core.auth import AuthResolver as AuthResolver
from ..core.auth import HostInfo
from ..models.apm_package import DependencyReference
from ..utils.github_host import (
    build_ado_api_url as build_ado_api_url,
)
from ..utils.github_host import (
    build_artifactory_archive_url as build_artifactory_archive_url,
)
from ..utils.github_host import (
    build_https_clone_url,
    build_raw_content_url,
    build_ssh_url,
    default_host,
    is_github_hostname,
)

# Re-exported (double-name so ruff keeps them): the GitLab git-first body moved
# to download_strategies_backends_ops for the Stage-2 800-line ceiling and
# reaches these through the ``_ds`` facade alias, which also preserves the
# ``apm_cli.deps.download_strategies.<name>`` monkeypatch seam.
from ..utils.path_security import PathTraversalError as PathTraversalError
from .git_file_transport import (
    GitFileFetchResult,
    GitSparseFileTransport,
)
from .git_file_transport import GitFileTransportError as GitFileTransportError
from .git_file_transport import (
    GitFileTransportSecurityError as GitFileTransportSecurityError,
)
from .github_rate_limit import GitHubThrottleError, github_throttle_error
from .host_backends import backend_for


def _debug(message: str) -> None:
    """Print debug message if APM_DEBUG environment variable is set."""
    if os.environ.get("APM_DEBUG"):
        print(f"[DEBUG] {message}", file=sys.stderr)


def _close_response(response: requests.Response, context: str) -> None:
    """Close an HTTP response and preserve failures in debug diagnostics."""
    try:
        response.close()
    except Exception as exc:
        _debug(f"{context} response close failed: {exc}")


def _close_git_file_transports(transports: dict[object, object]) -> None:
    """Close cached git file transports owned by a DownloadDelegate."""
    for transport in list(transports.values()):
        close = getattr(transport, "close", None)
        if close is not None:
            try:
                close()
            except Exception as exc:
                _debug(f"git file transport cleanup failed: {exc}")
    transports.clear()


# ---------------------------------------------------------------------------
# DownloadDelegate
# ---------------------------------------------------------------------------


class DownloadDelegate:
    """Facade/Delegate that encapsulates backend-specific download logic.

    Holds the real implementations of HTTP resilient-get, URL building,
    and file download methods for GitHub, Azure DevOps, and Artifactory
    backends.

    A back-reference to the owning ``GitHubPackageDownloader`` (*host*)
    is kept as a known trade-off: it creates a circular reference
    between the delegate and its owner, but avoids duplicating shared
    state (``auth_resolver``, tokens, ``registry_config``) and
    preserves existing test ``patch.object`` points on the orchestrator.
    """

    def __init__(self, host, git_file_transport_factory=None):
        """Initialize with a reference to the owning downloader.

        Args:
            host: The :class:`GitHubPackageDownloader` instance that owns
                this delegate.
        """
        self._host = host
        self._git_file_transports: dict[
            tuple[str, str, str, int | None], GitSparseFileTransport
        ] = {}
        self._git_file_transports_lock = threading.Lock()
        self._git_file_transport_factory = git_file_transport_factory
        self._git_file_transport_finalizer = weakref.finalize(
            self,
            _close_git_file_transports,
            self._git_file_transports,
        )

    # ------------------------------------------------------------------
    # HTTP resilient GET
    # ------------------------------------------------------------------

    def resilient_get(
        self,
        url: str,
        headers: dict[str, str],
        timeout: int = 30,
        max_retries: int = 3,
        *,
        stream: bool = False,
        retry_throttles: bool = True,
    ) -> requests.Response:
        """HTTP GET with selectable retries for transient HTTP failures.

        Args:
            url: Request URL
            headers: HTTP headers
            timeout: Request timeout in seconds
            max_retries: Maximum total attempts, including the initial request
            stream: Whether to stream the response body instead of buffering it
            retry_throttles: Whether general callers retry a classified
                throttle. Virtual-file download callers disable this so they
                can select sparse Git without a retry or sleep.

        Returns:
            requests.Response (caller should call .raise_for_status() as needed)

        Raises:
            requests.exceptions.RequestException: After all retries exhausted
        """
        last_exc = None
        last_response = None
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, timeout=timeout, stream=stream)

                throttle = github_throttle_error(response, "GitHub API")
                if throttle is not None:
                    if not retry_throttles:
                        return response
                    last_response = response
                    if attempt < max_retries - 1:
                        wait = throttle.throttle.retry_after_seconds
                        if wait is None:
                            wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                        else:
                            wait = min(wait, 60)
                        _debug(
                            f"Rate limited ({response.status_code}), retry in "
                            f"{wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                        )
                        _close_response(response, "rate-limit retry")
                        time.sleep(wait)
                    continue

                if response.status_code == 503:
                    last_response = response
                    if attempt < max_retries - 1:
                        wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                        _debug(
                            f"Service unavailable, retry in {wait:.1f}s "
                            f"(attempt {attempt + 1}/{max_retries})"
                        )
                        _close_response(response, "service-unavailable retry")
                        time.sleep(wait)
                    continue

                return response
            except requests.exceptions.ConnectionError as e:
                last_exc = e
                if attempt < max_retries - 1:
                    wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                    _debug(
                        f"Connection error, retry in {wait:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait)
            except requests.exceptions.Timeout as e:
                last_exc = e
                if attempt < max_retries - 1:
                    _debug(f"Timeout, retrying (attempt {attempt + 1}/{max_retries})")

        # A retryable service failure returns its final response for the caller
        # to inspect and render.
        if last_response is not None:
            return last_response

        if last_exc:
            raise last_exc
        raise requests.exceptions.RequestException(f"All {max_retries} attempts failed for {url}")

    # ------------------------------------------------------------------
    # Repository URL building
    # ------------------------------------------------------------------

    def build_repo_url(
        self,
        repo_ref: str,
        use_ssh: bool = False,
        dep_ref: DependencyReference = None,
        token: str | None = None,
        auth_scheme: str = "basic",
    ) -> str:
        """Build the appropriate repository URL for cloning.

        Supports both GitHub and Azure DevOps URL formats:
        - GitHub: https://github.com/owner/repo.git
        - ADO: https://dev.azure.com/org/project/_git/repo

        Args:
            repo_ref: Repository reference in format "owner/repo" or
                "org/project/repo" for ADO
            use_ssh: Whether to use SSH URL for git operations
            dep_ref: Optional DependencyReference for ADO-specific URL building
            token: Optional per-dependency token override
            auth_scheme: Auth scheme ("basic" or "bearer"). Bearer tokens are
                injected via env vars, NOT embedded in the URL.

        Returns:
            str: Repository URL suitable for git clone operations
        """
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

        # Resolve the effective token. ``token == ""`` is the explicit
        # "suppress per-instance default" signal used by the
        # TransportSelector for plain-HTTPS / SSH attempts.
        if token == "":
            effective_token: str | None = ""
        elif token is not None:
            effective_token = token
        elif is_ado:
            effective_token = self._host.ado_token
        elif backend.is_github_family:
            effective_token = self._host.github_token
        elif backend.kind == "gitlab" and dep_ref is not None:
            # GitLab tokens come from GITLAB_APM_PAT / GITLAB_TOKEN /
            # credential helpers via the per-dep AuthResolver lookup.
            effective_token = self._host.auth_resolver.resolve_for_dep(dep_ref).token
        else:
            # Generic hosts: backend never embeds tokens; pick None so the
            # branch below produces the expected "no credential in URL" form.
            effective_token = None

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
            port = None
            if use_ssh:
                return build_ssh_url(host, repo_ref, port=port)
            if is_insecure:
                return f"http://{host}/{repo_ref}.git"
            if backend.is_github_family and effective_token:
                return build_https_clone_url(host, repo_ref, token=effective_token, port=port)
            return build_https_clone_url(host, repo_ref, token=None, port=port)

        if use_ssh:
            return backend.build_clone_ssh_url(dep_ref)
        if is_insecure:
            return backend.build_clone_http_url(dep_ref)
        return backend.build_clone_https_url(
            dep_ref, token=effective_token, auth_scheme=auth_scheme
        )

    # ------------------------------------------------------------------
    # Artifactory helpers
    # ------------------------------------------------------------------

    def get_artifactory_headers(self) -> dict[str, str]:
        """Build HTTP headers for registry/Artifactory requests."""
        cfg = self._host.registry_config
        if cfg is not None:
            return cfg.get_headers()
        # Fallback: direct artifactory_token attribute (legacy path)
        headers: dict[str, str] = {}
        if self._host.artifactory_token:
            headers["Authorization"] = f"Bearer {self._host.artifactory_token}"
        return headers

    def download_artifactory_archive(
        self,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        ref: str,
        target_path: Path,
        scheme: str = "https",
    ) -> None:
        """Download and extract a zip archive from Artifactory VCS proxy."""
        from .download_strategies_backends_ops import download_artifactory_archive as _impl

        return _impl(self, host, prefix, owner, repo, ref, target_path, scheme)

    def download_file_from_artifactory(
        self,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
        scheme: str = "https",
    ) -> bytes:
        """Download a single file from Artifactory (entry API, then archive)."""
        from .download_strategies_backends_ops import download_file_from_artifactory as _impl

        return _impl(self, host, prefix, owner, repo, file_path, ref, scheme)

    # ------------------------------------------------------------------
    # Raw / CDN download helper
    # ------------------------------------------------------------------

    def try_raw_download(self, owner: str, repo: str, ref: str, file_path: str) -> bytes | None:
        """Attempt to fetch a file via raw.githubusercontent.com (CDN).

        This pre-auth helper must remain token-free: it runs before
        ``AuthResolver`` has established that credentials may be required.

        Returns the raw bytes on success, or ``None`` if the file was not found
        (HTTP 404) or the request failed for any reason.  This is intentionally
        best-effort: callers fall back to the Contents API when ``None`` is
        returned.
        """
        raw_url = build_raw_content_url(owner, repo, ref, file_path)
        try:
            response = requests.get(raw_url, timeout=30)
            if response.status_code == 200:
                return response.content
        except requests.exceptions.RequestException:
            pass
        return None

    # ------------------------------------------------------------------
    # Azure DevOps file download
    # ------------------------------------------------------------------

    def download_ado_file(
        self,
        dep_ref: DependencyReference,
        file_path: str,
        ref: str = "main",
    ) -> bytes:
        """Download a file from an Azure DevOps repository."""
        from .download_strategies_backends_ops import download_ado_file as _impl

        return _impl(self, dep_ref, file_path, ref)

    def _git_file_transport_key(
        self, dep_ref: DependencyReference, ref: str
    ) -> tuple[str, str, str, int | None]:
        """Return the cache key for one path-scoped Git file checkout."""
        return (dep_ref.host or default_host(), dep_ref.repo_url, ref, dep_ref.port)

    def _discard_git_file_transport(self, key: tuple[str, str, str, int | None]) -> None:
        """Close and remove a failed cached git-file checkout."""
        with self._git_file_transports_lock:
            transport = self._git_file_transports.pop(key, None)
        if transport is not None:
            transport.close()

    def _download_gitlab_file_via_git(
        self,
        dep_ref: DependencyReference,
        file_path: str,
        ref: str,
    ) -> bytes:
        """Fetch a GitLab path: file via a reusable sparse checkout."""
        from .download_strategies_ops import _download_gitlab_file_via_git_impl

        return _download_gitlab_file_via_git_impl(self, dep_ref, file_path, ref)

    def _download_github_file_via_git(
        self,
        dep_ref: DependencyReference,
        file_path: str,
        ref: str,
    ) -> GitFileFetchResult:
        """Fetch a throttled GitHub virtual file through one sparse Git transport."""
        from .download_strategies_ops import _download_github_file_via_git_impl

        return _download_github_file_via_git_impl(self, dep_ref, file_path, ref)

    def download_github_file_via_throttle_fallback(
        self,
        dep_ref: DependencyReference,
        file_path: str,
        ref: str,
        throttle: GitHubThrottleError,
    ) -> GitFileFetchResult:
        """Select the sole sparse-Git fallback for a confirmed GitHub throttle."""
        from .download_strategies_ops import download_github_file_via_throttle_fallback_impl

        return download_github_file_via_throttle_fallback_impl(
            self, dep_ref, file_path, ref, throttle
        )

    # ------------------------------------------------------------------
    # GitLab file download
    # ------------------------------------------------------------------

    def download_gitlab_file(
        self,
        dep_ref: DependencyReference,
        file_path: str,
        ref: str = "main",
        verbose_callback=None,
    ) -> bytes:
        """Download a GitLab file: git-transport-first, REST API as fallback.

        Primary path (the 410-killer): extracts the file via git sparse/
        partial checkout (blob:none + file-level sparse paths) so SSH keys and
        system git credentials are sufficient -- no REST API token needed.

        Fallback (thin GITLAB_PAT path): if the git transport fails (e.g.
        SSH not available, network restriction), the existing GitLab REST v4
        ``repository/files/.../raw`` endpoint is tried with the GITLAB_APM_PAT
        / GITLAB_TOKEN credential, mirroring the ADO_APM_PAT pattern.
        """
        from .download_strategies_backends_ops import download_gitlab_file_git_first_impl

        return download_gitlab_file_git_first_impl(self, dep_ref, file_path, ref, verbose_callback)

    def _download_public_github_file_anonymous_first(
        self,
        dep_ref: DependencyReference,
        file_path: str,
        ref: str,
        verbose_callback=None,
    ) -> bytes:
        """Fetch one github.com file anonymously, resolving auth only on 4xx."""
        from .download_strategies_ops import _download_public_github_file_anonymous_first_impl

        return _download_public_github_file_anonymous_first_impl(
            self, dep_ref, file_path, ref, verbose_callback
        )

    def download_github_file(
        self,
        dep_ref: DependencyReference,
        file_path: str,
        ref: str = "main",
        verbose_callback=None,
    ) -> bytes:
        """Download a file from a GitHub repository (CDN fast-path then API)."""
        from .download_strategies_ops import download_github_file as _impl

        return _impl(self, dep_ref, file_path, ref, verbose_callback)

    # ------------------------------------------------------------------
    # Helpers for download_github_file
    # ------------------------------------------------------------------

    @staticmethod
    def _is_configured_ghes(host: str) -> bool:
        """Return True when *host* matches the user's declared GHES via GITHUB_HOST.

        ``GITHUB_HOST=<custom-domain>`` is the documented opt-in for treating
        a non-``*.ghe.com`` FQDN as GitHub-family. Centralised so the routing
        check, header builder, and Contents-API URL builder cannot drift.
        """
        configured = os.environ.get("GITHUB_HOST", "").strip().lower()
        if not configured:
            return False
        return (host or "").lower() == configured

    @staticmethod
    def _build_contents_api_urls(
        host: str,
        owner: str,
        repo: str,
        file_path: str,
        ref: str,
        *,
        is_github_host: bool | None = None,
    ) -> list[str]:
        """Return the ordered list of Contents-API URL candidates for *host*.

        Thin wrapper around the per-host backends -- the actual URL shape
        lives on the backend. Kept as a static method on
        :class:`DownloadDelegate` for back-compat with existing callers
        and tests that monkey-patch it.
        """
        from .host_backends import GenericGitBackend, GHECloudBackend, GHESBackend, GitHubBackend

        if is_github_host is None:
            is_github_host = is_github_hostname(host) or DownloadDelegate._is_configured_ghes(host)

        host_lower = (host or "").lower()
        if not is_github_host:
            backend = GenericGitBackend(
                host_info=HostInfo(
                    host=host,
                    kind="generic",
                    has_public_repos=False,
                    api_base=f"https://{host}",
                )
            )
        elif host_lower == "github.com":
            backend = GitHubBackend(
                host_info=HostInfo(
                    host=host,
                    kind="github",
                    has_public_repos=True,
                    api_base="https://api.github.com",
                )
            )
        elif host_lower.endswith(".ghe.com"):
            backend = GHECloudBackend(
                host_info=HostInfo(
                    host=host,
                    kind="ghe_cloud",
                    has_public_repos=False,
                    api_base=f"https://{host}/api/v3",
                )
            )
        else:
            # Configured GHES (GITHUB_HOST=<custom-host>): api_base is
            # ``https://{host}/api/v3``, not ``https://api.{host}``.
            backend = GHESBackend(
                host_info=HostInfo(
                    host=host,
                    kind="ghes",
                    has_public_repos=False,
                    api_base=f"https://{host}/api/v3",
                )
            )
        return backend.build_contents_api_urls(owner, repo, file_path, ref)

    @staticmethod
    def _build_generic_host_auth_headers(
        host: str, auth_ctx, *, accept: str | None = None
    ) -> dict[str, str]:
        """Build HTTP headers for a generic-host (non-GitHub) request.

        SECURITY GUARD: HTTP single-file downloads use the same token
        boundary as clone URLs. Generic hosts receive no APM-managed
        Authorization header; they must succeed unauthenticated or use a
        host-specific backend such as ``type: gitlab``.

        Forwarding is allowed only when the caller passes a non-generic
        auth context whose token is unambiguously intended for this host.
        """
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        if auth_ctx is None or not getattr(auth_ctx, "token", None):
            return headers
        source = getattr(auth_ctx, "source", None) or ""
        host_scoped = source == "git-credential-fill"
        org_scoped = source.startswith("GITHUB_APM_PAT_")
        configured_ghes = DownloadDelegate._is_configured_ghes(host)
        if host_scoped or org_scoped or configured_ghes:
            headers["Authorization"] = f"token {auth_ctx.token}"
        return headers

    @staticmethod
    def _extract_contents_api_payload(response, is_github_host: bool) -> bytes:
        """Decode a Contents-API response into raw file bytes.

        - GitHub family: ``Accept: application/vnd.github.v3.raw`` returns
          the file bytes directly; pass through ``response.content``.
        - Generic hosts (Gitea, Gogs): the raw-media accept header is
          ignored and the server returns a JSON envelope of the form::

              {"content": "<base64>", "encoding": "base64", ...}

          Decode ``content`` as base64 and return the resulting bytes.
          Some Gitea installations also emit ``encoding: ""`` with raw
          content -- pass that through unchanged. If the response is not
          a JSON envelope at all (custom proxy, raw bytes), fall back to
          ``response.content``.
        """
        if is_github_host:
            return response.content

        body = response.content
        try:
            ctype = str((response.headers or {}).get("Content-Type") or "").lower()
        except (AttributeError, TypeError):
            ctype = ""
        if "json" not in ctype and not (
            isinstance(body, (bytes, bytearray)) and body.lstrip().startswith(b"{")
        ):
            return body
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, AttributeError, RecursionError, MemoryError):
            return body
        if not isinstance(payload, dict) or "content" not in payload:
            return body
        encoding = (payload.get("encoding") or "").lower()
        content_field = payload.get("content") or ""
        if encoding == "base64":
            try:
                return base64.b64decode(content_field, validate=False)
            except (ValueError, TypeError):
                return body
        # Non-base64 envelope (rare): return literal content if it's a string,
        # otherwise fall back to the raw body.
        if isinstance(content_field, str):
            return content_field.encode("utf-8")
        return body

    @staticmethod
    def _build_download_http_error(
        host: str,
        file_path: str,
        status: int | str,
        endpoint: str,
    ) -> str:
        """Build a host- and endpoint-specific HTTP download error."""
        return (
            f"Failed to download {file_path} from {host}: HTTP {status} "
            f"from {endpoint} endpoint. Re-run with --verbose to see attempted URLs."
        )

    @staticmethod
    def _build_download_network_error(
        host: str,
        file_path: str,
        endpoint: str,
        error: BaseException,
    ) -> str:
        """Build a host- and endpoint-specific network download error."""
        detail = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
        return (
            f"Network error downloading {file_path} from {host} via {endpoint} endpoint: "
            f"{detail}. Re-run with --verbose to see attempted URLs."
        )

    @staticmethod
    def _build_unsupported_or_missing_error(
        host: str,
        repo_url: str,
        file_path: str,
        ref: str,
        api_url_candidates: list[str],
        *,
        is_github_host: bool,
        fallback_ref: str | None = None,
    ) -> str:
        """Build a discoverable error when no Contents-API candidate hits 200."""
        ref_part = f"(tried refs: {ref}, {fallback_ref})" if fallback_ref else f"at ref '{ref}'"
        if is_github_host:
            return f"File not found: {file_path} in {repo_url} {ref_part}"
        # Non-GitHub host: name what was tried so users can diagnose
        # GitLab / unsupported-host cases without re-reading source.
        tried = ", ".join(["raw"] + [u.split("/api/")[1].split("/")[0] for u in api_url_candidates])
        canonical_url = f"https://{host}/{repo_url}/raw/{ref}/{file_path}"
        return (
            f"File not found on generic host {host}: {canonical_url} {ref_part}. "
            f"Tried URL families: {tried}. "
            "If this is GitLab, virtual subdirectory packages are not "
            "supported (use the dict-form full repo URL instead)."
        )
