"""Unit tests for SHA resolution on single-file (virtual) dependencies.

Workstream A1: ``download_virtual_file_package`` must populate
``PackageInfo.resolved_reference`` with the resolved 40-char commit SHA
on success, and gracefully fall back to ``None`` on any failure (404,
network, non-GitHub host) so the install pipeline never breaks on the
SHA lookup.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference, GitReferenceType


def _make_virtual_file_dep(
    repo_url: str = "owner/repo",
    vpath: str = "prompts/test.prompt.md",
    ref: str | None = "main",
    host: str | None = None,
) -> DependencyReference:
    return DependencyReference(
        repo_url=repo_url,
        host=host,
        reference=ref,
        virtual_path=vpath,
        is_virtual=True,
    )


def _fake_response(status_code: int, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


def _file_content(body: str = "# Test prompt\n") -> bytes:
    return f"---\ndescription: Test\n---\n\n{body}".encode()


@pytest.fixture
def downloader() -> GitHubPackageDownloader:
    """A GitHubPackageDownloader with a stub auth resolver (no token)."""
    auth = MagicMock()
    ctx = MagicMock()
    ctx.token = None
    auth.resolve.return_value = ctx
    return GitHubPackageDownloader(auth_resolver=auth)


# ---------------------------------------------------------------------------
# A1 -- happy path: SHA resolved and propagated
# ---------------------------------------------------------------------------


class TestSingleFileShaResolution:
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def test_resolved_sha_lands_on_package_info(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        dep = _make_virtual_file_dep()

        with (
            patch.object(downloader._strategies, "resilient_get") as mock_get,
            patch.object(downloader._strategies, "download_github_file") as mock_dl,
        ):
            mock_get.return_value = _fake_response(200, self.SHA)
            mock_dl.return_value = _file_content()
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        # Cheap commits API was called exactly once.
        assert mock_get.call_count == 1
        call = mock_get.call_args
        url = call.args[0]
        headers = call.args[1] if len(call.args) > 1 else call.kwargs.get("headers", {})
        from urllib.parse import urlparse

        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.hostname == "api.github.com"
        assert parsed.path == "/repos/owner/repo/commits/main"
        # Accept header asks for the SHA-only response shape.
        assert headers.get("Accept") == "application/vnd.github.sha"

        rr = pkg_info.resolved_reference
        assert rr is not None
        assert rr.resolved_commit == self.SHA
        assert rr.ref_name == "main"
        assert rr.ref_type == GitReferenceType.BRANCH

    def test_explicit_sha_ref_is_preserved_without_extra_call(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        # If the user passes a 40-char SHA as the ref, the resolver short
        # circuits and does NOT need an HTTP round-trip.
        dep = _make_virtual_file_dep(ref=self.SHA)

        with (
            patch.object(downloader._strategies, "resilient_get") as mock_get,
            patch.object(downloader._strategies, "download_github_file") as mock_dl,
        ):
            mock_get.return_value = _fake_response(200, self.SHA)
            mock_dl.return_value = _file_content()
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        # No call to the commits API -- the SHA is already resolved.
        assert mock_get.call_count == 0
        rr = pkg_info.resolved_reference
        assert rr.resolved_commit == self.SHA
        assert rr.ref_type == GitReferenceType.COMMIT


# ---------------------------------------------------------------------------
# A1 -- error/fallback paths (must NOT fail the install)
# ---------------------------------------------------------------------------


class TestShaResolutionFallback:
    def test_404_swallowed_resolved_commit_is_none(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        dep = _make_virtual_file_dep()

        with (
            patch.object(downloader._strategies, "resilient_get") as mock_get,
            patch.object(downloader._strategies, "download_github_file") as mock_dl,
        ):
            mock_get.return_value = _fake_response(404, "Not Found")
            mock_dl.return_value = _file_content()
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        rr = pkg_info.resolved_reference
        assert rr is not None
        assert rr.resolved_commit is None
        assert rr.ref_name == "main"

    def test_network_exception_swallowed_resolved_commit_is_none(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        dep = _make_virtual_file_dep()

        with (
            patch.object(downloader._strategies, "resilient_get") as mock_get,
            patch.object(downloader._strategies, "download_github_file") as mock_dl,
        ):
            mock_get.side_effect = ConnectionError("boom")
            mock_dl.return_value = _file_content()
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        rr = pkg_info.resolved_reference
        assert rr.resolved_commit is None

    def test_unexpected_body_shape_swallowed(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        # If the API returns a JSON blob (Accept negotiation failed for some
        # reason), we should NOT mistake the body for a SHA.
        dep = _make_virtual_file_dep()

        with (
            patch.object(downloader._strategies, "resilient_get") as mock_get,
            patch.object(downloader._strategies, "download_github_file") as mock_dl,
        ):
            mock_get.return_value = _fake_response(200, '{"sha": "...."}')
            mock_dl.return_value = _file_content()
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        rr = pkg_info.resolved_reference
        assert rr.resolved_commit is None

    def test_artifactory_dep_falls_back_to_ref_name_only(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        # An Artifactory-hosted dep should never trigger the commits API
        # call (no equivalent endpoint we want to depend on).
        dep = DependencyReference(
            repo_url="owner/repo",
            host="artifactory.example.com",
            artifactory_prefix="api/vcs/git",
            reference="main",
            virtual_path="prompts/p.prompt.md",
            is_virtual=True,
        )

        with (
            patch.object(downloader._strategies, "resilient_get") as mock_get,
            patch.object(downloader._strategies, "download_github_file") as mock_dl,
            patch.object(downloader, "download_raw_file", return_value=_file_content()),
        ):
            mock_get.return_value = _fake_response(200, "f" * 40)
            mock_dl.return_value = _file_content()
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        # No commits API call attempted.
        assert mock_get.call_count == 0
        rr = pkg_info.resolved_reference
        assert rr is not None
        assert rr.resolved_commit is None
        assert rr.ref_name == "main"
