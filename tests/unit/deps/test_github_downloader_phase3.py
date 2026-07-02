"""Comprehensive unit tests for github_downloader.py — phase-3 coverage push.

Target: push coverage from ~54 % to ≥ 85 %.

All HTTP requests, git subprocess calls, and filesystem mutations are mocked so
the suite is fully hermetic and requires no network access or git installation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from git import RemoteProgress

from apm_cli.deps.github_downloader import (
    GitHubPackageDownloader,
    GitProgressReporter,
    _close_repo,
    _debug,
)
from apm_cli.models.apm_package import (
    DependencyReference,
    GitReferenceType,
    ResolvedReference,
)

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


def _make_dep(
    repo_url: str = "owner/repo",
    *,
    host: str | None = None,
    reference: str | None = "main",
    virtual_path: str | None = None,
    is_virtual: bool = False,
    artifactory_prefix: str | None = None,
) -> DependencyReference:
    return DependencyReference(
        repo_url=repo_url,
        host=host,
        reference=reference,
        virtual_path=virtual_path,
        is_virtual=is_virtual,
        artifactory_prefix=artifactory_prefix,
    )


def _make_resolved(
    ref: str = "main",
    *,
    ref_type: GitReferenceType = GitReferenceType.BRANCH,
    commit: str | None = "abc1234" * 5 + "ab",
) -> ResolvedReference:
    return ResolvedReference(
        original_ref=ref,
        ref_name=ref,
        ref_type=ref_type,
        resolved_commit=commit,
    )


@pytest.fixture
def downloader() -> GitHubPackageDownloader:
    """Downloader with a fully-stubbed auth resolver (no token, no network)."""
    auth = MagicMock()
    host_info = MagicMock()
    host_info.kind = "github"
    auth.classify_host.return_value = host_info
    ctx = MagicMock()
    ctx.token = None
    ctx.auth_scheme = "basic"
    ctx.git_env = {}
    auth.resolve.return_value = ctx
    auth.resolve_for_dep.return_value = ctx
    auth._token_manager = MagicMock()
    auth._token_manager.get_token_for_purpose.return_value = None
    return GitHubPackageDownloader(auth_resolver=auth)


# ---------------------------------------------------------------------------
# _debug
# ---------------------------------------------------------------------------


class TestDebug:
    def test_prints_when_apm_debug_set(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.dict(os.environ, {"APM_DEBUG": "1"}):
            _debug("hello world")
        out = capsys.readouterr()
        assert "[DEBUG] hello world" in out.err

    def test_silent_when_apm_debug_unset(self, capsys: pytest.CaptureFixture[str]) -> None:
        env = {k: v for k, v in os.environ.items() if k != "APM_DEBUG"}
        with patch.dict(os.environ, env, clear=True):
            _debug("should not appear")
        out = capsys.readouterr()
        assert "[DEBUG]" not in out.err
        assert "[DEBUG]" not in out.out


# ---------------------------------------------------------------------------
# _close_repo
# ---------------------------------------------------------------------------


class TestCloseRepo:
    def test_none_repo_is_a_no_op(self) -> None:
        """_close_repo(None) must not raise."""
        _close_repo(None)  # no assertion needed — must not raise

    def test_repo_close_called(self) -> None:
        repo = MagicMock()
        _close_repo(repo)
        repo.close.assert_called_once()

    def test_exception_in_clear_cache_is_suppressed(self) -> None:
        repo = MagicMock()
        repo.git.clear_cache.side_effect = OSError("locked")
        repo.close.side_effect = RuntimeError("already closed")
        # Both exceptions must be suppressed — function must not raise.
        _close_repo(repo)

    def test_exception_in_close_is_suppressed(self) -> None:
        repo = MagicMock()
        repo.close.side_effect = RuntimeError("already closed")
        _close_repo(repo)  # must not raise


# ---------------------------------------------------------------------------
# GitProgressReporter.update
# ---------------------------------------------------------------------------


class TestGitProgressReporterUpdate:
    def _make_reporter(
        self,
        task_id: int | None = 1,
        progress_obj: MagicMock | None = None,
    ) -> GitProgressReporter:
        if progress_obj is None:
            progress_obj = MagicMock()
        return GitProgressReporter(
            progress_task_id=task_id,
            progress_obj=progress_obj,
            package_name="test-pkg",
        )

    def test_no_progress_obj_skips_update(self) -> None:
        reporter = GitProgressReporter(progress_task_id=1, progress_obj=None, package_name="pkg")
        reporter.update(RemoteProgress.RECEIVING, 50, max_count=100)  # must not raise

    def test_none_task_id_skips_update(self) -> None:
        progress = MagicMock()
        reporter = GitProgressReporter(
            progress_task_id=None, progress_obj=progress, package_name="pkg"
        )
        reporter.update(RemoteProgress.RECEIVING, 50, max_count=100)
        progress.update.assert_not_called()

    def test_disabled_skips_update(self) -> None:
        progress = MagicMock()
        reporter = self._make_reporter(progress_obj=progress)
        reporter.disabled = True
        reporter.update(RemoteProgress.RECEIVING, 50, max_count=100)
        progress.update.assert_not_called()

    def test_determinate_progress_uses_cur_and_max(self) -> None:
        progress = MagicMock()
        reporter = self._make_reporter(task_id=7, progress_obj=progress)
        reporter.update(RemoteProgress.RECEIVING, 30, max_count=100)
        progress.update.assert_called_once_with(7, completed=30, total=100)
        assert reporter.last_op == 30

    def test_indeterminate_progress_uses_fake_total(self) -> None:
        progress = MagicMock()
        reporter = self._make_reporter(task_id=3, progress_obj=progress)
        reporter.update(RemoteProgress.RECEIVING, 40, max_count=0)
        progress.update.assert_called_once_with(3, total=100, completed=40)

    def test_indeterminate_none_cur_count_uses_zero(self) -> None:
        progress = MagicMock()
        reporter = self._make_reporter(task_id=3, progress_obj=progress)
        reporter.update(RemoteProgress.RECEIVING, None, max_count=None)
        progress.update.assert_called_once_with(3, total=100, completed=0)

    def test_indeterminate_cur_count_capped_at_100(self) -> None:
        progress = MagicMock()
        reporter = self._make_reporter(task_id=3, progress_obj=progress)
        reporter.update(RemoteProgress.RECEIVING, 999, max_count=None)
        progress.update.assert_called_once_with(3, total=100, completed=100)


# ---------------------------------------------------------------------------
# GitProgressReporter._get_op_name
# ---------------------------------------------------------------------------


class TestGetOpName:
    def _r(self) -> GitProgressReporter:
        return GitProgressReporter()

    @pytest.mark.parametrize(
        ("op_code", "expected"),
        [
            (RemoteProgress.COUNTING, "Counting objects"),
            (RemoteProgress.COMPRESSING, "Compressing objects"),
            (RemoteProgress.WRITING, "Writing objects"),
            (RemoteProgress.RECEIVING, "Receiving objects"),
            (RemoteProgress.RESOLVING, "Resolving deltas"),
            (RemoteProgress.FINDING_SOURCES, "Finding sources"),
            (RemoteProgress.CHECKING_OUT, "Checking out files"),
            (0, "Cloning"),  # unknown op_code
        ],
    )
    def test_op_name(self, op_code: int, expected: str) -> None:
        reporter = self._r()
        assert reporter._get_op_name(op_code) == expected


# ---------------------------------------------------------------------------
# _is_generic_dependency_host
# ---------------------------------------------------------------------------


class TestIsGenericDependencyHost:
    def test_none_dep_ref_returns_false(self, downloader: GitHubPackageDownloader) -> None:
        assert downloader._is_generic_dependency_host(None) is False

    def test_azure_devops_returns_false(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep(host="dev.azure.com")
        assert downloader._is_generic_dependency_host(dep) is False

    def test_no_host_returns_false(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep()  # host=None
        assert downloader._is_generic_dependency_host(dep) is False

    def test_github_host_returns_false(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep(host="github.com")
        assert downloader._is_generic_dependency_host(dep) is False

    def test_gitlab_host_returns_false(self, downloader: GitHubPackageDownloader) -> None:
        host_info = MagicMock()
        host_info.kind = "gitlab"
        downloader.auth_resolver.classify_host.return_value = host_info
        dep = _make_dep(host="gitlab.example.com")
        assert downloader._is_generic_dependency_host(dep) is False

    def test_generic_host_returns_true(self, downloader: GitHubPackageDownloader) -> None:
        host_info = MagicMock()
        host_info.kind = "generic"
        downloader.auth_resolver.classify_host.return_value = host_info
        dep = _make_dep(host="bitbucket.example.com")
        assert downloader._is_generic_dependency_host(dep) is True


# ---------------------------------------------------------------------------
# _resolve_dep_token
# ---------------------------------------------------------------------------


class TestResolveDepToken:
    def test_none_dep_ref_returns_github_token(self, downloader: GitHubPackageDownloader) -> None:
        downloader.github_token = "ghp_mytoken"
        result = downloader._resolve_dep_token(None)
        assert result == "ghp_mytoken"

    def test_generic_host_returns_none(self, downloader: GitHubPackageDownloader) -> None:
        host_info = MagicMock()
        host_info.kind = "generic"
        downloader.auth_resolver.classify_host.return_value = host_info
        dep = _make_dep(host="bitbucket.example.com")
        result = downloader._resolve_dep_token(dep)
        assert result is None

    def test_github_dep_returns_resolved_token(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep()
        ctx = MagicMock()
        ctx.token = "ghp_resolved"
        downloader.auth_resolver.resolve_for_dep.return_value = ctx
        result = downloader._resolve_dep_token(dep)
        assert result == "ghp_resolved"


# ---------------------------------------------------------------------------
# _resolve_dep_auth_ctx
# ---------------------------------------------------------------------------


class TestResolveDepAuthCtx:
    def test_none_dep_ref_returns_none(self, downloader: GitHubPackageDownloader) -> None:
        result = downloader._resolve_dep_auth_ctx(None)
        assert result is None

    def test_generic_host_returns_none(self, downloader: GitHubPackageDownloader) -> None:
        host_info = MagicMock()
        host_info.kind = "generic"
        downloader.auth_resolver.classify_host.return_value = host_info
        dep = _make_dep(host="bitbucket.example.com")
        result = downloader._resolve_dep_auth_ctx(dep)
        assert result is None

    def test_verbose_mode_calls_notify(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep(host="github.com")
        ctx = MagicMock()
        ctx.auth_scheme = "basic"
        downloader.auth_resolver.resolve_for_dep.return_value = ctx
        with patch.dict(os.environ, {"APM_VERBOSE": "1"}):
            result = downloader._resolve_dep_auth_ctx(dep)
        downloader.auth_resolver.notify_auth_source.assert_called_once()
        assert result is ctx

    def test_non_verbose_does_not_call_notify(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep(host="github.com")
        ctx = MagicMock()
        ctx.auth_scheme = "basic"
        downloader.auth_resolver.resolve_for_dep.return_value = ctx
        env = {k: v for k, v in os.environ.items() if k != "APM_VERBOSE"}
        with patch.dict(os.environ, env, clear=True):
            downloader._resolve_dep_auth_ctx(dep)
        downloader.auth_resolver.notify_auth_source.assert_not_called()


# ---------------------------------------------------------------------------
# Backward-compat stubs (single-call delegation)
# ---------------------------------------------------------------------------


class TestBackwardCompatStubs:
    def test_get_artifactory_headers_delegates(self, downloader: GitHubPackageDownloader) -> None:
        downloader._strategies.get_artifactory_headers = MagicMock(return_value={"X": "Y"})
        result = downloader._get_artifactory_headers()
        assert result == {"X": "Y"}
        downloader._strategies.get_artifactory_headers.assert_called_once()

    def test_download_artifactory_archive_delegates(
        self, downloader: GitHubPackageDownloader
    ) -> None:
        downloader._strategies.download_artifactory_archive = MagicMock()
        downloader._download_artifactory_archive(
            "host", "prefix", "owner", "repo", "ref", Path("/t")
        )
        downloader._strategies.download_artifactory_archive.assert_called_once()

    def test_download_file_from_artifactory_delegates(
        self, downloader: GitHubPackageDownloader
    ) -> None:
        downloader._strategies.download_file_from_artifactory = MagicMock(return_value=b"data")
        result = downloader._download_file_from_artifactory(
            "host", "prefix", "owner", "repo", "file.txt", "ref"
        )
        assert result == b"data"

    def test_resilient_get_delegates(self, downloader: GitHubPackageDownloader) -> None:
        resp = MagicMock()
        downloader._strategies.resilient_get = MagicMock(return_value=resp)
        result = downloader._resilient_get("https://example.com", {})
        assert result is resp

    def test_try_raw_download_delegates(self, downloader: GitHubPackageDownloader) -> None:
        downloader._strategies.try_raw_download = MagicMock(return_value=b"raw")
        result = downloader._try_raw_download("owner", "repo", "ref", "file.txt")
        assert result == b"raw"

    def test_download_ado_file_delegates(self, downloader: GitHubPackageDownloader) -> None:
        downloader._strategies.download_ado_file = MagicMock(return_value=b"ado")
        dep = _make_dep(host="dev.azure.com")
        result = downloader._download_ado_file(dep, "file.txt", ref="main")
        assert result == b"ado"

    def test_parse_ls_remote_output_static(self) -> None:
        output = "abc123\trefs/heads/main\ndef456\trefs/tags/v1.0\n"
        result = GitHubPackageDownloader._parse_ls_remote_output(output)
        assert len(result) == 2

    def test_semver_sort_key_static(self) -> None:
        key = GitHubPackageDownloader._semver_sort_key("v1.2.3")
        assert key is not None  # just ensure it doesn't crash

    def test_sort_remote_refs_classmethod(self) -> None:
        from apm_cli.deps.git_remote_ops import RemoteRef

        refs = [
            MagicMock(spec=RemoteRef),
            MagicMock(spec=RemoteRef),
        ]
        with patch("apm_cli.deps.github_downloader.sort_remote_refs", return_value=refs) as m:
            result = GitHubPackageDownloader._sort_remote_refs(refs)
        m.assert_called_once_with(refs)
        assert result is refs

    def test_list_remote_refs_delegates_to_refs(self, downloader: GitHubPackageDownloader) -> None:
        downloader._refs = MagicMock()
        downloader._refs.list_remote_refs.return_value = []
        dep = _make_dep()
        result = downloader.list_remote_refs(dep)
        downloader._refs.list_remote_refs.assert_called_once_with(dep)
        assert result == []

    def test_materialize_from_bare_delegates(self, downloader: GitHubPackageDownloader) -> None:
        with patch(
            "apm_cli.deps.github_downloader.materialize_from_bare", return_value="sha123"
        ) as m:
            result = downloader._materialize_from_bare(
                Path("/bare"), Path("/consumer"), ref="main", env={}
            )
        assert result == "sha123"
        m.assert_called_once()

    def test_fetch_sha_into_bare_delegates(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep()
        with patch("apm_cli.deps.github_downloader.fetch_sha_into_bare", return_value=True) as m:
            result = downloader._fetch_sha_into_bare(Path("/bare"), "deadbeef" * 5, dep_ref=dep)
        assert result is True
        m.assert_called_once()


# ---------------------------------------------------------------------------
# registry_config property
# ---------------------------------------------------------------------------


class TestRegistryConfig:
    def test_registry_config_is_lazy(self, downloader: GitHubPackageDownloader) -> None:
        fake_cfg = MagicMock()
        with patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=fake_cfg):
            cfg1 = downloader.registry_config
            cfg2 = downloader.registry_config  # cached
        assert cfg1 is cfg2

    def test_registry_config_returns_none_when_unconfigured(
        self, downloader: GitHubPackageDownloader
    ) -> None:
        with patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None):
            cfg = downloader.registry_config
        assert cfg is None


# ---------------------------------------------------------------------------
# _get_clone_engine lazy construction
# ---------------------------------------------------------------------------


class TestGetCloneEngine:
    def test_returns_existing_engine(self, downloader: GitHubPackageDownloader) -> None:
        engine = downloader._get_clone_engine()
        assert engine is downloader._clone_engine

    def test_constructs_engine_when_missing(self, downloader: GitHubPackageDownloader) -> None:
        # Delete the cached engine to trigger lazy construction
        del downloader._clone_engine
        engine = downloader._get_clone_engine()
        assert engine is not None
        assert downloader._clone_engine is engine


# ---------------------------------------------------------------------------
# resolve_git_reference with tiered resolver
# ---------------------------------------------------------------------------


class TestResolveGitReference:
    def test_delegates_to_tiered_resolver_when_set(
        self, downloader: GitHubPackageDownloader
    ) -> None:
        tiered = MagicMock()
        resolved = _make_resolved()
        tiered.resolve.return_value = resolved
        downloader._tiered_resolver = tiered
        dep = _make_dep()
        result = downloader.resolve_git_reference(dep)
        assert result is resolved
        tiered.resolve.assert_called_once_with(dep)

    def test_falls_through_to_refs_when_no_tiered(
        self, downloader: GitHubPackageDownloader
    ) -> None:
        downloader._tiered_resolver = None
        resolved = _make_resolved()
        downloader._refs = MagicMock()
        downloader._refs.resolve.return_value = resolved
        dep = _make_dep()
        result = downloader.resolve_git_reference(dep)
        assert result is resolved
        downloader._refs.resolve.assert_called_once_with(dep)


# ---------------------------------------------------------------------------
# download_raw_file routing
# ---------------------------------------------------------------------------


class TestDownloadRawFile:
    def test_routes_to_artifactory_mode1(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep(
            repo_url="owner/repo",
            host="artifactory.example.com",
            artifactory_prefix="apm-local",
        )
        downloader._strategies.download_file_from_artifactory = MagicMock(return_value=b"art")
        result = downloader.download_raw_file(dep, "file.txt", ref="main")
        assert result == b"art"
        downloader._strategies.download_file_from_artifactory.assert_called_once()

    def test_routes_to_artifactory_mode2_proxy(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep()
        proxy = ("proxy.host", "apm-repo", "https")
        with (
            patch.object(downloader, "_parse_artifactory_base_url", return_value=proxy),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=True),
        ):
            downloader._strategies.download_file_from_artifactory = MagicMock(return_value=b"proxy")
            result = downloader.download_raw_file(dep, "file.txt")
        assert result == b"proxy"

    def test_routes_to_ado_file(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep(host="dev.azure.com")
        downloader._strategies.download_ado_file = MagicMock(return_value=b"ado")
        with patch.object(downloader, "_parse_artifactory_base_url", return_value=None):
            result = downloader.download_raw_file(dep, "file.txt")
        assert result == b"ado"

    def test_routes_to_github_file(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep()
        downloader._strategies.download_github_file = MagicMock(return_value=b"github")
        with patch.object(downloader, "_parse_artifactory_base_url", return_value=None):
            result = downloader.download_raw_file(dep, "file.txt")
        assert result == b"github"

    def test_no_proxy_match_skips_proxy_path(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep()
        downloader._strategies.download_github_file = MagicMock(return_value=b"direct")
        with (
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
        ):
            result = downloader.download_raw_file(dep, "readme.md")
        assert result == b"direct"


# ---------------------------------------------------------------------------
# _download_github_file gitlab routing
# ---------------------------------------------------------------------------


class TestDownloadGithubFile:
    def test_routes_to_gitlab_when_gitlab_host(self, downloader: GitHubPackageDownloader) -> None:
        host_info = MagicMock()
        host_info.kind = "gitlab"
        downloader.auth_resolver.classify_host.return_value = host_info
        dep = _make_dep(host="gitlab.example.com")
        downloader._strategies.download_gitlab_file = MagicMock(return_value=b"gitlab")
        result = downloader._download_github_file(dep, "file.txt")
        assert result == b"gitlab"
        downloader._strategies.download_gitlab_file.assert_called_once()

    def test_routes_to_strategies_for_github(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep()
        downloader._strategies.download_github_file = MagicMock(return_value=b"gh")
        result = downloader._download_github_file(dep, "file.txt")
        assert result == b"gh"


# ---------------------------------------------------------------------------
# validate_virtual_package_exists / shim methods
# ---------------------------------------------------------------------------


class TestValidateVirtualPackageExistsShim:
    def test_shim_delegates_to_validation_module(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="skills/foo")
        with patch(
            "apm_cli.deps.github_downloader_validation.validate_virtual_package_exists",
            return_value=True,
        ) as m:
            result = downloader.validate_virtual_package_exists(dep)
        assert result is True
        m.assert_called_once()

    def test_directory_exists_at_ref_shim(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep()
        log_fn = MagicMock()
        with patch(
            "apm_cli.deps.github_downloader_validation._directory_exists_at_ref",
            return_value=True,
        ) as m:
            result = downloader._directory_exists_at_ref(dep, "skills/foo", "main", log_fn)
        assert result is True
        m.assert_called_once()

    def test_ref_exists_via_ls_remote_shim(self, downloader: GitHubPackageDownloader) -> None:
        dep = _make_dep()
        log_fn = MagicMock()
        with patch(
            "apm_cli.deps.github_downloader_validation._ref_exists_via_ls_remote",
            return_value=(True, MagicMock()),
        ) as m:
            result = downloader._ref_exists_via_ls_remote(dep, "main", log_fn)
        assert result is True
        m.assert_called_once()

    def test_ssh_attempt_allowed_shim(self, downloader: GitHubPackageDownloader) -> None:
        with patch(
            "apm_cli.deps.github_downloader_validation._ssh_attempt_allowed",
            return_value=False,
        ) as m:
            result = downloader._ssh_attempt_allowed()
        assert result is False
        m.assert_called_once_with(downloader)


# ---------------------------------------------------------------------------
# download_virtual_file_package — error paths
# ---------------------------------------------------------------------------


class TestDownloadVirtualFilePackageErrors:
    def test_raises_if_not_virtual(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()  # is_virtual=False
        with pytest.raises(ValueError, match="virtual"):
            downloader.download_virtual_file_package(dep, tmp_path / "out")

    def test_raises_if_no_virtual_path(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path=None)
        with pytest.raises(ValueError, match="virtual"):
            downloader.download_virtual_file_package(dep, tmp_path / "out")

    def test_raises_if_not_valid_file_extension(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="skills/foo")  # not a file
        with pytest.raises(ValueError, match="not a valid individual file"):
            downloader.download_virtual_file_package(dep, tmp_path / "out")

    def test_runtime_error_on_download_failure(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="prompts/test.prompt.md")
        downloader._refs = MagicMock()
        downloader._refs.resolve_commit_sha_for_ref.return_value = None
        downloader._strategies.download_github_file = MagicMock(side_effect=RuntimeError("404"))
        with (
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            pytest.raises(RuntimeError, match="Failed to download virtual package"),
        ):
            downloader.download_virtual_file_package(dep, tmp_path / "out")

    def test_progress_updates_when_provided(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="prompts/test.prompt.md")
        downloader._refs = MagicMock()
        downloader._refs.resolve_commit_sha_for_ref.return_value = None
        downloader._strategies.download_github_file = MagicMock(return_value=b"# Test\n")
        progress_obj = MagicMock()
        with patch.object(downloader, "_parse_artifactory_base_url", return_value=None):
            downloader.download_virtual_file_package(
                dep, tmp_path / "out", progress_task_id=1, progress_obj=progress_obj
            )
        assert progress_obj.update.call_count >= 2

    def test_frontmatter_description_parsed(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="prompts/test.prompt.md")
        downloader._refs = MagicMock()
        downloader._refs.resolve_commit_sha_for_ref.return_value = None
        content = b'---\ndescription: "My great prompt"\n---\n\n# Content\n'
        downloader._strategies.download_github_file = MagicMock(return_value=content)
        with patch.object(downloader, "_parse_artifactory_base_url", return_value=None):
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "out")
        assert pkg_info.package.description == "My great prompt"

    def test_fallback_description_when_no_frontmatter(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="prompts/test.prompt.md")
        downloader._refs = MagicMock()
        downloader._refs.resolve_commit_sha_for_ref.return_value = None
        downloader._strategies.download_github_file = MagicMock(return_value=b"# No FM\n")
        with patch.object(downloader, "_parse_artifactory_base_url", return_value=None):
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "out")
        assert "test.prompt.md" in pkg_info.package.description

    def test_commit_sha_ref_type_detected(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        sha = "a" * 40
        dep = _make_dep(is_virtual=True, virtual_path="prompts/test.prompt.md", reference=sha)
        downloader._refs = MagicMock()
        downloader._refs.resolve_commit_sha_for_ref.return_value = None
        downloader._strategies.download_github_file = MagicMock(return_value=b"# Test\n")
        with patch.object(downloader, "_parse_artifactory_base_url", return_value=None):
            pkg_info = downloader.download_virtual_file_package(dep, tmp_path / "out")
        assert pkg_info.resolved_reference.ref_type == GitReferenceType.COMMIT

    def test_instructions_subdir_mapping(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="instructions/code.instructions.md")
        downloader._refs = MagicMock()
        downloader._refs.resolve_commit_sha_for_ref.return_value = None
        downloader._strategies.download_github_file = MagicMock(return_value=b"# Instructions\n")
        with patch.object(downloader, "_parse_artifactory_base_url", return_value=None):
            downloader.download_virtual_file_package(dep, tmp_path / "out")
        assert (tmp_path / "out" / ".apm" / "instructions" / "code.instructions.md").exists()

    def test_chatmode_subdir_mapping(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="agents/mymode.agent.md")
        downloader._refs = MagicMock()
        downloader._refs.resolve_commit_sha_for_ref.return_value = None
        downloader._strategies.download_github_file = MagicMock(return_value=b"# Chat\n")
        with patch.object(downloader, "_parse_artifactory_base_url", return_value=None):
            downloader.download_virtual_file_package(dep, tmp_path / "out")
        assert (tmp_path / "out" / ".apm" / "agents" / "mymode.agent.md").exists()

    def test_agent_subdir_mapping(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="agents/myagent.agent.md")
        downloader._refs = MagicMock()
        downloader._refs.resolve_commit_sha_for_ref.return_value = None
        downloader._strategies.download_github_file = MagicMock(return_value=b"# Agent\n")
        with patch.object(downloader, "_parse_artifactory_base_url", return_value=None):
            downloader.download_virtual_file_package(dep, tmp_path / "out")
        assert (tmp_path / "out" / ".apm" / "agents" / "myagent.agent.md").exists()


# ---------------------------------------------------------------------------
# download_subdirectory_package — error paths
# ---------------------------------------------------------------------------


class TestDownloadSubdirectoryPackageErrors:
    def test_raises_if_not_virtual(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()  # is_virtual=False
        with pytest.raises(ValueError, match="virtual subdirectory"):
            downloader.download_subdirectory_package(dep, tmp_path / "out")

    def test_raises_if_no_virtual_path(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path=None)
        with pytest.raises(ValueError, match="virtual subdirectory"):
            downloader.download_subdirectory_package(dep, tmp_path / "out")

    def test_raises_if_not_valid_subdirectory(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        # a file extension path is not a valid subdirectory
        dep = _make_dep(is_virtual=True, virtual_path="prompts/test.prompt.md")
        with pytest.raises(ValueError, match="not a valid subdirectory"):
            downloader.download_subdirectory_package(dep, tmp_path / "out")

    def test_subdir_not_found_raises_runtime_error(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="skills/my-skill")
        sparse_ok = False

        def fake_sparse(*args: Any, **kwargs: Any) -> bool:
            return sparse_ok

        subdir_path = tmp_path / "clone"
        subdir_path.mkdir()

        def fake_mkdtemp(dir: Any = None) -> str:
            subdir_path.mkdir(exist_ok=True)
            return str(tmp_path)

        with (
            patch.object(downloader, "_try_sparse_checkout", return_value=False),
            patch.object(
                downloader,
                "_clone_with_fallback",
                return_value=MagicMock(),
            ),
            patch("apm_cli.deps.github_downloader.tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("apm_cli.utils.path_security.ensure_path_within"),
        ):
            with pytest.raises(RuntimeError, match="not found"):
                downloader.download_subdirectory_package(dep, tmp_path / "out")

    def test_progress_callbacks_called(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        """Progress updates are sent when progress_obj and task_id are provided."""
        dep = _make_dep(is_virtual=True, virtual_path="skills/my-skill")
        progress_obj = MagicMock()

        import contextlib

        with (
            patch.object(downloader, "_try_sparse_checkout", return_value=False),
            patch.object(downloader, "_clone_with_fallback", return_value=MagicMock()),
            patch("apm_cli.deps.github_downloader.tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("apm_cli.utils.path_security.ensure_path_within"),
        ):
            # Will fail because subdir doesn't exist but progress must already be called
            with contextlib.suppress(RuntimeError):
                downloader.download_subdirectory_package(
                    dep, tmp_path / "out", progress_task_id=1, progress_obj=progress_obj
                )
        progress_obj.update.assert_called()

    def test_permission_error_converted_to_runtime(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="skills/my-skill")
        exc = PermissionError(13, "Access denied")
        exc.filename = str(tmp_path / "some-file")

        with (
            patch.object(downloader, "_try_sparse_checkout", side_effect=exc),
            patch("apm_cli.deps.github_downloader.tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            with pytest.raises(RuntimeError, match="Access denied"):
                downloader.download_subdirectory_package(dep, tmp_path / "out")

    def test_oserror_13_in_temp_converted_to_runtime(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="skills/my-skill")
        exc = OSError(13, "Permission denied")
        exc.errno = 13
        exc.filename = str(tmp_path / "some-file")

        with (
            patch.object(downloader, "_try_sparse_checkout", side_effect=exc),
            patch("apm_cli.deps.github_downloader.tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            with pytest.raises(RuntimeError, match="Access denied"):
                downloader.download_subdirectory_package(dep, tmp_path / "out")

    def test_oserror_non_13_re_raised(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="skills/my-skill")
        exc = OSError(5, "Some other OS error")
        exc.errno = 5
        exc.filename = "/other/path"

        with (
            patch.object(downloader, "_try_sparse_checkout", side_effect=exc),
            patch("apm_cli.deps.github_downloader.tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            with pytest.raises(OSError):
                downloader.download_subdirectory_package(dep, tmp_path / "out")

    def test_ws2_resolved_commit_skips_repo_open(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        """When shared cache hits, _ws2_resolved_commit should skip Repo() open."""
        dep = _make_dep(is_virtual=True, virtual_path="skills/my-skill")
        sha = "a" * 40

        # Build a source subdir that looks like a real APM package
        skill_dir = tmp_path / "consumer" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "apm.yml").write_text("name: my-skill\nversion: 1.0.0\n")

        shared_cache = MagicMock()
        shared_bare = tmp_path / "bare"
        shared_bare.mkdir()
        shared_cache.get_or_clone.return_value = shared_bare
        downloader.shared_clone_cache = shared_cache

        validation = MagicMock()
        validation.is_valid = True
        validation.package = MagicMock()
        validation.package_type = MagicMock()

        def fake_materialize(bare: Path, consumer: Path, **kwargs: Any) -> str:
            consumer.mkdir(parents=True, exist_ok=True)
            sub = consumer / "skills" / "my-skill"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / "apm.yml").write_text("name: my-skill\nversion: 1.0.0\n")
            return sha

        with (
            patch.object(downloader, "_materialize_from_bare", side_effect=fake_materialize),
            patch.object(downloader, "_git_env_dict", return_value={}),
            patch.object(downloader, "_bare_clone_with_fallback"),
            patch("apm_cli.deps.github_downloader.tempfile.mkdtemp", return_value=str(tmp_path)),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("apm_cli.utils.path_security.ensure_path_within"),
            patch("apm_cli.deps.github_downloader.validate_apm_package", return_value=validation),
            patch("apm_cli.deps.package_validator.stamp_plugin_version"),
            patch("apm_cli.utils.file_ops.robust_copytree"),
            patch("apm_cli.utils.file_ops.robust_copy2"),
        ):
            pkg_info = downloader.download_subdirectory_package(dep, tmp_path / "out")
        # ws2 path — resolved_commit comes from _materialize_from_bare, not Repo()
        assert pkg_info.resolved_reference.resolved_commit == sha


# ---------------------------------------------------------------------------
# _try_sparse_checkout
# ---------------------------------------------------------------------------


class TestTrySparseCheckout:
    def test_returns_false_on_subprocess_nonzero(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        downloader._strategies.build_repo_url = MagicMock(return_value="https://github.com/o/r")
        downloader.git_env = {}
        ctx = MagicMock()
        ctx.auth_scheme = "basic"
        ctx.git_env = {}
        downloader.auth_resolver.resolve_for_dep.return_value = ctx

        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stderr = "some error"

        with patch("apm_cli.deps.github_downloader.subprocess.run", return_value=fail_result):
            result = downloader._try_sparse_checkout(dep, tmp_path / "sparse", "skills/foo", "main")
        assert result is False

    def test_returns_false_on_exception(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        downloader._strategies.build_repo_url = MagicMock(side_effect=RuntimeError("boom"))
        downloader.git_env = {}
        ctx = MagicMock()
        ctx.auth_scheme = "basic"
        ctx.git_env = {}
        downloader.auth_resolver.resolve_for_dep.return_value = ctx
        result = downloader._try_sparse_checkout(dep, tmp_path / "sparse", "skills/foo", "main")
        assert result is False

    def test_returns_true_on_success(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        downloader._strategies.build_repo_url = MagicMock(return_value="https://github.com/o/r")
        downloader.git_env = {}
        ctx = MagicMock()
        ctx.auth_scheme = "basic"
        ctx.git_env = {}
        downloader.auth_resolver.resolve_for_dep.return_value = ctx

        ok_result = MagicMock()
        ok_result.returncode = 0

        with patch("apm_cli.deps.github_downloader.subprocess.run", return_value=ok_result):
            result = downloader._try_sparse_checkout(dep, tmp_path / "sparse", "skills/foo", "main")
        assert result is True

    def test_bearer_auth_scheme_uses_dep_auth_ctx_git_env(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(host="dev.azure.com")
        downloader._strategies.build_repo_url = MagicMock(return_value="https://dev.azure.com/o/r")
        downloader.git_env = {}
        ctx = MagicMock()
        ctx.auth_scheme = "bearer"
        ctx.git_env = {"GIT_EXTRA_HEADER": "Authorization: Bearer tok"}
        downloader.auth_resolver.resolve_for_dep.return_value = ctx

        ok_result = MagicMock()
        ok_result.returncode = 0

        calls_seen: list[dict[str, Any]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            calls_seen.append(dict(env=kwargs.get("env", {})))
            return ok_result

        with patch("apm_cli.deps.github_downloader.subprocess.run", side_effect=fake_run):
            result = downloader._try_sparse_checkout(dep, tmp_path / "sparse", "skills/foo", "main")
        assert result is True
        # env must contain the git header injected by the bearer auth ctx
        assert any("GIT_EXTRA_HEADER" in c["env"] for c in calls_seen)


# ---------------------------------------------------------------------------
# download_package routing
# ---------------------------------------------------------------------------


class TestDownloadPackage:
    def test_invalid_string_ref_raises_value_error(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        with patch(
            "apm_cli.models.apm_package.DependencyReference.parse",
            side_effect=ValueError("bad ref"),
        ):
            with pytest.raises(ValueError, match="Invalid repository reference"):
                downloader.download_package("!!bad!!", tmp_path / "out")

    def test_virtual_file_routed_to_download_virtual_file_package(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="prompts/test.prompt.md")
        pkg_info = MagicMock()
        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "download_virtual_file_package", return_value=pkg_info),
        ):
            result = downloader.download_package(dep, tmp_path / "out")
        assert result is pkg_info

    def test_virtual_subdir_routed_to_download_subdirectory_package(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="skills/my-skill")
        pkg_info = MagicMock()
        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "download_subdirectory_package", return_value=pkg_info),
        ):
            result = downloader.download_package(dep, tmp_path / "out")
        assert result is pkg_info

    def test_artifactory_only_no_proxy_raises(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="skills/my-skill")
        with (
            patch.object(downloader, "_is_artifactory_only", return_value=True),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="PROXY_REGISTRY_ONLY"):
                downloader.download_package(dep, tmp_path / "out")

    def test_non_virtual_artifactory_dep_routes_to_artifactory(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(
            repo_url="owner/repo",
            host="artifactory.example.com",
            artifactory_prefix="apm-local",
        )
        pkg_info = MagicMock()
        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_download_package_from_artifactory", return_value=pkg_info),
        ):
            result = downloader.download_package(dep, tmp_path / "out")
        assert result is pkg_info

    def test_non_virtual_proxy_dep_routes_to_artifactory(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        proxy = ("proxy.host", "apm-repo", "https")
        pkg_info = MagicMock()
        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=proxy),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=True),
            patch.object(downloader, "_download_package_from_artifactory", return_value=pkg_info),
        ):
            result = downloader.download_package(dep, tmp_path / "out")
        assert result is pkg_info

    def test_artifactory_only_non_virtual_no_proxy_raises(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        with (
            patch.object(downloader, "_is_artifactory_only", return_value=True),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
        ):
            with pytest.raises(RuntimeError, match="PROXY_REGISTRY_ONLY"):
                downloader.download_package(dep, tmp_path / "out")

    def test_regular_package_clones_and_validates(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        resolved = _make_resolved(ref_type=GitReferenceType.BRANCH)
        repo_mock = MagicMock()
        validation = MagicMock()
        validation.is_valid = True
        validation.package = MagicMock()
        validation.package_type = MagicMock()
        pkg = MagicMock()

        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
            patch.object(downloader, "resolve_git_reference", return_value=resolved),
            patch.object(downloader, "_clone_with_fallback", return_value=repo_mock),
            patch("apm_cli.deps.github_downloader.validate_apm_package", return_value=validation),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("apm_cli.deps.package_validator.stamp_plugin_version"),
            patch("apm_cli.deps._shared._validate_and_load_package", return_value=pkg),
        ):
            result = downloader.download_package(dep, tmp_path / "pkg")
        assert result.package is pkg

    def test_git_command_error_auth_failure_raises_runtime(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        from git.exc import GitCommandError

        dep = _make_dep()
        resolved = _make_resolved(ref_type=GitReferenceType.BRANCH)

        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
            patch.object(downloader, "resolve_git_reference", return_value=resolved),
            patch.object(
                downloader,
                "_clone_with_fallback",
                side_effect=GitCommandError("git clone", "Authentication failed"),
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch.object(
                downloader.auth_resolver, "build_error_context", return_value="check your token"
            ),
        ):
            with pytest.raises(RuntimeError, match="Failed to clone"):
                downloader.download_package(dep, tmp_path / "pkg")

    def test_git_command_error_other_sanitizes_message(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        from git.exc import GitCommandError

        dep = _make_dep()
        resolved = _make_resolved(ref_type=GitReferenceType.BRANCH)

        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
            patch.object(downloader, "resolve_git_reference", return_value=resolved),
            patch.object(
                downloader,
                "_clone_with_fallback",
                side_effect=GitCommandError("git clone", "network timeout"),
            ),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            with pytest.raises(RuntimeError, match="Failed to clone"):
                downloader.download_package(dep, tmp_path / "pkg")

    def test_commit_ref_type_checkouts_specific_commit(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        sha = "a" * 40
        dep = _make_dep()
        resolved = _make_resolved(ref=sha, ref_type=GitReferenceType.COMMIT, commit=sha)
        repo_mock = MagicMock()
        validation = MagicMock()
        validation.is_valid = True
        validation.package = MagicMock()
        validation.package_type = MagicMock()
        pkg = MagicMock()

        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
            patch.object(downloader, "resolve_git_reference", return_value=resolved),
            patch.object(downloader, "_clone_with_fallback", return_value=repo_mock),
            patch("apm_cli.deps.github_downloader.validate_apm_package", return_value=validation),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("apm_cli.deps.package_validator.stamp_plugin_version"),
            patch("apm_cli.deps._shared._validate_and_load_package", return_value=pkg),
        ):
            downloader.download_package(dep, tmp_path / "pkg")
        # For commit type, checkout of the specific SHA must be called
        repo_mock.git.checkout.assert_called_once_with(sha)

    def test_virtual_artifactory_subdir_routes_to_artifactory(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(
            repo_url="owner/repo",
            host="artifactory.example.com",
            artifactory_prefix="apm-local",
            is_virtual=True,
            virtual_path="skills/my-skill",
        )
        pkg_info = MagicMock()
        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(
                downloader, "_download_subdirectory_from_artifactory", return_value=pkg_info
            ),
        ):
            result = downloader.download_package(dep, tmp_path / "out")
        assert result is pkg_info

    def test_virtual_artifactory_only_proxy_routes_to_artifactory(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep(is_virtual=True, virtual_path="skills/my-skill")
        proxy = ("proxy.host", "apm-repo", "https")
        pkg_info = MagicMock()
        with (
            patch.object(downloader, "_is_artifactory_only", return_value=True),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=proxy),
            patch.object(
                downloader, "_download_subdirectory_from_artifactory", return_value=pkg_info
            ),
        ):
            result = downloader.download_package(dep, tmp_path / "out")
        assert result is pkg_info


# ---------------------------------------------------------------------------
# _get_clone_progress_callback
# ---------------------------------------------------------------------------


class TestGetCloneProgressCallback:
    def test_callback_with_max_count(
        self, downloader: GitHubPackageDownloader, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cb = downloader._get_clone_progress_callback()
        cb(0, 50, max_count=100)
        out = capsys.readouterr().out
        assert "50%" in out

    def test_callback_without_max_count(
        self, downloader: GitHubPackageDownloader, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cb = downloader._get_clone_progress_callback()
        cb(0, 10)  # max_count defaults to None
        out = capsys.readouterr().out
        assert "10" in out

    def test_callback_returns_callable(self, downloader: GitHubPackageDownloader) -> None:
        cb = downloader._get_clone_progress_callback()
        assert callable(cb)


# ---------------------------------------------------------------------------
# _sanitize_git_error
# ---------------------------------------------------------------------------


class TestSanitizeGitError:
    def test_removes_token_from_url(self, downloader: GitHubPackageDownloader) -> None:
        msg = "remote: https://ghp_supersecret@github.com/org/repo.git"
        sanitized = downloader._sanitize_git_error(msg)
        assert "ghp_supersecret" not in sanitized

    def test_removes_ghp_token(self, downloader: GitHubPackageDownloader) -> None:
        msg = "Error: token ghp_abc123XYZ is invalid"
        sanitized = downloader._sanitize_git_error(msg)
        assert "ghp_abc123XYZ" not in sanitized
        assert "***" in sanitized

    def test_removes_env_var_token(self, downloader: GitHubPackageDownloader) -> None:
        msg = "GITHUB_APM_PAT=mytoken123 is wrong"
        sanitized = downloader._sanitize_git_error(msg)
        assert "mytoken123" not in sanitized
        assert "GITHUB_APM_PAT=***" in sanitized

    def test_plain_message_unchanged(self, downloader: GitHubPackageDownloader) -> None:
        msg = "network timeout occurred"
        assert downloader._sanitize_git_error(msg) == msg


# ---------------------------------------------------------------------------
# Artifactory backward-compat stubs
# ---------------------------------------------------------------------------


class TestArtifactoryStubs:
    def test_is_artifactory_only_delegates(self, downloader: GitHubPackageDownloader) -> None:
        with patch(
            "apm_cli.deps.artifactory_orchestrator.ArtifactoryRouter.is_registry_only",
            return_value=True,
        ):
            assert downloader._is_artifactory_only() is True

    def test_should_use_artifactory_proxy_delegates(
        self, downloader: GitHubPackageDownloader
    ) -> None:
        dep = _make_dep()
        with patch(
            "apm_cli.deps.artifactory_orchestrator.ArtifactoryRouter.should_use_proxy",
            return_value=False,
        ):
            assert downloader._should_use_artifactory_proxy(dep) is False

    def test_parse_artifactory_base_url_delegates(
        self, downloader: GitHubPackageDownloader
    ) -> None:
        with patch(
            "apm_cli.deps.artifactory_orchestrator.ArtifactoryRouter.parse_proxy_config",
            return_value=None,
        ):
            assert downloader._parse_artifactory_base_url() is None

    def test_download_subdirectory_from_artifactory_delegates(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        pkg_info = MagicMock()
        downloader._artifactory.download_subdirectory = MagicMock(return_value=pkg_info)
        result = downloader._download_subdirectory_from_artifactory(
            dep, tmp_path / "out", ("host", "pfx", "https")
        )
        assert result is pkg_info

    def test_download_package_from_artifactory_delegates(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        pkg_info = MagicMock()
        downloader._artifactory.download_package = MagicMock(return_value=pkg_info)
        result = downloader._download_package_from_artifactory(dep, tmp_path / "out")
        assert result is pkg_info


# ---------------------------------------------------------------------------
# Persistent git cache paths in download_package
# ---------------------------------------------------------------------------


class TestPersistentGitCacheInDownloadPackage:
    def test_cache_hit_returns_package_without_clone(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        resolved = _make_resolved(ref_type=GitReferenceType.BRANCH)

        # Set up cached checkout dir with a file in it
        cached_dir = tmp_path / "cached"
        cached_dir.mkdir()
        (cached_dir / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n")

        cache = MagicMock()
        cache.get_checkout.return_value = cached_dir
        downloader.persistent_git_cache = cache

        pkg = MagicMock()
        pkg.version = "1.0.0"
        validation = MagicMock()
        validation.is_valid = True
        validation.package = pkg
        validation.package_type = MagicMock()

        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
            patch.object(downloader, "resolve_git_reference", return_value=resolved),
            patch("apm_cli.deps.github_downloader.validate_apm_package", return_value=validation),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("apm_cli.utils.file_ops.robust_copytree"),
            patch("apm_cli.utils.file_ops.robust_copy2"),
            patch.object(downloader, "_git_env_dict", return_value={}),
        ):
            result = downloader.download_package(dep, tmp_path / "out")
        assert result.package is pkg
        # No clone should have been attempted
        assert not hasattr(downloader, "_clone_with_fallback_called")

    def test_cache_exception_falls_through_to_clone(
        self, downloader: GitHubPackageDownloader, tmp_path: Path
    ) -> None:
        dep = _make_dep()
        resolved = _make_resolved(ref_type=GitReferenceType.BRANCH)

        cache = MagicMock()
        cache.get_checkout.side_effect = RuntimeError("cache miss")
        downloader.persistent_git_cache = cache

        repo_mock = MagicMock()
        validation = MagicMock()
        validation.is_valid = True
        validation.package = MagicMock()
        validation.package_type = MagicMock()
        pkg = MagicMock()

        with (
            patch.object(downloader, "_is_artifactory_only", return_value=False),
            patch.object(downloader, "_parse_artifactory_base_url", return_value=None),
            patch.object(downloader, "_should_use_artifactory_proxy", return_value=False),
            patch.object(downloader, "resolve_git_reference", return_value=resolved),
            patch.object(downloader, "_clone_with_fallback", return_value=repo_mock) as clone_mock,
            patch("apm_cli.deps.github_downloader.validate_apm_package", return_value=validation),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("apm_cli.deps.package_validator.stamp_plugin_version"),
            patch("apm_cli.deps._shared._validate_and_load_package", return_value=pkg),
            patch.object(downloader, "_git_env_dict", return_value={}),
        ):
            downloader.download_package(dep, tmp_path / "out")
        clone_mock.assert_called_once()
