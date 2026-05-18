"""Backend-specific download delegates for APM packages.

Encapsulates HTTP resilient-get, GitHub API file download, Azure DevOps
file download, and Artifactory archive download logic.  The owning
:class:`~apm_cli.deps.github_downloader.GitHubPackageDownloader` creates
a single :class:`DownloadDelegate` instance and delegates download
operations to it (Facade/Delegate pattern).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import requests

from ...utils.github_host import (
    build_artifactory_archive_url,
)
from .class_ import _debug


@dataclass(frozen=True, slots=True)
class _ArtifactoryTarget:
    """Coordinates identifying an Artifactory-proxied repository + ref."""

    host: str
    prefix: str
    owner: str
    repo: str
    ref: str
    scheme: str = "https"


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
    target: _ArtifactoryTarget,
    target_path: Path,
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
        target.host, target.prefix, target.owner, target.repo, ref=target.ref, scheme=target.scheme
    )
    headers = self.get_artifactory_headers()

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
                _extract_artifactory_zip(resp.content, target_path, url)
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
        f"Failed to download package {target.owner}/{target.repo}#{target.ref} from Artifactory "
        f"({target.host}/{target.prefix}). Last error: {last_error}"
    )


def _extract_artifactory_zip(content: bytes, target_path: Path, url: str) -> None:
    """Extract zip archive content to target_path, stripping the root directory."""
    import io
    import zipfile

    target_path.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError(f"Empty archive from {url}")
        root_prefix = names[0]
        if not root_prefix.endswith("/"):
            zf.extractall(target_path)
            return
        for member in zf.infolist():
            if member.filename == root_prefix:
                continue
            rel = member.filename[len(root_prefix) :]
            if not rel:
                continue
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


def download_file_from_artifactory(
    self,
    target: _ArtifactoryTarget,
    file_path: str,
) -> bytes:
    """Download a single file from Artifactory.

    Tries the Archive Entry Download API first (fetches one file
    without downloading the full archive).  Falls back to the full
    archive approach when the entry API is unavailable or returns an
    error.
    """
    # Fast path: use the RegistryClient interface for entry download
    cfg = self._host.registry_config
    if cfg is not None and cfg.host == target.host:
        client = cfg.get_client()
        content = client.fetch_file(
            target.owner,
            target.repo,
            file_path,
            target.ref,
            resilient_get=self._host._resilient_get,
        )
    else:
        # No RegistryConfig or host mismatch (explicit FQDN mode) --
        # fall back to the standalone helper.
        from ..artifactory_entry import _ArchiveCoords, fetch_entry_from_archive

        content = fetch_entry_from_archive(
            _ArchiveCoords(
                host=target.host, prefix=target.prefix, owner=target.owner, repo=target.repo
            ),
            file_path,
            target.ref,
            scheme=target.scheme,
            headers=self.get_artifactory_headers(),
            resilient_get=self._host._resilient_get,
        )
    if content is not None:
        return content

    # Fallback: download full archive and extract the file
    import io
    import zipfile

    archive_urls = build_artifactory_archive_url(
        target.host,
        target.prefix,
        target.owner,
        target.repo,
        ref=target.ref,
        scheme=target.scheme,
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
        f"({target.host}/{target.prefix}/{target.owner}/{target.repo}#{target.ref})"
    )
