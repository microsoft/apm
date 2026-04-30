"""Artifactory registry backend -- Archive Entry Download.

JFrog Artifactory supports downloading individual entries from an archived
artifact without fetching the entire archive.  The URL pattern appends
``!/{path}`` to the archive URL::

    GET {archive_url}!/{root_prefix}/{file_path}

Both GitHub and GitLab archives use a root directory prefix of
``{repo}-{ref}/``, though hosting platforms may normalize the ref
(e.g. ``feature/foo`` becomes ``feature-foo`` in the directory name).
This module tries both the raw and normalized forms.

:class:`ArtifactoryRegistryClient` implements the :class:`RegistryClient`
protocol defined in :mod:`~apm_cli.deps.registry_proxy` so the download
pipeline can fetch files without knowing which registry type is in use.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, List, Optional  # noqa: F401, UP035
from urllib.parse import quote

import requests as _requests

if TYPE_CHECKING:
    from .registry_proxy import RegistryConfig

logger = logging.getLogger(__name__)


class ArtifactoryRegistryClient:
    """Artifactory backend for the :class:`RegistryClient` protocol.

    Constructed via :meth:`RegistryConfig.get_client`; callers interact
    with the :class:`RegistryClient` protocol, not this class directly.
    """

    def __init__(self, config: RegistryConfig) -> None:
        self._config = config

    # -- RegistryClient protocol ---------------------------------------------

    def fetch_file(
        self,
        owner: str,
        repo: str,
        file_path: str,
        ref: str = "main",
        resilient_get: Callable | None = None,
    ) -> bytes | None:
        """Fetch a single file via the Archive Entry Download API.

        Tries each candidate archive URL (GitHub heads, GitLab, GitHub
        tags) with both raw and normalized root prefixes.  Returns raw
        file bytes on success, or ``None`` when the entry API is not
        supported or the file is not found -- the caller should fall
        back to downloading the full archive.
        """
        return _fetch_entry(
            host=self._config.host,
            prefix=self._config.prefix,
            owner=owner,
            repo=repo,
            file_path=file_path,
            ref=ref,
            scheme=self._config.scheme,
            headers=self._config.get_headers(),
            resilient_get=resilient_get,
        )


# ---------------------------------------------------------------------------
# Standalone helper (for callers without a RegistryConfig)
# ---------------------------------------------------------------------------


def fetch_entry_from_archive(
    host: str,
    prefix: str,
    owner: str,
    repo: str,
    file_path: str,
    ref: str = "main",
    scheme: str = "https",
    headers: dict | None = None,
    resilient_get: Callable | None = None,
) -> bytes | None:
    """Fetch a single file from an Artifactory-proxied archive.

    Convenience wrapper around the core entry-download logic for callers
    that do not have a :class:`RegistryConfig` instance (e.g. the
    marketplace client in #506).

    Returns raw file bytes on success, or ``None`` on failure.
    """
    return _fetch_entry(
        host=host,
        prefix=prefix,
        owner=owner,
        repo=repo,
        file_path=file_path,
        ref=ref,
        scheme=scheme,
        headers=headers,
        resilient_get=resilient_get,
    )


# ---------------------------------------------------------------------------
# Core implementation (shared by class and standalone function)
# ---------------------------------------------------------------------------


def _fetch_entry(
    host: str,
    prefix: str,
    owner: str,
    repo: str,
    file_path: str,
    ref: str,
    scheme: str,
    headers: dict | None,
    resilient_get: Callable | None,
) -> bytes | None:
    """Core entry-download logic shared by the class and standalone helper."""
    from ..utils.github_host import build_artifactory_archive_url
    from ..utils.path_security import PathTraversalError, validate_path_segments

    # Guard: reject traversal sequences via the centralized path validator
    try:
        validate_path_segments(
            file_path,
            context="artifactory archive entry path",
            reject_empty=True,
        )
    except PathTraversalError:
        logger.debug("Refusing invalid file_path: %s", file_path)
        return None

    archive_urls = build_artifactory_archive_url(
        host,
        prefix,
        owner,
        repo,
        ref,
        scheme=scheme,
    )

    # Root directory inside the archive is typically "{repo}-{ref}", but
    # hosting platforms may normalize refs (e.g. "feature/foo" -> "feature-foo").
    root_prefixes: list[str] = [f"{repo}-{ref}"]
    normalized_ref = ref.replace("/", "-")
    if normalized_ref != ref:
        normalized_root = f"{repo}-{normalized_ref}"
        if normalized_root not in root_prefixes:
            root_prefixes.append(normalized_root)

    req_headers = headers or {}

    for archive_url in archive_urls:
        for root_prefix in root_prefixes:
            # URL-encode the entry path (spaces, special chars) but keep '/' as-is
            encoded_path = quote(f"{root_prefix}/{file_path}", safe="/")
            entry_url = f"{archive_url}!/{encoded_path}"
            try:
                if resilient_get is not None:
                    resp = resilient_get(entry_url, headers=req_headers, timeout=30)
                else:
                    resp = _requests.get(
                        entry_url,
                        headers=req_headers,
                        timeout=30,
                    )
                if resp.status_code == 200:
                    logger.debug("Archive entry download OK: %s", entry_url)
                    return resp.content
                logger.debug(
                    "Archive entry download HTTP %d: %s",
                    resp.status_code,
                    entry_url,
                )
            except _requests.RequestException:
                logger.debug(
                    "Archive entry download failed: %s",
                    entry_url,
                    exc_info=True,
                )
                continue

    return None
