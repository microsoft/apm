"""Hermetic end-to-end test for Artifactory VCS proxy download_package.

Coverage target
---------------
``ArtifactoryOrchestrator.download_package`` with the *proxy_info* path
(dep_ref is a plain GitHub dep; routing is forced through an in-process
HTTP server that serves a valid APM ZIP archive).

Design
------
* In-process ``http.server.HTTPServer`` bound to ``127.0.0.1:0`` (OS-chosen
  port, no firewall, no secrets, fully offline).
* ``_LocalArchiveDownloader`` stub that does real HTTP fetch (via
  ``urllib.request``) and real ZIP extraction (stripping the root prefix).
  The stub mirrors the interface of ``DownloadDelegate.download_artifactory_archive``
  without requiring a live ``HostInfo``; ZIP extraction runs inside the stub
  (not in ``ArtifactoryOrchestrator`` itself).
* The ZIP contains ``<root>/<content>`` where ``<root>`` is ``testrepo-main/``
  so that root-prefix stripping lands ``apm.yml`` and ``.apm/`` directly in
  ``target_path``.
* ``ArtifactoryOrchestrator.download_package`` runs on real production code
  with the stub satisfying the archive-downloader protocol.
* URL assertions use ``urllib.parse`` component comparison, never substring.
"""

from __future__ import annotations

import io
import threading
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from apm_cli.deps.artifactory_orchestrator import ArtifactoryOrchestrator
from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.models.apm_package import DependencyReference, PackageInfo, PackageType
from apm_cli.utils.archive import ArchiveError, safe_extract_zip

# ---------------------------------------------------------------------------
# Helpers: build a minimal APM ZIP
# ---------------------------------------------------------------------------

_APM_YML_CONTENT = b"name: test-art-package\nversion: 0.1.0\n"
_PLACEHOLDER_CONTENT = b"placeholder\n"

# Root prefix that the production code strips (matches {repo}-{ref}/ convention)
_ZIP_ROOT = "testrepo-main/"


def _build_apm_zip() -> bytes:
    """Return bytes of a valid APM package ZIP with a single root directory.

    Layout inside the ZIP::

        testrepo-main/
        testrepo-main/apm.yml
        testrepo-main/.apm/
        testrepo-main/.apm/instructions/
        testrepo-main/.apm/instructions/guide.md

    After root-prefix stripping the caller receives::

        apm.yml
        .apm/instructions/guide.md
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Root directory entry (must come first so root_prefix = names[0])
        root_info = zipfile.ZipInfo(_ZIP_ROOT)
        root_info.external_attr = 0o755 << 16
        zf.writestr(root_info, "")

        # apm.yml
        zf.writestr(_ZIP_ROOT + "apm.yml", _APM_YML_CONTENT)

        # .apm/ directory
        apm_dir_info = zipfile.ZipInfo(_ZIP_ROOT + ".apm/")
        apm_dir_info.external_attr = 0o755 << 16
        zf.writestr(apm_dir_info, "")

        # .apm/instructions/ directory
        instr_dir_info = zipfile.ZipInfo(_ZIP_ROOT + ".apm/instructions/")
        instr_dir_info.external_attr = 0o755 << 16
        zf.writestr(instr_dir_info, "")

        # .apm/instructions/guide.md (at least one file so the dir is real)
        zf.writestr(_ZIP_ROOT + ".apm/instructions/guide.md", _PLACEHOLDER_CONTENT)

    return buf.getvalue()


# Pre-build once so every test request serves identical bytes
_ZIP_BYTES: bytes = _build_apm_zip()


def _build_traversal_zip() -> bytes:
    """Return a ZIP whose root-stripped member attempts to escape target_path."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        root_info = zipfile.ZipInfo(_ZIP_ROOT)
        root_info.external_attr = 0o755 << 16
        zf.writestr(root_info, "")
        zf.writestr(_ZIP_ROOT + "../escape.txt", b"pwned")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Helpers: in-process HTTP server
# ---------------------------------------------------------------------------


class _ArchiveHTTPServer(HTTPServer):
    """HTTP server carrying the archive bytes served by ``_ZipHandler``."""

    archive_bytes: bytes


class _ZipHandler(BaseHTTPRequestHandler):
    """Serve the pre-built ZIP for any GET request path."""

    def do_GET(self) -> None:
        archive_bytes = self.server.archive_bytes
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(archive_bytes)))
        self.end_headers()
        self.wfile.write(archive_bytes)

    def log_message(self, fmt: str, *args: object) -> None:  # pragma: no cover
        # Suppress request logs in test output
        pass


