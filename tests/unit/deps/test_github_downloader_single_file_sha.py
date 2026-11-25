"""Unit tests for SHA resolution on single-file (virtual) dependencies.

Workstream A1: ``download_virtual_file_package`` must populate
``PackageInfo.resolved_reference`` with the resolved 40-char commit SHA
on success. A declared ref is fetched by its admitted commit and an
unresolvable declared ref refuses the install; an undeclared ref (the
default branch) falls back to ``None`` on any lookup failure.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.deps.git_file_transport import GitFileFetchResult
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.github_rate_limit import GitHubThrottle, GitHubThrottleError
from apm_cli.models.apm_package import DependencyReference, GitReferenceType

pytestmark = pytest.mark.component


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


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep reachability markers out of the developer's cache, and see only scripted refs.

    The cells script ``git ls-remote``. An install attempt another test left
    open would answer ``owner/repo`` from its listing memo, and the in-process
    anonymous listing would ask the real github.com first.
    """
    monkeypatch.setenv("APM_CACHE_DIR", str(tmp_path / "cache"))
    from apm_cli.cache.commit_reachability import open_verdict_stage

    open_verdict_stage().discard()  # closes any attempt another test left open
    monkeypatch.setattr(
        "apm_cli.deps.anonymous_ref_listing.list_refs_anonymously", lambda *a, **k: None
    )


