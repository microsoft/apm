"""Azure DevOps and GitLab file-download helpers for APM packages.

Implements backend-specific download logic for ADO (REST Items API with
Basic-auth PAT) and GitLab (REST v4 ``repository/files/.../raw``).
All names are private to the ``download_strategies`` package; the public
API surface lives in :mod:`git_strategy` which re-exports everything.
"""

import base64
from dataclasses import dataclass
from urllib.parse import quote

import requests

from ...core.auth import AuthResolver
from ...models.apm_package import DependencyReference
from ...utils.github_host import build_ado_api_url, default_host


@dataclass(frozen=True, slots=True)
class _GitLabErrCtx:
    dep_ref: DependencyReference
    file_path: str
    ref: str
    headers: dict
    host: str
    org: str
    token: object
    verbose_callback: object


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


def _handle_gitlab_http_error(
    self,
    e: requests.exceptions.HTTPError,
    ctx: _GitLabErrCtx,
    raw_url_fn,
) -> bytes:
    """Handle HTTPError from a GitLab file-download attempt.

    Extracted from ``download_gitlab_file`` to reduce McCabe complexity.
    Returns ``bytes`` on a successful fallback, or raises ``RuntimeError``.
    """
    if e.response is not None and e.response.status_code == 404:
        if ctx.ref not in ("main", "master"):
            raise RuntimeError(
                f"File not found: {ctx.file_path} at ref '{ctx.ref}' in {ctx.dep_ref.repo_url}"
            ) from e
        fallback_ref = "master" if ctx.ref == "main" else "main"
        fallback_url = raw_url_fn(fallback_ref)
        try:
            response = self._host._resilient_get(fallback_url, headers=ctx.headers, timeout=30)
            response.raise_for_status()
            if ctx.verbose_callback:
                ctx.verbose_callback(
                    f"Downloaded file: {ctx.host}/{ctx.dep_ref.repo_url}/{ctx.file_path}"
                )
            return response.content
        except requests.exceptions.HTTPError as fallback_err:
            raise RuntimeError(
                f"File not found: {ctx.file_path} in {ctx.dep_ref.repo_url} "
                f"(tried refs: {ctx.ref}, {fallback_ref})"
            ) from fallback_err
    if e.response is not None and e.response.status_code in (401, 403):
        error_msg = (
            f"Authentication failed for GitLab {ctx.dep_ref.repo_url} "
            f"(file: {ctx.file_path}, ref: {ctx.ref}). "
        )
        if not ctx.token:
            error_msg += self._host.auth_resolver.build_error_context(
                ctx.host, "download", org=ctx.org, port=ctx.dep_ref.port
            )
        else:
            error_msg += "Please verify your token can read this project (required API scope)."
        raise RuntimeError(error_msg) from e
    if e.response is not None:
        raise RuntimeError(
            f"Failed to download {ctx.file_path}: HTTP {e.response.status_code}"
        ) from e
    raise e


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
    _err_ctx = _GitLabErrCtx(
        dep_ref=dep_ref,
        file_path=file_path,
        ref=ref,
        headers=headers,
        host=host,
        org=org,
        token=token,
        verbose_callback=verbose_callback,
    )

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
        return _handle_gitlab_http_error(self, e, _err_ctx, _raw_url)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error downloading {file_path}: {e}") from e