class _LocalZipServer:
    """Thin wrapper: start / stop an in-process HTTPServer on 127.0.0.1:0."""

    def __init__(self, archive_bytes: bytes = _ZIP_BYTES) -> None:
        self._server = _ArchiveHTTPServer(("127.0.0.1", 0), _ZipHandler)
        self._server.archive_bytes = archive_bytes
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        addr, port = self._server.server_address
        return f"{addr}:{port}"

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture(scope="module")
def zip_server() -> Iterator[_LocalZipServer]:
    """Start the in-process ZIP server once for this module."""
    srv = _LocalZipServer()
    srv.start()
    yield srv
    srv.stop()


# ---------------------------------------------------------------------------
# Stub _HasArchiveDownloader
# ---------------------------------------------------------------------------


class _LocalArchiveDownloader:
    """Real HTTP fetch + real ZIP extraction.

    Implements the ``_HasArchiveDownloader`` protocol.  Unlike
    ``DownloadDelegate`` it does not need a live ``HostInfo``; it uses
    ``urllib.request`` (no auth) and performs the same root-prefix stripping
    that the production implementation does.

    The ``captured_url`` attribute is set by each call so the test can
    inspect the URL that would have been sent to a real Artifactory server.
    """

    def __init__(self) -> None:
        self.captured_url: str | None = None

    def download_artifactory_archive(
        self,
        host: str,
        prefix: str,
        owner: str,
        repo: str,
        ref: str,
        target_path: Path,
        *,
        scheme: str = "https",
    ) -> None:
        import urllib.error
        import urllib.request

        # Build first-candidate URL (GitHub-style) -- same formula as
        # build_artifactory_archive_url candidate 0.
        url = f"{scheme}://{host}/{prefix}/{owner}/{repo}/archive/refs/heads/{ref}.zip"
        self.captured_url = url

        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                data = resp.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Download failed for {url}: {exc}") from exc

        # Extract, stripping root prefix -- mirrors DownloadDelegate logic
        target_path.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if not names:
                raise RuntimeError(f"Empty archive from {url}")
            root_prefix = names[0]
            if root_prefix.endswith("/"):

                def _strip_root(member_name: str) -> str | None:
                    if member_name == root_prefix:
                        return None
                    if not member_name.startswith(root_prefix):
                        raise ArchiveError(
                            f"Archive member is outside root prefix {root_prefix!r}: "
                            f"{member_name!r}"
                        )
                    rel = member_name[len(root_prefix) :]
                    return rel or None

                safe_extract_zip(
                    zf,
                    target_path,
                    error_type=ArchiveError,
                    member_name_transform=_strip_root,
                )
                return

            safe_extract_zip(zf, target_path, error_type=ArchiveError)


def _real_archive_downloader() -> DownloadDelegate:
    """Build a real DownloadDelegate with enough host surface for integration tests."""
    host = SimpleNamespace(registry_config=None, artifactory_token=None)
    delegate = DownloadDelegate(host)
    host._resilient_get = delegate.resilient_get
    return delegate


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

_PROXY_PREFIX = "artifactory/github"
_OWNER = "testorg"
_REPO = "testrepo"
_REF = "main"