def _ls_remote(stdout: str, returncode: int = 0) -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr="")


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

        listing = _ls_remote(f"{self.SHA}\trefs/heads/main\n{'f' * 40}\trefs/tags/heads/main\n")
        with (
            patch.object(downloader._strategies, "resilient_get") as mock_get,
            patch.object(downloader._strategies, "download_github_file") as mock_dl,
            patch("apm_cli.utils.git_env.git_remote_refs", return_value=listing),
        ):
            mock_dl.return_value = _file_content()
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        # A branch name resolves from the exact ref listing, not the commits API.
        assert mock_get.call_count == 0

        rr = pkg_info.resolved_reference
        assert rr is not None
        assert rr.resolved_commit == self.SHA
        assert rr.ref_name == "main"
        assert rr.ref_type == GitReferenceType.BRANCH
        # The file is fetched by the admitted commit, not by the ref name.
        assert mock_dl.call_args.kwargs["ref"] == self.SHA

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

    def test_sparse_throttle_fallback_overrides_api_sha_with_fetch_head(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        """The sparse fallback's observed SHA is the lock provenance anchor."""
        dep = _make_virtual_file_dep(ref=None)
        throttle = GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com")

        with (
            patch.object(downloader._refs, "resolve_commit_sha_for_ref", return_value=None),
            patch.object(downloader._strategies, "download_github_file", side_effect=throttle),
            patch.object(
                downloader._strategies,
                "download_github_file_via_throttle_fallback",
                return_value=GitFileFetchResult(_file_content(), self.SHA),
            ) as fallback,
        ):
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        assert pkg_info.resolved_reference is not None
        assert pkg_info.resolved_reference.resolved_commit == self.SHA
        fallback.assert_called_once_with(dep, dep.virtual_path, "main", throttle)

    def test_throttle_fallback_fetches_the_admitted_commit(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        dep = _make_virtual_file_dep(ref="v1")
        throttle = GitHubThrottleError(GitHubThrottle(429, "http-429"), "github.com")

        with (
            patch(
                "apm_cli.utils.git_env.git_remote_refs",
                return_value=_ls_remote(f"{self.SHA}\trefs/tags/v1\n"),
            ),
            patch.object(downloader._strategies, "download_github_file", side_effect=throttle),
            patch.object(
                downloader._strategies,
                "download_github_file_via_throttle_fallback",
                return_value=GitFileFetchResult(_file_content(), self.SHA),
            ) as fallback,
        ):
            downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        fallback.assert_called_once_with(dep, dep.virtual_path, self.SHA, throttle)
        assert any((tmp_path / "vpkg").rglob("*.prompt.md"))


# ---------------------------------------------------------------------------
# A1 -- error/fallback paths (an undeclared ref must NOT fail the install)
# ---------------------------------------------------------------------------


class TestShaResolutionFallback:
    def test_404_swallowed_resolved_commit_is_none(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        dep = _make_virtual_file_dep(ref=None)

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
        dep = _make_virtual_file_dep(ref=None)

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
        dep = _make_virtual_file_dep(ref=None)

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


# ---------------------------------------------------------------------------
# Declared refs: admitted exactly, fail closed
# ---------------------------------------------------------------------------


class TestDeclaredRefAdmission:
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def test_api_miss_falls_back_to_exact_ls_remote_tip(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        dep = _make_virtual_file_dep(ref="v1")
        listing = _ls_remote(f"{self.SHA}\trefs/tags/v1\n{'f' * 40}\trefs/pull/1/head\n")

        with (
            patch.object(downloader._strategies, "resilient_get") as mock_get,
            patch.object(downloader._strategies, "download_github_file") as mock_dl,
            patch("apm_cli.utils.git_env.git_remote_refs", return_value=listing),
        ):
            mock_get.return_value = _fake_response(404, "Not Found")
            mock_dl.return_value = _file_content()
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        assert pkg_info.resolved_reference.resolved_commit == self.SHA
        assert mock_dl.call_args.kwargs["ref"] == self.SHA
        assert any((tmp_path / "vpkg").rglob("*.prompt.md"))

    @pytest.mark.parametrize("ref", ["main", "pull/1/head"])
    def test_unresolvable_declared_ref_refuses_without_fetch(
        self, ref: str, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        from apm_cli.cache.commit_reachability import CommitNotReachableError

        dep = _make_virtual_file_dep(ref=ref)
        listing = _ls_remote(f"{'f' * 40}\trefs/pull/1/head\n")

        with (
            patch.object(downloader._strategies, "resilient_get") as mock_get,
            patch.object(downloader._strategies, "download_github_file") as mock_dl,
            patch("apm_cli.utils.git_env.git_remote_refs", return_value=listing),
            pytest.raises(CommitNotReachableError),
        ):
            mock_get.side_effect = ConnectionError("boom")
            downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        assert mock_dl.call_count == 0
        assert not (tmp_path / "vpkg").exists()


# ---------------------------------------------------------------------------
# Accepted residual: proxy (Artifactory) routes have no Git origin to admit
# against; the proxy's upstream configuration is the trust boundary. Pinned
# here so a change is deliberate.
# ---------------------------------------------------------------------------


class TestProxyRouteResidual:
    SHA = "0123456789abcdef0123456789abcdef01234567"

    @pytest.fixture(autouse=True)
    def _origin_contacts(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        # Every admission contacts the origin through the clone transport plan.
        calls: list[str] = []
        monkeypatch.setattr(
            "apm_cli.deps.clone_engine.CloneEngine.execute",
            lambda _engine, base, *_args, **_kwargs: calls.append(base),
        )
        self.calls = calls
        return calls

    def test_a_commit_pinned_file_through_artifactory_installs_without_admission(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        dep = DependencyReference(
            repo_url="owner/repo",
            host="artifactory.example.com",
            artifactory_prefix="api/vcs/git",
            reference=self.SHA,
            virtual_path="prompts/p.prompt.md",
            is_virtual=True,
        )

        with patch.object(downloader, "download_raw_file", return_value=_file_content()) as raw:
            downloader.download_virtual_file_package(dep, tmp_path / "vpkg")

        assert raw.call_args.args[2] == self.SHA
        assert any((tmp_path / "vpkg").rglob("*.prompt.md"))
        assert self.calls == []

    def test_a_commit_pinned_package_through_artifactory_installs_without_admission(
        self, tmp_path: Path, downloader: GitHubPackageDownloader
    ) -> None:
        dep = DependencyReference(
            repo_url="owner/repo",
            host="artifactory.example.com",
            artifactory_prefix="api/vcs/git",
            reference=self.SHA,
        )
        target = tmp_path / "pkg"

        def _download(_orchestrator, _dep, path, **_kwargs):
            path.mkdir(parents=True)
            (path / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
            return MagicMock()

        with patch(
            "apm_cli.deps.artifactory_orchestrator.ArtifactoryOrchestrator.download_package",
            autospec=True,
            side_effect=_download,
        ):
            downloader.download_package(dep, target)

        assert (target / "apm.yml").is_file()
        assert self.calls == []
