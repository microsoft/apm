"""Backend (Artifactory / Azure DevOps / GitLab) file-download ops for
:class:`~apm_cli.deps.download_strategies.DownloadDelegate`.

Moved bodies (kept thin wrappers on the class). Each function takes the
owning ``DownloadDelegate`` as ``delegate``. Names that tests patch on
``apm_cli.deps.download_strategies`` are referenced through a function-level
``_ds`` alias so the patch still applies; this module never imports the
original module at module scope (avoids an import cycle).
"""

import base64
import io
import os
import zipfile
from pathlib import Path

import requests

from ..models.apm_package import DependencyReference


def download_artifactory_archive(
    delegate,
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
    from apm_cli.deps import download_strategies as _ds

    archive_urls = _ds.build_artifactory_archive_url(host, prefix, owner, repo, ref, scheme=scheme)
    headers = delegate.get_artifactory_headers()

    # Guard: reject unreasonably large archives (default 500 MB)
    max_archive_bytes = int(os.environ.get("ARTIFACTORY_MAX_ARCHIVE_MB", "500")) * 1024 * 1024

    last_error = None
    for url in archive_urls:
        _ds._debug(f"Trying Artifactory archive: {url}")
        try:
            resp = delegate._host._resilient_get(url, headers=headers, timeout=60)
            if resp.status_code == 200:
                if len(resp.content) > max_archive_bytes:
                    last_error = f"Archive too large ({len(resp.content)} bytes) from {url}"
                    _ds._debug(last_error)
                    continue
                _extract_stripped_archive(resp.content, target_path, url)
                _ds._debug(f"Extracted Artifactory archive to {target_path}")
                return
            last_error = f"HTTP {resp.status_code} from {url}"
            _ds._debug(last_error)
        except zipfile.BadZipFile:
            last_error = f"Invalid zip archive from {url}"
            _ds._debug(last_error)
        except requests.RequestException as e:
            last_error = str(e)
            _ds._debug(f"Request failed: {last_error}")

    raise RuntimeError(
        f"Failed to download package {owner}/{repo}#{ref} from Artifactory "
        f"({host}/{prefix}). Last error: {last_error}"
    )


def _extract_stripped_archive(content: bytes, target_path: Path, url: str) -> None:
    """Extract a zip archive into *target_path*, stripping the root prefix."""
    from apm_cli.deps import download_strategies as _ds

    target_path.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError(f"Empty archive from {url}")
        root_prefix = names[0]
        if not root_prefix.endswith("/"):
            # Single file archive; extract as-is
            zf.extractall(target_path)
            return
        for member in zf.infolist():
            if member.filename == root_prefix:
                continue
            rel = member.filename[len(root_prefix) :]
            if not rel:
                continue
            # Guard: prevent zip path traversal (CWE-22)
            dest = target_path / rel
            if not dest.resolve().is_relative_to(target_path.resolve()):
                _ds._debug(f"Skipping zip entry escaping target: {member.filename}")
                continue
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                if unix_mode:
                    os.chmod(dest, unix_mode & 0o755)


def download_file_from_artifactory(
    delegate,
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
    from apm_cli.deps import download_strategies as _ds

    # Fast path: use the RegistryClient interface for entry download
    cfg = delegate._host.registry_config
    if cfg is not None and cfg.host == host:
        client = cfg.get_client()
        content = client.fetch_file(
            owner,
            repo,
            file_path,
            ref,
            resilient_get=delegate._host._resilient_get,
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
            headers=delegate.get_artifactory_headers(),
            resilient_get=delegate._host._resilient_get,
        )
    if content is not None:
        return content

    # Fallback: download full archive and extract the file
    archive_urls = _ds.build_artifactory_archive_url(host, prefix, owner, repo, ref, scheme=scheme)
    headers = delegate.get_artifactory_headers()

    for url in archive_urls:
        try:
            resp = delegate._host._resilient_get(url, headers=headers, timeout=60)
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


def _ado_build_headers(delegate, dep_ref: DependencyReference, host: str) -> dict[str, str]:
    """Build ADO auth headers.

    PAT path is first and unchanged; bearer is strictly the fallback when no
    PAT is present.  Bearer acquisition is routed through ``AuthResolver.resolve``
    so this module stays inside the auth-protocol boundary; auth.py's resolver
    handles the AAD bearer lookup internally.
    """
    headers: dict[str, str] = {}
    if delegate._host.ado_token:
        # ADO uses Basic auth: username can be empty, password is the PAT
        auth = base64.b64encode(f":{delegate._host.ado_token}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"
    else:
        # No PAT: ask the resolver for an AAD bearer token.  If az-cli is
        # available and the user is signed in, AuthResolver._resolve_token()
        # returns a bearer token and auth_scheme="bearer" transparently.
        auth_ctx = delegate._host.auth_resolver.resolve(
            host,
            dep_ref.ado_organization,
            port=dep_ref.port,
        )
        if auth_ctx.token and auth_ctx.auth_scheme == "bearer":
            headers["Authorization"] = f"Bearer {auth_ctx.token}"
    return headers


def _ado_check_html_signin(delegate, response, dep_ref: DependencyReference, host: str) -> None:
    """Fail-closed when ADO returns an interactive sign-in HTML page.

    Azure DevOps responds with HTTP 200 + text/html when auth is missing or
    insufficient instead of a 401.  Writing that HTML to disk produces a
    corrupt file (the #1671 bug).  Detect it by Content-Type only on 200
    responses so 404/403 error pages with text/html bodies still fall through
    to raise_for_status and the existing 404-fallback / 401-403 error paths.
    Content-Type is lowercased before comparison per RFC 7230
    case-insensitivity.
    """
    if response.status_code != 200:
        return
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type:
        error_msg = (
            f"Azure DevOps returned a sign-in page for {dep_ref.repo_url}. "
            "The server responded with HTML instead of the requested file, "
            "which means authentication is missing or insufficient. "
        )
        error_msg += delegate._host.auth_resolver.build_error_context(
            host,
            "download",
            org=dep_ref.ado_organization if dep_ref else None,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
        raise RuntimeError(error_msg)


def download_ado_file(
    delegate,
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
    from apm_cli.deps import download_strategies as _ds

    # Validate required ADO fields before proceeding
    if not all([dep_ref.ado_organization, dep_ref.ado_project, dep_ref.ado_repo]):
        raise ValueError(
            "Invalid Azure DevOps dependency reference: missing "
            "organization, project, or repo. "
            f"Got: org={dep_ref.ado_organization}, "
            f"project={dep_ref.ado_project}, repo={dep_ref.ado_repo}"
        )

    host = dep_ref.host or "dev.azure.com"
    api_url = _ds.build_ado_api_url(
        dep_ref.ado_organization,
        dep_ref.ado_project,
        dep_ref.ado_repo,
        file_path,
        ref,
        host,
    )

    headers = _ado_build_headers(delegate, dep_ref, host)

    try:
        response = delegate._host._resilient_get(api_url, headers=headers, timeout=30)
        _ado_check_html_signin(delegate, response, dep_ref, host)
        response.raise_for_status()
        return response.content
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return _ado_handle_404(delegate, e, dep_ref, file_path, ref, host, headers)
        if e.response.status_code in (401, 403):
            raise RuntimeError(_ado_auth_error_msg(delegate, dep_ref, host)) from e
        raise RuntimeError(f"Failed to download {file_path}: HTTP {e.response.status_code}") from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error downloading {file_path}: {e}") from e


def _ado_handle_404(
    delegate,
    e: requests.exceptions.HTTPError,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str,
    host: str,
    headers: dict[str, str],
) -> bytes:
    """Retry the other default branch when an ADO file 404s."""
    from apm_cli.deps import download_strategies as _ds

    if ref not in ["main", "master"]:
        raise RuntimeError(
            f"File not found: {file_path} at ref '{ref}' in {dep_ref.repo_url}"
        ) from e

    fallback_ref = "master" if ref == "main" else "main"
    fallback_url = _ds.build_ado_api_url(
        dep_ref.ado_organization,
        dep_ref.ado_project,
        dep_ref.ado_repo,
        file_path,
        fallback_ref,
        host,
    )

    try:
        response = delegate._host._resilient_get(fallback_url, headers=headers, timeout=30)
        _ado_check_html_signin(delegate, response, dep_ref, host)
        response.raise_for_status()
        return response.content
    except requests.exceptions.HTTPError as fallback_err:
        raise RuntimeError(
            f"File not found: {file_path} in {dep_ref.repo_url} (tried refs: {ref}, {fallback_ref})"
        ) from fallback_err


def _ado_auth_error_msg(delegate, dep_ref: DependencyReference, host: str) -> str:
    """Build the auth-failure message for an ADO 401/403."""
    error_msg = f"Authentication failed for Azure DevOps {dep_ref.repo_url}. "
    if not delegate._host.ado_token:
        error_msg += delegate._host.auth_resolver.build_error_context(
            host,
            "download",
            org=dep_ref.ado_organization if dep_ref else None,
            port=dep_ref.port if dep_ref else None,
            dep_url=dep_ref.repo_url if dep_ref else None,
        )
    else:
        error_msg += "Please check your Azure DevOps PAT permissions."
    return error_msg


def download_gitlab_file(
    delegate,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str = "main",
    verbose_callback=None,
) -> bytes:
    """Download a file via GitLab REST v4 ``repository/files/.../raw``."""
    from apm_cli.deps import download_strategies as _ds

    host = dep_ref.host or _ds.default_host()
    host_info = delegate._host.auth_resolver.classify_host(host)
    project_path = dep_ref.repo_url
    if not project_path:
        raise RuntimeError("Missing repository path for GitLab file download")

    org = project_path.split("/")[0]
    file_ctx = delegate._host.auth_resolver.resolve(host, org, port=dep_ref.port)
    token = file_ctx.token
    headers = _ds.AuthResolver.gitlab_rest_headers(token)

    api_base = host_info.api_base.rstrip("/")
    enc_proj = _ds.quote(project_path, safe="")
    enc_file = _ds.quote(file_path, safe="")

    def _raw_url(r: str) -> str:
        return (
            f"{api_base}/projects/{enc_proj}/repository/files/{enc_file}/raw"
            f"?ref={_ds.quote(r, safe='')}"
        )

    api_url = _raw_url(ref)

    try:
        response = delegate._host._resilient_get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        if verbose_callback:
            verbose_callback(f"Downloaded file: {host}/{dep_ref.repo_url}/{file_path}")
        return response.content
    except requests.exceptions.HTTPError as e:
        return _gitlab_handle_http_error(
            delegate,
            e,
            dep_ref,
            file_path,
            ref,
            host,
            org,
            token,
            headers,
            _raw_url,
            verbose_callback,
        )
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error downloading {file_path}: {e}") from e


def _gitlab_handle_http_error(
    delegate,
    e: requests.exceptions.HTTPError,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str,
    host: str,
    org: str,
    token: str | None,
    headers: dict[str, str],
    raw_url_builder,
    verbose_callback,
) -> bytes:
    """Handle 404/auth/other errors for a GitLab raw file fetch."""
    if e.response is not None and e.response.status_code == 404:
        if ref not in ("main", "master"):
            raise RuntimeError(
                f"File not found: {file_path} at ref '{ref}' in {dep_ref.repo_url}"
            ) from e
        fallback_ref = "master" if ref == "main" else "main"
        fallback_url = raw_url_builder(fallback_ref)
        try:
            response = delegate._host._resilient_get(fallback_url, headers=headers, timeout=30)
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
        raise RuntimeError(
            _gitlab_auth_error_msg(delegate, dep_ref, file_path, ref, host, org, token)
        ) from e
    if e.response is not None:
        raise RuntimeError(f"Failed to download {file_path}: HTTP {e.response.status_code}") from e
    raise e


def _gitlab_auth_error_msg(
    delegate,
    dep_ref: DependencyReference,
    file_path: str,
    ref: str,
    host: str,
    org: str,
    token: str | None,
) -> str:
    """Build the auth-failure message for a GitLab 401/403."""
    error_msg = (
        f"Authentication failed for GitLab {dep_ref.repo_url} (file: {file_path}, ref: {ref}). "
    )
    if not token:
        error_msg += delegate._host.auth_resolver.build_error_context(
            host, "download", org=org, port=dep_ref.port
        )
    else:
        error_msg += "Please verify your token can read this project (required API scope)."
    return error_msg