class TestArtifactoryInstallE2E:
    """Hermetic offline e2e for ``ArtifactoryOrchestrator.download_package``.

    Checks:
    1. ``download_package`` returns a ``PackageInfo`` with the expected fields.
    2. Root prefix is stripped: ``apm.yml`` and ``.apm/`` exist directly under
       ``target_path`` (not inside a ``testrepo-main/`` subdirectory).
    3. APM validation passed (no raise; ``PackageType.APM_PACKAGE``).
    4. URL assertions use ``urllib.parse`` component comparison only.
    5. No live marks; no secrets; no env vars; no production-code edits.
    """

    def test_download_package_returns_package_info(
        self, zip_server: _LocalZipServer, tmp_path: Path
    ) -> None:
        """``download_package`` returns ``PackageInfo`` with correct structure."""
        downloader = _LocalArchiveDownloader()
        orchestrator = ArtifactoryOrchestrator(archive_downloader=downloader)

        dep_ref = DependencyReference(
            repo_url=f"{_OWNER}/{_REPO}",
            host="github.com",
            reference=_REF,
        )
        # dep_ref.is_artifactory() is False (no artifactory_prefix), so the
        # orchestrator uses proxy_info.
        proxy_info = (zip_server.host, _PROXY_PREFIX, "http")
        target_path = tmp_path / "installed"

        result = orchestrator.download_package(dep_ref, target_path, proxy_info=proxy_info)

        # (1) PackageInfo structure
        assert isinstance(result, PackageInfo)
        assert result.install_path == target_path
        assert result.package is not None
        assert result.package_type is not None
        assert result.dependency_ref is dep_ref

    def test_root_prefix_stripped(self, zip_server: _LocalZipServer, tmp_path: Path) -> None:
        """``apm.yml`` and ``.apm/`` land directly in ``target_path`` (no nesting)."""
        downloader = _LocalArchiveDownloader()
        orchestrator = ArtifactoryOrchestrator(archive_downloader=downloader)

        dep_ref = DependencyReference(
            repo_url=f"{_OWNER}/{_REPO}",
            host="github.com",
            reference=_REF,
        )
        proxy_info = (zip_server.host, _PROXY_PREFIX, "http")
        target_path = tmp_path / "installed2"

        orchestrator.download_package(dep_ref, target_path, proxy_info=proxy_info)

        # (2) Root prefix stripped
        assert (target_path / "apm.yml").exists(), "apm.yml must exist directly under target_path"
        assert (target_path / ".apm").is_dir(), ".apm/ must exist directly under target_path"

    def test_apm_validation_passed(self, zip_server: _LocalZipServer, tmp_path: Path) -> None:
        """APM validation does not raise and returns APM_PACKAGE type."""
        downloader = _LocalArchiveDownloader()
        orchestrator = ArtifactoryOrchestrator(archive_downloader=downloader)

        dep_ref = DependencyReference(
            repo_url=f"{_OWNER}/{_REPO}",
            host="github.com",
            reference=_REF,
        )
        proxy_info = (zip_server.host, _PROXY_PREFIX, "http")
        target_path = tmp_path / "installed3"

        # (3) No raise from APM validation
        result = orchestrator.download_package(dep_ref, target_path, proxy_info=proxy_info)
        assert result.package_type == PackageType.APM_PACKAGE

    def test_url_via_urllib_parse_components(
        self, zip_server: _LocalZipServer, tmp_path: Path
    ) -> None:
        """URL components are correct (no substring checks)."""
        downloader = _LocalArchiveDownloader()
        orchestrator = ArtifactoryOrchestrator(archive_downloader=downloader)

        dep_ref = DependencyReference(
            repo_url=f"{_OWNER}/{_REPO}",
            host="github.com",
            reference=_REF,
        )
        proxy_info = (zip_server.host, _PROXY_PREFIX, "http")
        target_path = tmp_path / "installed4"

        orchestrator.download_package(dep_ref, target_path, proxy_info=proxy_info)

        # (4) URL assertions via urllib.parse components only
        assert downloader.captured_url is not None
        parsed = urlparse(downloader.captured_url)

        assert parsed.scheme == "http"
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == zip_server.port

        # Path must start with /artifactory/github/testorg/testrepo/
        path_parts = [p for p in parsed.path.split("/") if p]
        assert path_parts[0] == "artifactory"
        assert path_parts[1] == "github"
        assert path_parts[2] == _OWNER
        assert path_parts[3] == _REPO

        # Must end with .zip (GitHub-style candidate)
        assert parsed.path.endswith(".zip")

    def test_malicious_zip_traversal_rejected_through_real_downloader(self, tmp_path: Path) -> None:
        """Real Artifactory install path rejects zip-slip and writes no escape file."""
        zip_server = _LocalZipServer(_build_traversal_zip())
        zip_server.start()
        try:
            downloader = _real_archive_downloader()
            orchestrator = ArtifactoryOrchestrator(archive_downloader=downloader)
            dep_ref = DependencyReference(
                repo_url=f"{_OWNER}/{_REPO}",
                host="github.com",
                reference=_REF,
            )
            proxy_info = (zip_server.host, _PROXY_PREFIX, "http")
            target_path = tmp_path / "installed-malicious"

            with pytest.raises(RuntimeError, match=r"Unsafe zip archive.*path-traversal"):
                orchestrator.download_package(dep_ref, target_path, proxy_info=proxy_info)

            assert not (tmp_path / "escape.txt").exists()
            assert not (target_path / "escape.txt").exists()
        finally:
            zip_server.stop()
