"""Backend-specific download delegates for APM packages.

Encapsulates HTTP resilient-get, GitHub API file download, Azure DevOps
file download, and Artifactory archive download logic.  The owning
:class:`~apm_cli.deps.github_downloader.GitHubPackageDownloader` creates
a single :class:`DownloadDelegate` instance and delegates download
operations to it (Facade/Delegate pattern).
"""

import os
import random
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import requests

from ..models.apm_package import DependencyReference
from ..utils.github_host import (
    build_ado_api_url,
    build_ado_https_clone_url,
    build_ado_ssh_url,
    build_artifactory_archive_url,
    build_https_clone_url,
    build_raw_content_url,
    build_ssh_url,
    default_host,
    is_azure_devops_hostname,
    is_github_hostname,
)


# ---------------------------------------------------------------------------
# Module-level debug helper (mirrors the one in github_downloader so that
# this module has no import dependency on the orchestrator).
# ---------------------------------------------------------------------------

def _debug(message: str) -> None:
    """Print debug message if APM_DEBUG environment variable is set."""
    if os.environ.get('APM_DEBUG'):
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
        headers: Dict[str, str],
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
                            wait = min(2 ** attempt, 30) * (0.5 + random.random())
                    elif reset_at:
                        try:
                            wait = max(0, min(int(reset_at) - time.time(), 60))
                        except (TypeError, ValueError):
                            wait = min(2 ** attempt, 30) * (0.5 + random.random())
                    else:
                        wait = min(2 ** attempt, 30) * (0.5 + random.random())
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
                    wait = min(2 ** attempt, 30) * (0.5 + random.random())
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
        raise requests.exceptions.RequestException(
            f"All {max_retries} attempts failed for {url}"
        )

    # ------------------------------------------------------------------
    # Repository URL building
    # ------------------------------------------------------------------

    def build_repo_url(
        self,
        repo_ref: str,
        use_ssh: bool = False,
        dep_ref: DependencyReference = None,
        token: Optional[str] = None,
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
        # Use dep_ref.host if available (for ADO), otherwise fall back to
        # instance or default
        if dep_ref and dep_ref.host:
            host = dep_ref.host
        else:
            host = getattr(self._host, 'github_host', None) or default_host()

        # Check if this is Azure DevOps (either via dep_ref or host detection)
        is_ado = (
            (dep_ref and dep_ref.is_azure_devops())
            or is_azure_devops_hostname(host)
        )
        is_insecure = (
            bool(getattr(dep_ref, "is_insecure", False))
            if dep_ref is not None
            else False
        )

        # Use provided token or fall back to instance default.  Pass an empty
        # string ("") explicitly to suppress the per-instance token (used by
        # the TransportSelector for "plain HTTPS" / "SSH" attempts that must
        # NOT embed credentials in the URL).
        if token == "":
            github_token = ""
            ado_token = ""
        else:
            github_token = token if token is not None else self._host.github_token
            ado_token = (
                token if (token is not None and is_ado) else self._host.ado_token
            )

        _debug(
            f"build_repo_url: host={host}, is_ado={is_ado}, "
            f"dep_ref={'present' if dep_ref else 'None'}, "
            f"ado_org={dep_ref.ado_organization if dep_ref else None}"
        )

        if is_ado and dep_ref and dep_ref.ado_organization:
            # Use Azure DevOps URL builders with ADO-specific token
            if use_ssh:
                return build_ado_ssh_url(
                    dep_ref.ado_organization, dep_ref.ado_project, dep_ref.ado_repo
                )
            elif auth_scheme == "bearer":
                # Bearer tokens are injected via GIT_CONFIG env vars
                # (Authorization header), NOT embedded in the clone URL.
                return build_ado_https_clone_url(
                    dep_ref.ado_organization,
                    dep_ref.ado_project,
                    dep_ref.ado_repo,
                    token=None,
                    host=host,
                )
            elif ado_token:
                return build_ado_https_clone_url(
                    dep_ref.ado_organization,
                    dep_ref.ado_project,
                    dep_ref.ado_repo,
                    token=ado_token,
                    host=host,
                )
            else:
                return build_ado_https_clone_url(
                    dep_ref.ado_organization,
                    dep_ref.ado_project,
                    dep_ref.ado_repo,
                    host=host,
                )
        else:
            # Determine if this host should receive a GitHub token
            is_github = is_github_hostname(host)
            # Thread the user-declared custom port (e.g. 7999 for Bitbucket DC)
            # through the URL builders so neither SSH nor HTTPS attempts
            # silently drop it.
            port = dep_ref.port if dep_ref else None
            if use_ssh:
                return build_ssh_url(host, repo_ref, port=port)
            elif is_insecure:
                netloc = f"{host}:{port}" if port else host
                return f"http://{netloc}/{repo_ref}.git"
            elif is_github and github_token:
                # Only send GitHub tokens to GitHub hosts
                return build_https_clone_url(
                    host, repo_ref, token=github_token, port=port
                )
            else:
                # Generic hosts: plain HTTPS, let git credential helpers
                # handle auth
                return build_https_clone_url(host, repo_ref, token=None, port=port)

    # ------------------------------------------------------------------
    # Artifactory helpers
    # ------------------------------------------------------------------

    def get_artifactory_headers(self) -> Dict[str, str]:
        """Build HTTP headers for registry/Artifactory requests."""
        cfg = self._host.registry_config
        if cfg is not None:
            return cfg.get_headers()
        # Fallback: direct artifactory_token attribute (legacy path)
        headers: Dict[str, str] = {}
        if self._host.artifactory_token:
            headers['Authorization'] = f'Bearer {self._host.artifactory_token}'
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

        archive_urls = build_artifactory_archive_url(
            host, prefix, owner, repo, ref, scheme=scheme
        )
        headers = self.get_artifactory_headers()

        # Guard: reject unreasonably large archives (default 500 MB)
        max_archive_bytes = int(
            os.environ.get('ARTIFACTORY_MAX_ARCHIVE_MB', '500')
        ) * 1024 * 1024

        last_error = None
        for url in archive_urls:
            _debug(f"Trying Artifactory archive: {url}")
            try:
                resp = self._host._resilient_get(url, headers=headers, timeout=60)
                if resp.status_code == 200:
                    if len(resp.content) > max_archive_bytes:
                        last_error = (
                            f"Archive too large ({len(resp.content)} bytes) from {url}"
                        )
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
                        if not root_prefix.endswith('/'):
                            # Single file archive; extract as-is
                            zf.extractall(target_path)
                            return
                        for member in zf.infolist():
                            # Strip root prefix
                            if member.filename == root_prefix:
                                continue
                            rel = member.filename[len(root_prefix):]
                            if not rel:
                                continue
                            # Guard: prevent zip path traversal (CWE-22)
                            dest = target_path / rel
                            if not dest.resolve().is_relative_to(
                                target_path.resolve()
                            ):
                                _debug(
                                    "Skipping zip entry escaping target: "
                                    f"{member.filename}"
                                )
                                continue
                            if member.is_dir():
                                dest.mkdir(parents=True, exist_ok=True)
                            else:
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                with zf.open(member) as src, open(dest, 'wb') as dst:
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
                owner, repo, file_path, ref,
                resilient_get=self._host._resilient_get,
            )
        else:
            # No RegistryConfig or host mismatch (explicit FQDN mode) --
            # fall back to the standalone helper.
            from .artifactory_entry import fetch_entry_from_archive

            content = fetch_entry_from_archive(
                host, prefix, owner, repo, file_path, ref,
                scheme=scheme,
                headers=self.get_artifactory_headers(),
                resilient_get=self._host._resilient_get,
            )
        if content is not None:
            return content

        # Fallback: download full archive and extract the file
        import io
        import zipfile

        archive_urls = build_artifactory_archive_url(
            host, prefix, owner, repo, ref, scheme=scheme
        )
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

    def try_raw_download(
        self, owner: str, repo: str, ref: str, file_path: str
    ) -> Optional[bytes]:
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
        if not all(
            [dep_ref.ado_organization, dep_ref.ado_project, dep_ref.ado_repo]
        ):
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
        headers: Dict[str, str] = {}
        if self._host.ado_token:
            # ADO uses Basic auth: username can be empty, password is the PAT
            auth = base64.b64encode(
                f":{self._host.ado_token}".encode()
            ).decode()
            headers['Authorization'] = f'Basic {auth}'

        try:
            response = self._host._resilient_get(api_url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # Try fallback branches
                if ref not in ["main", "master"]:
                    raise RuntimeError(
                        f"File not found: {file_path} at ref '{ref}' "
                        f"in {dep_ref.repo_url}"
                    )

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
                    response = self._host._resilient_get(
                        fallback_url, headers=headers, timeout=30
                    )
                    response.raise_for_status()
                    return response.content
                except requests.exceptions.HTTPError:
                    raise RuntimeError(
                        f"File not found: {file_path} in {dep_ref.repo_url} "
                        f"(tried refs: {ref}, {fallback_ref})"
                    )
            elif e.response.status_code in (401, 403):
                error_msg = (
                    "Authentication failed for Azure DevOps "
                    f"{dep_ref.repo_url}. "
                )
                if not self._host.ado_token:
                    error_msg += self._host.auth_resolver.build_error_context(
                        host,
                        "download",
                        org=dep_ref.ado_organization if dep_ref else None,
                        port=dep_ref.port if dep_ref else None,
                        dep_url=dep_ref.repo_url if dep_ref else None,
                    )
                else:
                    error_msg += (
                        "Please check your Azure DevOps PAT permissions."
                    )
                raise RuntimeError(error_msg)
            else:
                raise RuntimeError(
                    f"Failed to download {file_path}: "
                    f"HTTP {e.response.status_code}"
                )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error downloading {file_path}: {e}")

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
        owner, repo = dep_ref.repo_url.split('/', 1)

        # Resolve token via AuthResolver for CDN fast-path decision
        org = None
        if dep_ref and dep_ref.repo_url:
            parts = dep_ref.repo_url.split('/')
            if parts:
                org = parts[0]
        file_ctx = self._host.auth_resolver.resolve(
            host, org, port=dep_ref.port
        )
        token = file_ctx.token

        # --- CDN fast-path for github.com without a token ---
        # raw.githubusercontent.com is served from GitHub's CDN and is not
        # subject to the REST API rate limit (60 req/h unauthenticated).
        # Only available for github.com -- GHES/GHE-DR have no equivalent.
        if host.lower() == "github.com" and not token:
            content = self.try_raw_download(owner, repo, ref, file_path)
            if content is not None:
                if verbose_callback:
                    verbose_callback(
                        f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}"
                    )
                return content
            # raw download returned 404 -- could be wrong default branch.
            # Try the other default branch before falling through to the API.
            if ref in ("main", "master"):
                fallback_ref = "master" if ref == "main" else "main"
                content = self.try_raw_download(
                    owner, repo, fallback_ref, file_path
                )
                if content is not None:
                    if verbose_callback:
                        verbose_callback(
                            f"Downloaded file: "
                            f"{host}/{dep_ref.repo_url}/{file_path}"
                        )
                    return content
            # All raw attempts failed -- fall through to API path which
            # handles private repos, rate-limit messaging, and SAML errors.

        # --- Contents API path (authenticated, enterprise, or raw fallback) ---
        # Build GitHub API URL - format differs by host type
        if host == "github.com":
            api_url = (
                f"https://api.github.com/repos/{owner}/{repo}"
                f"/contents/{file_path}?ref={ref}"
            )
        elif host.lower().endswith(".ghe.com"):
            api_url = (
                f"https://api.{host}/repos/{owner}/{repo}"
                f"/contents/{file_path}?ref={ref}"
            )
        else:
            api_url = (
                f"https://{host}/api/v3/repos/{owner}/{repo}"
                f"/contents/{file_path}?ref={ref}"
            )

        # Set up authentication headers
        headers: Dict[str, str] = {
            'Accept': 'application/vnd.github.v3.raw'  # Returns raw content
        }
        if token:
            headers['Authorization'] = f'token {token}'

        # Try to download with the specified ref
        try:
            response = self._host._resilient_get(
                api_url, headers=headers, timeout=30
            )
            response.raise_for_status()
            if verbose_callback:
                verbose_callback(
                    f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}"
                )
            return response.content
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # Try fallback branches if the specified ref fails
                if ref not in ["main", "master"]:
                    raise RuntimeError(
                        f"File not found: {file_path} at ref '{ref}' "
                        f"in {dep_ref.repo_url}"
                    )

                # Try the other default branch
                fallback_ref = "master" if ref == "main" else "main"

                # Build fallback API URL
                if host == "github.com":
                    fallback_url = (
                        f"https://api.github.com/repos/{owner}/{repo}"
                        f"/contents/{file_path}?ref={fallback_ref}"
                    )
                elif host.lower().endswith(".ghe.com"):
                    fallback_url = (
                        f"https://api.{host}/repos/{owner}/{repo}"
                        f"/contents/{file_path}?ref={fallback_ref}"
                    )
                else:
                    fallback_url = (
                        f"https://{host}/api/v3/repos/{owner}/{repo}"
                        f"/contents/{file_path}?ref={fallback_ref}"
                    )

                try:
                    response = self._host._resilient_get(
                        fallback_url, headers=headers, timeout=30
                    )
                    response.raise_for_status()
                    if verbose_callback:
                        verbose_callback(
                            f"Downloaded file: "
                            f"{host}/{dep_ref.repo_url}/{file_path}"
                        )
                    return response.content
                except requests.exceptions.HTTPError:
                    raise RuntimeError(
                        f"File not found: {file_path} in {dep_ref.repo_url} "
                        f"(tried refs: {ref}, {fallback_ref})"
                    )
            elif e.response.status_code in (401, 403):
                # Distinguish rate limiting from auth failure.
                is_rate_limit = False
                try:
                    rl_remaining = e.response.headers.get(
                        "X-RateLimit-Remaining"
                    )
                    if rl_remaining is not None and int(rl_remaining) == 0:
                        is_rate_limit = True
                except (TypeError, ValueError):
                    pass

                if is_rate_limit:
                    error_msg = (
                        "GitHub API rate limit exceeded for "
                        f"{dep_ref.repo_url}. "
                    )
                    if not token:
                        error_msg += (
                            "Unauthenticated requests are limited to "
                            "60/hour (shared per IP). "
                            + self._host.auth_resolver.build_error_context(
                                host,
                                "API request (rate limited)",
                                org=owner,
                                port=(
                                    dep_ref.port if dep_ref else None
                                ),
                                dep_url=(
                                    dep_ref.repo_url if dep_ref else None
                                ),
                            )
                        )
                    else:
                        error_msg += (
                            "Authenticated rate limit exhausted. "
                            "Wait a few minutes or check your token's "
                            "rate-limit quota."
                        )
                    raise RuntimeError(error_msg)

                # Token may lack SSO/SAML authorization for this org.
                # Retry without auth -- the repo might be public.
                if token and not host.lower().endswith(".ghe.com"):
                    try:
                        unauth_headers: Dict[str, str] = {
                            'Accept': 'application/vnd.github.v3.raw'
                        }
                        response = self._host._resilient_get(
                            api_url, headers=unauth_headers, timeout=30
                        )
                        response.raise_for_status()
                        if verbose_callback:
                            verbose_callback(
                                f"Downloaded file: "
                                f"{host}/{dep_ref.repo_url}/{file_path}"
                            )
                        return response.content
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
                elif token and not host.lower().endswith(".ghe.com"):
                    error_msg += (
                        "Both authenticated and unauthenticated access "
                        "were attempted. The repository may be private, "
                        "or your token may lack SSO/SAML authorization "
                        "for this organization."
                    )
                else:
                    error_msg += (
                        "Please check your GitHub token permissions."
                    )
                raise RuntimeError(error_msg)
            else:
                raise RuntimeError(
                    f"Failed to download {file_path}: "
                    f"HTTP {e.response.status_code}"
                )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Network error downloading {file_path}: {e}")
