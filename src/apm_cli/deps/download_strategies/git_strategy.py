"""Backend-specific download delegates for APM packages.

Encapsulates HTTP resilient-get, GitHub API file download, Azure DevOps
file download, and Artifactory archive download logic.  The owning
:class:`~apm_cli.deps.github_downloader.GitHubPackageDownloader` creates
a single :class:`DownloadDelegate` instance and delegates download
operations to it (Facade/Delegate pattern).

This module is a thin re-exporter: implementation lives in the private
sibling modules ``_git_ado_gitlab``, ``_git_github_download``, and
``_git_host_utils``.  All names below were previously defined here and
remain importable as ``apm_cli.deps.download_strategies.git_strategy.<name>``
so that existing callers (``class_.py``, tests) see no change.
"""

from ._git_ado_gitlab import download_ado_file, download_gitlab_file
from ._git_github_download import (
    _handle_http_401_or_403,
    _HttpDownloadContext,
    _try_raw_cdn_download,
    download_github_file,
    try_raw_download,
)
from ._git_host_utils import (
    _build_contents_api_urls,
    _build_generic_host_auth_headers,
    _build_unsupported_or_missing_error,
    _decode_json_envelope,
    _extract_contents_api_payload,
    _is_configured_ghes,
    _MissingFileCtx,
)

__all__ = [
    "_HttpDownloadContext",
    "_MissingFileCtx",
    "_build_contents_api_urls",
    "_build_generic_host_auth_headers",
    "_build_unsupported_or_missing_error",
    "_decode_json_envelope",
    "_extract_contents_api_payload",
    "_handle_http_401_or_403",
    "_is_configured_ghes",
    "_try_raw_cdn_download",
    "download_ado_file",
    "download_github_file",
    "download_gitlab_file",
    "try_raw_download",
]
