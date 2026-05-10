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
import time
from pathlib import Path
from urllib.parse import quote

import requests

from ..core.auth import AuthResolver, HostInfo
from ..models.apm_package import DependencyReference
from ..utils.github_host import (
    build_ado_api_url,
    build_ado_https_clone_url,  # noqa: F401  -- re-exported for tests/back-compat
    build_ado_ssh_url,  # noqa: F401  -- re-exported for tests/back-compat
    build_artifactory_archive_url,
    build_https_clone_url,
    build_raw_content_url,
    build_ssh_url,
    default_host,
    is_github_hostname,
)
from .host_backends import backend_for

# ---------------------------------------------------------------------------
# Module-level debug helper (mirrors the one in github_downloader so that
# this module has no import dependency on the orchestrator).
# ---------------------------------------------------------------------------


def _debug(message: str) -> None:
    """Print debug message if APM_DEBUG environment variable is set."""
    if os.environ.get("APM_DEBUG"):
        print(f"[DEBUG] {message}", file=sys.stderr)


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

    def __init__(self, host):
        """Initialize with a reference to the owning downloader.

        Args:
            host: The :class:`GitHubPackageDownloader` instance that owns
                this delegate.
        """
        self._host = host

    # ------------------------------------------------------------------
    # HTTP resilient GET
    # ------------------------------------------------------------------

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

                # Handle rate limiting -- GitHub returns 429 for secondary limits
                # and 403 with X-RateLimit-Remaining: 0 for primary limits.
                is_rate_limited = response.status_code in (429, 503)
                if not is_rate_limited and response.status_code == 403:
                    try:
                        remaining = response.headers.get("X-RateLimit-Remaining")
                        if remaining is not None and int(remaining) == 0:
                            is_rate_limited = True
                    except (TypeError, ValueError):
                        pass

                if is_rate_limited:
                    last_response = response
                    retry_after = response.headers.get("Retry-After")
                    reset_at = response.headers.get("X-RateLimit-Reset")
                    if retry_after:
                        try:
                            wait = min(float(retry_after), 60)
                        except (TypeError, ValueError):
                            # Retry-After may be an HTTP-date; fall back to exponential backoff
                            wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                    elif reset_at:
                        try:
                            wait = max(0, min(int(reset_at) - time.time(), 60))
                        except (TypeError, ValueError):
                            wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                    else:
                        wait = min(2**attempt, 30) * (0.5 + random.random())  # noqa: S311
                    _debug(
                        f"Rate limited ({response.status_code}), retry in "
                        f"{wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait)
                    continue

                # Log rate limit proximity
                remaining = response.headers.get("X-RateLimit-Remaining")
                try:
                    if remaining and int(remaining) < 10:
                        _debug(f"GitHub API rate limit low: {remaining} requests remaining")
                except (TypeError, ValueError):
                    pass

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

        # If rate limiting exhausted all retries, return the last response so
        # callers can inspect headers (e.g. X-RateLimit-Remaining) and raise
        # an appropriate user-facing error.
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
        """Download and extract a zip archive from Artifactory VCS proxy.

        Tries multiple URL patterns (GitHub-style and GitLab-style).
        GitHub archives contain a single root directory named {repo}-{ref}/;
        this method strips that prefix on extraction so files land directly
        in *target_path*.

        Raises RuntimeError on failure.
        """
        import io
        import zipfile

        archive_urls = build_artifactory_archive_url(host, prefix, owner, repo, ref, scheme=scheme)
        headers = self.get_artifactory_headers()

        # Guard: reject unreasonably large archives (default 500 MB)
        max_archive_bytes = int(os.environ.get("ARTIFACTORY_MAX_ARCHIVE_MB", "500")) * 1024 * 1024

        last_error = None
        for url in archive_urls:
            _debug(f"Trying Artifactory archive: {url}")
            try:
                resp = self._host._resilient_get(url, headers=headers, timeout=60)
                if resp.status_code == 200:
                    if len(resp.content) > max_archive_bytes:
                        last_error = f"Archive too large ({len(resp.content)} bytes) from {url}"
                        _debug(last_error)
                        continue
                    # Extract zip, stripping the top-level directory
                    target_path.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                        # Identify the root prefix (e.g., "repo-main/")
                        names = zf.namelist()
                        if not names:
                            raise RuntimeError(f"Empty archive from {url}")
                        root_prefix = names[0]
                        if not root_prefix.endswith("/"):
                            # Single file archive; extract as-is
                            zf.extractall(target_path)
                            return
                        for member in zf.infolist():
                            # Strip root prefix
                            if member.filename == root_prefix:
                                continue
                            rel = member.filename[len(root_prefix) :]
                            if not rel:
                                continue
                            # Guard: prevent zip path traversal (CWE-22)
                            dest = target_path / rel
                            if not dest.resolve().is_relative_to(target_path.resolve()):
                                _debug(f"Skipping zip entry escaping target: {member.filename}")
                                continue
                            if member.is_dir():
                                dest.mkdir(parents=True, exist_ok=True)
                            else:
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                with zf.open(member) as src, open(dest, "wb") as dst:
                                    dst.write(src.read())
                    _debug(f"Extracted Artifactory archive to {target_path}")
                    return
                else:
                    last_error = f"HTTP {resp.status_code} from {url}"
                    _debug(last_error)
            except zipfile.BadZipFile:
                last_error = f"Invalid zip archive from {url}"
                _debug(last_error)
            except requests.RequestException as e:
                last_error = str(e)
                _debug(f"Request failed: {last_error}")

        raise RuntimeError(
            f"Failed to download package {owner}/{repo}#{ref} from Artifactory "
            f"({host}/{prefix}). Last error: {last_error}"
        )

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
        """Download a single file from Artifactory.

        Tries the Archive Entry Download API first (fetches one file
        without downloading the full archive).  Falls back to the full
        archive approach when the entry API is unavailable or returns an
        error.
        """
        # Fast path: use the RegistryClient interface for entry download
        cfg = self._host.registry_config
        if cfg is not None and cfg.host == host:
            client = cfg.get_client()
            content = client.fetch_file(
                owner,
                repo,
                file_path,
                ref,
                resilient_get=self._host._resilient_get,
            )
        else:
            # No RegistryConfig or host mismatch (explicit FQDN mode) --
            # fall back to the standalone helper.
            from .artifactory_entry import fetch_entry_from_archive

            content = fetch_entry_from_archive(
                host,
                prefix,
                owner,
                repo,
                file_path,
                ref,
                scheme=scheme,
                headers=self.get_artifactory_headers(),
                resilient_get=self._host._resilient_get,
            )
        if content is not None:
            return content

        # Fallback: download full archive and extract the file
        import io
        import zipfile

        archive_urls = build_artifactory_archive_url(host, prefix, owner, repo, ref, scheme=scheme)
        headers = self.get_artifactory_headers()

        for url in archive_urls:
            try:
                resp = self._host._resilient_get(url, headers=headers, timeout=60)
                if resp.status_code != 200:
                    continue
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    names = zf.namelist()
                    root_prefix = names[0] if names else ""
                    target_name = root_prefix + file_path
                    if target_name in names:
                        return zf.read(target_name)
                    if file_path in names:
                        return zf.read(file_path)
            except (zipfile.BadZipFile, requests.RequestException):
                continue

        raise RuntimeError(
            f"Failed to download file '{file_path}' from Artifactory "
            f"({host}/{prefix}/{owner}/{repo}#{ref})"
        )

    # ------------------------------------------------------------------
    # Raw / CDN download helper
    # ------------------------------------------------------------------

    def try_raw_download(self, owner: str, repo: str, ref: str, file_path: str) -> bytes | None:
        """Attempt to fetch a file via raw.githubusercontent.com (CDN).

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
        """Download a file from Azure DevOps repository.

        Args:
            dep_ref: Parsed dependency reference with ADO-specific fields
            file_path: Path to file within the repository
            ref: Git reference (branch, tag, or commit SHA)

        Returns:
            bytes: File content
        """
        import base64

        # Validate required ADO fields before proceeding
        if not all([dep_ref.ado_organization, dep_ref.ado_project, dep_ref.ado_repo]):
            raise ValueError(
                "Invalid Azure DevOps dependency reference: missing "
                "organization, project, or repo. "
                f"Got: org={dep_ref.ado_organization}, "
                f"project={dep_ref.ado_project}, repo={dep_ref.ado_repo}"
            )

        host = dep_ref.host or "dev.azure.com"
        api_url = build_ado_api_url(
            dep_ref.ado_organization,
            dep_ref.ado_project,
            dep_ref.ado_repo,
            file_path,
            ref,
            host,
        )

        # Set up authentication headers - ADO uses Basic auth with PAT
        headers: dict[str, str] = {}
        if self._host.ado_token:
            # ADO uses Basic auth: username can be empty, password is the PAT
            auth = base64.b64encode(f":{self._host.ado_token}".encode()).decode()
            headers["Authorization"] = f"Basic {auth}"

        try:
            response = self._host._resilient_get(api_url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # Try fallback branches
                if ref not in ["main", "master"]:
                    raise RuntimeError(
                        f"File not found: {file_path} at ref '{ref}' in {dep_ref.repo_url}"
                    ) from e

                fallback_ref = "master" if ref == "main" else "main"
                fallback_url = build_ado_api_url(
                    dep_ref.ado_organization,
                    dep_ref.ado_project,
                    dep_ref.ado_repo,
                    file_path,
                    fallback_ref,
                    host,
                )

                try:
                    response = self._host._resilient_get(fallback_url, headers=headers, timeout=30)
                    response.raise_for_status()
                    return response.content
                except requests.exceptions.HTTPError as fallback_err:
                    raise RuntimeError(
                        f"File not found: {file_path} in {dep_ref.repo_url} "
                        f"(tried refs: {ref}, {fallback_ref})"
                    ) from fallback_err
            elif e.response.status_code in (401, 403):
                error_msg = f"Authentication failed for Azure DevOps {dep_ref.repo_url}. "
                if not self._host.ado_token:
                    error_msg += self._host.auth_resolver.build_error_context(
                        host,
                        "download",
                        org=dep_ref.ado_organization if dep_ref else None,
                        port=dep_ref.port if dep_ref else None,
                        dep_url=dep_ref.repo_url if dep_ref else None,
                    )
                else:
                    error_msg += "Please check your Azure DevOps PAT permissions."
                raise RuntimeError(error_msg) from e
            else:
                raise RuntimeError(
                    f"Failed to download {file_path}: HTTP {e.response.status_code}"
                ) from e
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error downloading {file_path}: {e}") from e

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
        """Download a file via GitLab REST v4 ``repository/files/.../raw``."""
        host = dep_ref.host or default_host()
        host_info = self._host.auth_resolver.classify_host(host)
        project_path = dep_ref.repo_url
        if not project_path:
            raise RuntimeError("Missing repository path for GitLab file download")

        org = project_path.split("/")[0]
        file_ctx = self._host.auth_resolver.resolve(host, org, port=dep_ref.port)
        token = file_ctx.token
        headers = AuthResolver.gitlab_rest_headers(token)

        api_base = host_info.api_base.rstrip("/")
        enc_proj = quote(project_path, safe="")
        enc_file = quote(file_path, safe="")

        def _raw_url(r: str) -> str:
            return (
                f"{api_base}/projects/{enc_proj}/repository/files/{enc_file}/raw"
                f"?ref={quote(r, safe='')}"
            )

        api_url = _raw_url(ref)

        try:
            response = self._host._resilient_get(api_url, headers=headers, timeout=30)
            response.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return response.content
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                if ref not in ("main", "master"):
                    raise RuntimeError(
                        f"File not found: {file_path} at ref '{ref}' in {dep_ref.repo_url}"
                    ) from e
                fallback_ref = "master" if ref == "main" else "main"
                fallback_url = _raw_url(fallback_ref)
                try:
                    response = self._host._resilient_get(fallback_url, headers=headers, timeout=30)
                    response.raise_for_status()
                    if verbose_callback:
                        verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
                    return response.content
                except requests.exceptions.HTTPError as fallback_err:
                    raise RuntimeError(
                        f"File not found: {file_path} in {dep_ref.repo_url} "
                        f"(tried refs: {ref}, {fallback_ref})"
                    ) from fallback_err
            if e.response is not None and e.response.status_code in (401, 403):
                error_msg = (
                    f"Authentication failed for GitLab {dep_ref.repo_url} "
                    f"(file: {file_path}, ref: {ref}). "
                )
                if not token:
                    error_msg += self._host.auth_resolver.build_error_context(
                        host, "download", org=org, port=dep_ref.port
                    )
                else:
                    error_msg += (
                        "Please verify your token can read this project (required API scope)."
                    )
                raise RuntimeError(error_msg) from e
            if e.response is not None:
                raise RuntimeError(
                    f"Failed to download {file_path}: HTTP {e.response.status_code}"
                ) from e
            raise
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error downloading {file_path}: {e}") from e

    # ------------------------------------------------------------------
    # GitHub file download
    # ------------------------------------------------------------------

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

        # Parse owner/repo from repo_url
        owner, repo = dep_ref.repo_url.split("/", 1)

        # Resolve token via AuthResolver for CDN fast-path decision
        org = None
        if dep_ref and dep_ref.repo_url:
            parts = dep_ref.repo_url.split("/")
            if parts:
                org = parts[0]
        file_ctx = self._host.auth_resolver.resolve(host, org, port=dep_ref.port)
        token = file_ctx.token

        # --- CDN fast-path for github.com without a token ---
        # raw.githubusercontent.com is served from GitHub's CDN and is not
        # subject to the REST API rate limit (60 req/h unauthenticated).
        # Only available for github.com -- GHES/GHE-DR have no equivalent.
        if host.lower() == "github.com" and not token:
            content = self.try_raw_download(owner, repo, ref, file_path)
            if content is not None:
                if verbose_callback:
                    verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
                return content
            # raw download returned 404 -- could be wrong default branch.
            # Try the other default branch before falling through to the API.
            if ref in ("main", "master"):
                fallback_ref = "master" if ref == "main" else "main"
                content = self.try_raw_download(owner, repo, fallback_ref, file_path)
                if content is not None:
                    if verbose_callback:
                        verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
                    return content
            # All raw attempts failed -- fall through to API path which
            # handles private repos, rate-limit messaging, and SAML errors.

        # --- Generic host: raw URL first, then API version negotiation ---
        # For non-GitHub non-GHE hosts (Gitea, Gogs, self-hosted git), try the
        # raw URL path first, then negotiate API versions v1 -> v3.
        is_github_host = is_github_hostname(host) or self._is_configured_ghes(host)
        if not is_github_host:
            raw_url = f"https://{host}/{owner}/{repo}/raw/{ref}/{file_path}"
            raw_headers = self._build_generic_host_auth_headers(host, file_ctx, accept=None)
            if verbose_callback:
                verbose_callback(f"Trying raw URL on generic host {host}: {raw_url}")
            try:
                response = self._host._resilient_get(raw_url, headers=raw_headers, timeout=30)
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

        # --- Contents API path (authenticated, enterprise, or raw fallback) ---
        # Build API URL candidates - format differs by host type
        api_url_candidates = self._build_contents_api_urls(
            host, owner, repo, file_path, ref, is_github_host=is_github_host
        )
        api_url = api_url_candidates[0]

        # Set up authentication headers
        # GitHub family: use GitHub raw-media accept header. Generic hosts
        # ignore it and may return JSON envelopes -- handle that on read.
        accept = "application/vnd.github.v3.raw" if is_github_host else "application/json"
        if is_github_host:
            headers: dict[str, str] = {"Accept": accept}
            if token:
                headers["Authorization"] = f"token {token}"
        else:
            headers = self._build_generic_host_auth_headers(host, file_ctx, accept=accept)

        # Try to download with the specified ref
        try:
            if verbose_callback and not is_github_host:
                verbose_callback(f"Trying Contents API on {host}: {api_url}")
            response = self._host._resilient_get(api_url, headers=headers, timeout=30)
            response.raise_for_status()
            if verbose_callback:
                verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
            return self._extract_contents_api_payload(response, is_github_host)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # For generic hosts, try remaining API version candidates before ref fallback
                for candidate_url in api_url_candidates[1:]:
                    try:
                        if verbose_callback:
                            verbose_callback(
                                f"Contents API 404; trying next candidate: {candidate_url}"
                            )
                        candidate_resp = self._host._resilient_get(
                            candidate_url, headers=headers, timeout=30
                        )
                        candidate_resp.raise_for_status()
                        if verbose_callback:
                            verbose_callback(
                                f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}"
                            )
                        return self._extract_contents_api_payload(candidate_resp, is_github_host)
                    except requests.exceptions.HTTPError as ce:
                        if ce.response.status_code != 404:
                            raise RuntimeError(  # noqa: B904
                                f"Failed to download {file_path}: HTTP {ce.response.status_code}"
                            )

                # Try fallback branches if the specified ref fails
                if ref not in ["main", "master"]:
                    raise RuntimeError(  # noqa: B904
                        self._build_unsupported_or_missing_error(
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
                fallback_url_candidates = self._build_contents_api_urls(
                    host, owner, repo, file_path, fallback_ref
                )

                for fallback_url in fallback_url_candidates:
                    try:
                        response = self._host._resilient_get(
                            fallback_url, headers=headers, timeout=30
                        )
                        response.raise_for_status()
                        if verbose_callback:
                            verbose_callback(
                                f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}"
                            )
                        return self._extract_contents_api_payload(response, is_github_host)
                    except requests.exceptions.HTTPError as fe:
                        if fe.response.status_code != 404:
                            raise RuntimeError(  # noqa: B904
                                f"Failed to download {file_path}: HTTP {fe.response.status_code}"
                            )

                raise RuntimeError(  # noqa: B904
                    self._build_unsupported_or_missing_error(
                        host,
                        dep_ref.repo_url,
                        file_path,
                        ref,
                        api_url_candidates,
                        is_github_host=is_github_host,
                        fallback_ref=fallback_ref,
                    )
                )
            elif e.response.status_code in (401, 403):
                # Distinguish rate limiting from auth failure.
                # X-RateLimit-* headers are GitHub-specific; treat as
                # rate-limit only when the host is in the GitHub family.
                is_rate_limit = False
                if is_github_host:
                    try:
                        rl_remaining = e.response.headers.get("X-RateLimit-Remaining")
                        if rl_remaining is not None and int(rl_remaining) == 0:
                            is_rate_limit = True
                    except (TypeError, ValueError):
                        pass

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
                if token and is_github_host and not host.lower().endswith(".ghe.com"):
                    try:
                        unauth_headers: dict[str, str] = {"Accept": "application/vnd.github.v3.raw"}
                        response = self._host._resilient_get(
                            api_url, headers=unauth_headers, timeout=30
                        )
                        response.raise_for_status()
                        if verbose_callback:
                            verbose_callback(
                                f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}"
                            )
                        return self._extract_contents_api_payload(response, is_github_host)
                    except requests.exceptions.HTTPError:
                        pass  # Fall through to the original error

                error_msg = (
                    f"Authentication failed for {dep_ref.repo_url} "
                    f"(file: {file_path}, ref: {ref}). "
                )
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
            else:
                raise RuntimeError(
                    f"Failed to download {file_path}: HTTP {e.response.status_code}"
                ) from e
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error downloading {file_path}: {e}")  # noqa: B904

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

        SECURITY GUARD: Only attach Authorization when the token is
        unambiguously intended for this host. A token resolved from a
        global env var (GITHUB_APM_PAT, GITHUB_TOKEN, GH_TOKEN) MUST NOT
        be sent to an arbitrary non-GitHub host -- doing so leaks the
        user's GitHub PAT to whatever FQDN is in the dependency line.
        The clone path at ``get_clone_url`` already enforces the same
        guard via ``is_github_hostname``; this mirrors it for HTTP file
        downloads.

        Forwarding is allowed when:
        - source == ``git-credential-fill``: git's credential helper
          looks tokens up by host, so they are host-scoped by
          construction.
        - source == ``GITHUB_APM_PAT_<ORG>``: per-org env var is
          explicit user opt-in for that org's host.
        - the user opted into this host as their GitHub Enterprise
          Server via ``GITHUB_HOST=<host>``: the token is intended for
          this host, even if the FQDN is not under ``*.ghe.com``.
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
        except (ValueError, UnicodeDecodeError, AttributeError):
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
