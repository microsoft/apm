"""Integration tests for four modules with large integration coverage gaps.

Modules covered
---------------
1. ``src/apm_cli/deps/github_downloader.py``   (54.4 %, gap = 339)
2. ``src/apm_cli/deps/apm_resolver.py``         (56.7 %, gap = 193)
3. ``src/apm_cli/core/script_runner.py``        (69.4 %, gap = 217)
4. ``src/apm_cli/deps/plugin_parser.py``        (58.6 %, gap = 202)

Strategy
--------
* Exercise real code paths; only mock HTTP, git, subprocess, and filesystem
  side-effects that would hit the network or modify the developer's machine.
* No live network calls.
* Type hints on every public function/method signature.
* URL assertions use ``urllib.parse.urlparse``, never substring matching.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_dep_ref(
    repo_url: str = "owner/repo",
    host: str | None = "github.com",
    port: int | None = None,
    reference: str | None = None,
    is_virtual: bool = False,
    virtual_path: str | None = None,
    is_local: bool = False,
    local_path: str | None = None,
    is_insecure: bool = False,
    ado_organization: str | None = None,
    ado_project: str | None = None,
    ado_repo: str | None = None,
    alias: str | None = None,
    is_parent_repo_inheritance: bool = False,
    explicit_scheme: str | None = None,
) -> Any:
    """Build a DependencyReference instance without network calls."""
    from apm_cli.models.dependency.reference import DependencyReference

    return DependencyReference(
        repo_url=repo_url,
        host=host,
        port=port,
        reference=reference,
        is_virtual=is_virtual,
        virtual_path=virtual_path,
        is_local=is_local,
        local_path=local_path,
        is_insecure=is_insecure,
        ado_organization=ado_organization,
        ado_project=ado_project,
        ado_repo=ado_repo,
        alias=alias,
        is_parent_repo_inheritance=is_parent_repo_inheritance,
        explicit_scheme=explicit_scheme,
    )


def _make_downloader_no_network() -> Any:
    """Build a GitHubPackageDownloader with all external I/O mocked out."""
    from apm_cli.deps.github_downloader import GitHubPackageDownloader

    with (
        patch("apm_cli.deps.github_downloader.AuthResolver") as mock_ar,
        patch("apm_cli.deps.github_downloader.TransportSelector"),
    ):
        mock_tm = MagicMock()
        mock_tm.get_token_for_purpose.return_value = None
        mock_ar.return_value._token_manager = mock_tm

        with patch(
            "apm_cli.deps.git_auth_env.GitAuthEnvBuilder.setup_environment",
            return_value={},
        ):
            dl = GitHubPackageDownloader()
    return dl


def _write_apm_yml(path: Path, content: dict[str, Any]) -> None:
    path.write_text(yaml.dump(content), encoding="utf-8")


# ============================================================================
# SECTION 1 - GitHubPackageDownloader: helper / utility methods
# ============================================================================


class TestGitHubDownloaderSanitizeGitError:
    """_sanitize_git_error removes tokens and credentials from messages."""

    def _get_downloader(self) -> Any:
        return _make_downloader_no_network()

    def test_sanitizes_github_token_in_url(self) -> None:
        dl = self._get_downloader()
        msg = "fatal: repository 'https://ghp_ABCDEFGH1234567890@github.com/org/repo.git' not found"
        sanitized = dl._sanitize_git_error(msg)
        assert "ghp_ABCDEFGH1234567890" not in sanitized

    def test_sanitizes_token_env_var_assignment(self) -> None:
        dl = self._get_downloader()
        msg = "GITHUB_TOKEN=my_secret_token authentication failed"
        sanitized = dl._sanitize_git_error(msg)
        assert "my_secret_token" not in sanitized
        assert "GITHUB_TOKEN=***" in sanitized

    def test_sanitizes_github_apm_pat(self) -> None:
        dl = self._get_downloader()
        msg = "GITHUB_APM_PAT=ghp_MY_PAT failed"
        sanitized = dl._sanitize_git_error(msg)
        assert "ghp_MY_PAT" not in sanitized

    def test_sanitizes_ado_pat(self) -> None:
        dl = self._get_downloader()
        msg = "ADO_APM_PAT=secret_ado_pat failed"
        sanitized = dl._sanitize_git_error(msg)
        assert "secret_ado_pat" not in sanitized

    def test_sanitizes_generic_https_token_at_host(self) -> None:
        dl = self._get_downloader()
        msg = "https://sometoken@company.corp/org/repo: error"
        sanitized = dl._sanitize_git_error(msg)
        assert "sometoken@" not in sanitized

    def test_passes_through_clean_messages(self) -> None:
        dl = self._get_downloader()
        msg = "fatal: not a git repository"
        sanitized = dl._sanitize_git_error(msg)
        assert sanitized == msg

    def test_sanitizes_glpat_token(self) -> None:
        dl = self._get_downloader()
        msg = "glpat-ABCDEF123456 authentication failed"
        sanitized = dl._sanitize_git_error(msg)
        assert "glpat-ABCDEF123456" not in sanitized

    def test_sanitizes_gitlab_token_env(self) -> None:
        dl = self._get_downloader()
        msg = "GITLAB_APM_PAT=mytoken error"
        sanitized = dl._sanitize_git_error(msg)
        assert "mytoken" not in sanitized


class TestGitHubDownloaderIsGenericDependencyHost:
    """_is_generic_dependency_host returns True for non-GitHub/non-GitLab/non-ADO hosts."""

    def _get_downloader(self) -> Any:
        return _make_downloader_no_network()

    def test_github_com_returns_false(self) -> None:
        dl = self._get_downloader()
        dep = _make_dep_ref(host="github.com")
        assert dl._is_generic_dependency_host(dep) is False

    def test_none_dep_ref_returns_false(self) -> None:
        dl = self._get_downloader()
        assert dl._is_generic_dependency_host(None) is False

    def test_azure_devops_returns_false(self) -> None:
        dl = self._get_downloader()
        dep = _make_dep_ref(
            host="dev.azure.com",
            repo_url="org/project/repo",
            ado_organization="org",
            ado_project="project",
            ado_repo="repo",
        )
        assert dl._is_generic_dependency_host(dep) is False

    def test_generic_bitbucket_returns_true(self) -> None:
        dl = self._get_downloader()
        dep = _make_dep_ref(host="bitbucket.mycompany.com")
        dl.auth_resolver.classify_host.return_value = MagicMock(kind="generic")
        assert dl._is_generic_dependency_host(dep) is True

    def test_gitlab_com_returns_false(self) -> None:
        dl = self._get_downloader()
        dep = _make_dep_ref(host="gitlab.com")
        dl.auth_resolver.classify_host.return_value = MagicMock(kind="gitlab")
        assert dl._is_generic_dependency_host(dep) is False


class TestGitHubDownloaderResolveDepToken:
    """_resolve_dep_token returns the correct token per host type."""

    def _get_downloader(self) -> Any:
        return _make_downloader_no_network()

    def test_returns_github_token_for_none_dep_ref(self) -> None:
        dl = self._get_downloader()
        dl.github_token = "github_tok"
        assert dl._resolve_dep_token(None) == "github_tok"

    def test_returns_none_for_generic_host(self) -> None:
        dl = self._get_downloader()
        dep = _make_dep_ref(host="bitbucket.corp.com")
        dl.auth_resolver.classify_host.return_value = MagicMock(kind="generic")
        result = dl._resolve_dep_token(dep)
        assert result is None

    def test_delegates_to_auth_resolver_for_github_dep(self) -> None:
        dl = self._get_downloader()
        dep = _make_dep_ref(host="github.com")
        mock_ctx = MagicMock()
        mock_ctx.token = "resolved_token"
        dl.auth_resolver.resolve_for_dep.return_value = mock_ctx
        dl.auth_resolver.classify_host.return_value = MagicMock(kind="github")
        result = dl._resolve_dep_token(dep)
        assert result == "resolved_token"


class TestGitHubDownloaderProgressReporter:
    """GitProgressReporter.update handles determinate and indeterminate progress."""

    def test_update_determinate_progress(self) -> None:
        from apm_cli.deps.github_downloader import GitProgressReporter

        mock_progress = MagicMock()
        reporter = GitProgressReporter(
            progress_task_id=1, progress_obj=mock_progress, package_name="test-pkg"
        )
        reporter.update(0, 50, max_count=100)
        mock_progress.update.assert_called_once_with(1, completed=50, total=100)

    def test_update_indeterminate_progress(self) -> None:
        from apm_cli.deps.github_downloader import GitProgressReporter

        mock_progress = MagicMock()
        reporter = GitProgressReporter(
            progress_task_id=2, progress_obj=mock_progress, package_name="test-pkg"
        )
        reporter.update(0, 30, max_count=None)
        mock_progress.update.assert_called_once_with(2, total=100, completed=30)

    def test_update_skipped_when_disabled(self) -> None:
        from apm_cli.deps.github_downloader import GitProgressReporter

        mock_progress = MagicMock()
        reporter = GitProgressReporter(
            progress_task_id=3, progress_obj=mock_progress, package_name="test-pkg"
        )
        reporter.disabled = True
        reporter.update(0, 10, max_count=100)
        mock_progress.update.assert_not_called()

    def test_update_skipped_when_no_progress_obj(self) -> None:
        from apm_cli.deps.github_downloader import GitProgressReporter

        reporter = GitProgressReporter(progress_task_id=None, progress_obj=None)
        # Should not raise even with no progress object
        reporter.update(0, 10, max_count=100)

    def test_get_op_name_counting(self) -> None:
        from git import RemoteProgress

        from apm_cli.deps.github_downloader import GitProgressReporter

        reporter = GitProgressReporter()
        name = reporter._get_op_name(RemoteProgress.COUNTING)
        assert "Counting" in name

    def test_get_op_name_receiving(self) -> None:
        from git import RemoteProgress

        from apm_cli.deps.github_downloader import GitProgressReporter

        reporter = GitProgressReporter()
        name = reporter._get_op_name(RemoteProgress.RECEIVING)
        assert "Receiving" in name

    def test_get_op_name_unknown_falls_back(self) -> None:
        from apm_cli.deps.github_downloader import GitProgressReporter

        reporter = GitProgressReporter()
        name = reporter._get_op_name(0)
        assert name == "Cloning"


class TestGitHubDownloaderDebugHelper:
    """_debug helper emits only when APM_DEBUG is set."""

    def test_debug_prints_when_env_set(self, capsys) -> None:
        from apm_cli.deps.github_downloader import _debug

        with patch.dict(os.environ, {"APM_DEBUG": "1"}):
            _debug("hello world")
        captured = capsys.readouterr()
        assert "hello world" in captured.err

    def test_debug_silent_by_default(self, capsys) -> None:
        from apm_cli.deps.github_downloader import _debug

        env = {k: v for k, v in os.environ.items() if k != "APM_DEBUG"}
        with patch.dict(os.environ, env, clear=True):
            _debug("should not appear")
        captured = capsys.readouterr()
        assert "should not appear" not in captured.err


class TestGitHubDownloaderCloseRepo:
    """_close_repo handles None and exception gracefully."""

    def test_close_repo_with_none(self) -> None:
        from apm_cli.deps.github_downloader import _close_repo

        _close_repo(None)  # must not raise

    def test_close_repo_clears_cache(self) -> None:
        from apm_cli.deps.github_downloader import _close_repo

        mock_repo = MagicMock()
        _close_repo(mock_repo)
        mock_repo.git.clear_cache.assert_called_once()
        mock_repo.close.assert_called_once()

    def test_close_repo_tolerates_exception(self) -> None:
        from apm_cli.deps.github_downloader import _close_repo

        mock_repo = MagicMock()
        mock_repo.git.clear_cache.side_effect = RuntimeError("locked")
        _close_repo(mock_repo)  # must not propagate


class TestGitHubDownloaderDownloadVirtualFilePackage:
    """download_virtual_file_package creates the correct .apm directory structure."""

    def test_raises_when_not_virtual(self, tmp_path: Path) -> None:
        dl = _make_downloader_no_network()
        dep = _make_dep_ref(is_virtual=False)
        with pytest.raises(ValueError, match="virtual file package"):
            dl.download_virtual_file_package(dep, tmp_path / "target")

    def test_raises_when_not_virtual_file(self, tmp_path: Path) -> None:
        dl = _make_downloader_no_network()
        dep = _make_dep_ref(is_virtual=True, virtual_path="skills/my-skill")
        with pytest.raises(ValueError):
            dl.download_virtual_file_package(dep, tmp_path / "target")

    def test_creates_prompt_md_structure(self, tmp_path: Path) -> None:
        dl = _make_downloader_no_network()
        dep = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/test.prompt.md",
            reference="main",
        )
        target = tmp_path / "target"

        with (
            patch.object(dl, "_resolve_commit_sha_for_ref", return_value="abc123"),
            patch.object(
                dl,
                "download_raw_file",
                return_value=b"# Test prompt\nHello world",
            ),
        ):
            info = dl.download_virtual_file_package(dep, target)

        assert (target / ".apm" / "prompts" / "test.prompt.md").exists()
        assert (target / "apm.yml").exists()
        assert info.package.name is not None

    def test_creates_agent_md_structure(self, tmp_path: Path) -> None:
        dl = _make_downloader_no_network()
        dep = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="agents/helper.agent.md",
            reference="v1.0",
        )
        target = tmp_path / "target"

        with (
            patch.object(dl, "_resolve_commit_sha_for_ref", return_value=None),
            patch.object(dl, "download_raw_file", return_value=b"# Agent"),
        ):
            dl.download_virtual_file_package(dep, target)

        assert (target / ".apm" / "agents" / "helper.agent.md").exists()

    def test_extracts_description_from_frontmatter(self, tmp_path: Path) -> None:
        dl = _make_downloader_no_network()
        dep = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/code.prompt.md",
        )
        target = tmp_path / "target"
        content = b"---\ndescription: My awesome prompt\n---\n\nContent here"

        with (
            patch.object(dl, "_resolve_commit_sha_for_ref", return_value=None),
            patch.object(dl, "download_raw_file", return_value=content),
        ):
            info = dl.download_virtual_file_package(dep, target)

        assert "My awesome prompt" in info.package.description

    def test_updates_progress_if_provided(self, tmp_path: Path) -> None:
        dl = _make_downloader_no_network()
        dep = _make_dep_ref(
            repo_url="owner/repo",
            is_virtual=True,
            virtual_path="prompts/code.prompt.md",
        )
        target = tmp_path / "target"
        mock_progress = MagicMock()

        with (
            patch.object(dl, "_resolve_commit_sha_for_ref", return_value=None),
            patch.object(dl, "download_raw_file", return_value=b"content"),
        ):
            dl.download_virtual_file_package(
                dep, target, progress_task_id=1, progress_obj=mock_progress
            )

        assert mock_progress.update.called


class TestGitHubDownloaderRegistryConfig:
    """registry_config property is lazily constructed."""

    def test_registry_config_cached_on_second_access(self) -> None:
        dl = _make_downloader_no_network()
        with patch("apm_cli.deps.registry_proxy.RegistryConfig.from_env", return_value=None) as m:
            _ = dl.registry_config
            _ = dl.registry_config
        # from_env must be called exactly once (lazy + cached)
        m.assert_called_once()


class TestGitHubDownloaderTrySparseCheckout:
    """_try_sparse_checkout returns False when git steps fail."""

    def test_returns_false_on_subprocess_failure(self, tmp_path: Path) -> None:
        dl = _make_downloader_no_network()
        dep = _make_dep_ref(repo_url="owner/repo")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error")
            result = dl._try_sparse_checkout(dep, tmp_path / "sparse", "subdir", ref="main")
        assert result is False

    def test_returns_false_on_exception(self, tmp_path: Path) -> None:
        dl = _make_downloader_no_network()
        dep = _make_dep_ref(repo_url="owner/repo")
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = dl._try_sparse_checkout(dep, tmp_path / "sparse", "subdir", ref="main")
        assert result is False


# ============================================================================
# SECTION 2 - APMDependencyResolver
# ============================================================================


class TestResolverMaxParallel:
    """_resolve_max_parallel picks up env var and explicit values correctly."""

    def test_explicit_value_wins(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        assert APMDependencyResolver._resolve_max_parallel(7) == 7

    def test_explicit_value_clamped_to_one(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        assert APMDependencyResolver._resolve_max_parallel(0) == 1

    def test_env_var_sets_value(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        with patch.dict(os.environ, {"APM_RESOLVE_PARALLEL": "3"}):
            assert APMDependencyResolver._resolve_max_parallel(None) == 3

    def test_invalid_env_var_falls_back_to_default(self) -> None:
        from apm_cli.deps.apm_resolver import _DEFAULT_RESOLVE_PARALLEL, APMDependencyResolver

        with patch.dict(os.environ, {"APM_RESOLVE_PARALLEL": "not_a_number"}):
            result = APMDependencyResolver._resolve_max_parallel(None)
        assert result == _DEFAULT_RESOLVE_PARALLEL

    def test_no_env_var_uses_default(self) -> None:
        from apm_cli.deps.apm_resolver import _DEFAULT_RESOLVE_PARALLEL, APMDependencyResolver

        env = {k: v for k, v in os.environ.items() if k != "APM_RESOLVE_PARALLEL"}
        with patch.dict(os.environ, env, clear=True):
            result = APMDependencyResolver._resolve_max_parallel(None)
        assert result == _DEFAULT_RESOLVE_PARALLEL


class TestSignatureAcceptsParentPkg:
    """_signature_accepts_parent_pkg inspects callback signatures correctly."""

    def test_accepts_parent_pkg_parameter(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        def cb(dep_ref, apm_modules_dir, parent_chain="", parent_pkg=None):
            pass

        assert APMDependencyResolver._signature_accepts_parent_pkg(cb) is True

    def test_legacy_callback_without_parent_pkg(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        def legacy_cb(dep_ref, apm_modules_dir, parent_chain=""):
            pass

        assert APMDependencyResolver._signature_accepts_parent_pkg(legacy_cb) is False

    def test_kwargs_callback_accepted(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        def kwargs_cb(dep_ref, apm_modules_dir, **kwargs):
            pass

        assert APMDependencyResolver._signature_accepts_parent_pkg(kwargs_cb) is True

    def test_uninspectable_callback_returns_false(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        # Built-in functions can't always be introspected
        assert APMDependencyResolver._signature_accepts_parent_pkg(len) is False


class TestResolverIsRemoteParent:
    """_is_remote_parent classifies parent packages correctly."""

    def test_none_parent_is_not_remote(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        assert APMDependencyResolver._is_remote_parent(None) is False

    def test_local_prefix_is_not_remote(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage(name="local-pkg", version="1.0.0", source="_local/local-pkg")
        assert APMDependencyResolver._is_remote_parent(pkg) is False

    def test_https_url_source_is_remote(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage(name="remote-pkg", version="1.0.0", source="https://github.com/owner/repo")
        assert APMDependencyResolver._is_remote_parent(pkg) is True

    def test_owner_slash_repo_source_is_remote(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage(name="remote-pkg", version="1.0.0", source="owner/repo")
        assert APMDependencyResolver._is_remote_parent(pkg) is True

    def test_git_at_source_is_remote(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage(name="remote", version="1.0.0", source="git@github.com:owner/repo.git")
        assert APMDependencyResolver._is_remote_parent(pkg) is True

    def test_dot_slash_source_is_not_remote(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage(name="local", version="1.0.0", source="./path/to/pkg")
        assert APMDependencyResolver._is_remote_parent(pkg) is False


class TestResolverComputeDepSourcePath:
    """_compute_dep_source_path resolves paths correctly."""

    def test_remote_dep_returns_install_path(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        dep = _make_dep_ref(repo_url="owner/repo", is_local=False)
        install = tmp_path / "apm_modules" / "owner" / "repo"
        install.mkdir(parents=True)
        result = APMDependencyResolver._compute_dep_source_path(dep, None, install)
        assert result == install.resolve()

    def test_local_dep_absolute_returns_absolute(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        local_dir = tmp_path / "mylib"
        local_dir.mkdir()
        dep = _make_dep_ref(is_local=True, local_path=str(local_dir), repo_url="_local/mylib")
        install = tmp_path / "apm_modules" / "something"
        result = APMDependencyResolver._compute_dep_source_path(dep, None, install)
        assert result == local_dir.resolve()


class TestResolverDownloadDedupKey:
    """_download_dedup_key builds unique keys per (dep, parent) pair."""

    def test_non_local_dep_returns_unique_key(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        dep = _make_dep_ref(repo_url="owner/repo")
        key = APMDependencyResolver._download_dedup_key(dep, None)
        assert key == dep.get_unique_key()

    def test_local_dep_with_parent_includes_source_path(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import APMPackage

        dep = _make_dep_ref(is_local=True, local_path="./mylib", repo_url="_local/mylib")
        parent = APMPackage(name="parent", version="1.0.0", source_path=tmp_path)
        key = APMDependencyResolver._download_dedup_key(dep, parent)
        assert str(tmp_path) in key

    def test_local_dep_without_parent_returns_base_key(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        dep = _make_dep_ref(is_local=True, local_path="./mylib", repo_url="_local/mylib")
        key = APMDependencyResolver._download_dedup_key(dep, None)
        assert key == dep.get_unique_key()


class TestResolverEffectiveBaseDir:
    """_effective_base_dir returns the right anchor directory."""

    def test_no_parent_uses_project_root(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        result = APMDependencyResolver._effective_base_dir(None, tmp_path)
        assert result == tmp_path

    def test_parent_with_source_path_returns_source_path(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import APMPackage

        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()
        parent = APMPackage(name="p", version="1.0.0", source_path=parent_dir)
        result = APMDependencyResolver._effective_base_dir(parent, tmp_path)
        assert result == parent_dir


class TestResolverValidateDependencyReference:
    """_validate_dependency_reference detects malformed references."""

    def test_valid_reference_returns_true(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        resolver = APMDependencyResolver()
        dep = _make_dep_ref(repo_url="owner/repo")
        assert resolver._validate_dependency_reference(dep) is True

    def test_missing_repo_url_returns_false(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        resolver = APMDependencyResolver()
        dep = _make_dep_ref(repo_url="")
        assert resolver._validate_dependency_reference(dep) is False

    def test_repo_url_without_slash_returns_false(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        resolver = APMDependencyResolver()
        dep = _make_dep_ref(repo_url="nodash")
        assert resolver._validate_dependency_reference(dep) is False


class TestResolverResolveDependenciesNoApmYml:
    """resolve_dependencies handles projects with no apm.yml."""

    def test_empty_graph_returned_for_missing_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        resolver = APMDependencyResolver()
        graph = resolver.resolve_dependencies(tmp_path)
        assert graph is not None
        assert graph.root_package.name == "unknown"

    def test_error_graph_returned_for_invalid_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        (tmp_path / "apm.yml").write_text("invalid: [yaml: content", encoding="utf-8")
        resolver = APMDependencyResolver()
        graph = resolver.resolve_dependencies(tmp_path)
        # Should return some graph without crashing
        assert graph is not None


class TestResolverBuildDependencyTree:
    """build_dependency_tree constructs the tree for simple packages."""

    def test_tree_has_root_node(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        _write_apm_yml(
            tmp_path / "apm.yml",
            {"name": "test-pkg", "version": "1.0.0"},
        )
        resolver = APMDependencyResolver()
        resolver._project_root = tmp_path
        resolver._apm_modules_dir = tmp_path / "apm_modules"
        tree = resolver.build_dependency_tree(tmp_path / "apm.yml")
        assert tree.root_package.name == "test-pkg"

    def test_tree_with_one_dep(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        _write_apm_yml(
            tmp_path / "apm.yml",
            {
                "name": "my-pkg",
                "version": "1.0.0",
                "dependencies": {"apm": ["dep-owner/dep-repo"]},
            },
        )
        resolver = APMDependencyResolver()
        resolver._project_root = tmp_path
        resolver._apm_modules_dir = tmp_path / "apm_modules"
        tree = resolver.build_dependency_tree(tmp_path / "apm.yml")
        assert tree.root_package.name == "my-pkg"

    def test_tree_raises_for_parent_repo_in_root(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        _write_apm_yml(
            tmp_path / "apm.yml",
            {
                "name": "bad-pkg",
                "version": "1.0.0",
                "dependencies": {"apm": [{"git": "parent", "path": "sub/dir"}]},
            },
        )
        resolver = APMDependencyResolver()
        resolver._project_root = tmp_path
        resolver._apm_modules_dir = tmp_path / "apm_modules"
        with pytest.raises(ValueError, match="git: parent cannot be used in the root"):
            resolver.build_dependency_tree(tmp_path / "apm.yml")


class TestResolverDetectCircularDependencies:
    """detect_circular_dependencies finds cycles in dependency trees."""

    def test_no_circular_in_simple_tree(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        _write_apm_yml(tmp_path / "apm.yml", {"name": "p", "version": "1.0.0"})
        resolver = APMDependencyResolver()
        resolver._project_root = tmp_path
        resolver._apm_modules_dir = tmp_path / "apm_modules"
        tree = resolver.build_dependency_tree(tmp_path / "apm.yml")
        circulars = resolver.detect_circular_dependencies(tree)
        assert circulars == []


class TestResolverFlattenDependencies:
    """flatten_dependencies deduplicates a dependency tree correctly."""

    def test_flat_map_for_no_deps(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        _write_apm_yml(tmp_path / "apm.yml", {"name": "p", "version": "1.0.0"})
        resolver = APMDependencyResolver()
        resolver._project_root = tmp_path
        resolver._apm_modules_dir = tmp_path / "apm_modules"
        tree = resolver.build_dependency_tree(tmp_path / "apm.yml")
        flat = resolver.flatten_dependencies(tree)
        assert flat is not None


class TestResolverExpandParentRepoDeclExpansion:
    """expand_parent_repo_decl merges parent repo coordinates into a child dep."""

    def test_expands_child_with_parent_repo(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.dependency.reference import DependencyReference

        resolver = APMDependencyResolver()
        parent = DependencyReference.parse("org/monorepo#main")
        child = DependencyReference(
            repo_url="",
            is_parent_repo_inheritance=True,
            virtual_path="services/svc-a",
            is_virtual=True,
        )
        expanded = resolver.expand_parent_repo_decl(parent, child)
        assert expanded.repo_url == "org/monorepo"
        assert expanded.reference == "main"
        assert not expanded.is_parent_repo_inheritance

    def test_raises_for_local_parent(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.dependency.reference import DependencyReference

        resolver = APMDependencyResolver()
        local_parent = DependencyReference(repo_url="_local/some", is_local=True)
        child = DependencyReference(repo_url="", is_parent_repo_inheritance=True)
        with pytest.raises(ValueError, match="local path"):
            resolver.expand_parent_repo_decl(local_parent, child)

    def test_raises_when_child_not_flagged(self) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.dependency.reference import DependencyReference

        resolver = APMDependencyResolver()
        parent = DependencyReference.parse("org/monorepo")
        child = DependencyReference.parse("org/other")  # not flagged
        with pytest.raises(ValueError, match="is_parent_repo_inheritance"):
            resolver.expand_parent_repo_decl(parent, child)


class TestResolverCreateResolutionSummary:
    """_create_resolution_summary formats graph info into a readable string."""

    def test_summary_contains_root_package_name(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        _write_apm_yml(tmp_path / "apm.yml", {"name": "my-root", "version": "1.0.0"})
        resolver = APMDependencyResolver()
        graph = resolver.resolve_dependencies(tmp_path)
        summary = resolver._create_resolution_summary(graph)
        assert "my-root" in summary

    def test_summary_contains_dependency_count(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        _write_apm_yml(tmp_path / "apm.yml", {"name": "p", "version": "1.0.0"})
        resolver = APMDependencyResolver()
        graph = resolver.resolve_dependencies(tmp_path)
        summary = resolver._create_resolution_summary(graph)
        assert "Total dependencies" in summary


class TestResolverTryLoadDependencyPackageWithCallback:
    """_try_load_dependency_package invokes the download callback correctly."""

    def test_callback_invoked_for_missing_package(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        called_with: list[Any] = []

        def mock_callback(dep_ref, apm_modules_dir, parent_chain="", parent_pkg=None):
            called_with.append(dep_ref)

        resolver = APMDependencyResolver(
            apm_modules_dir=tmp_path / "apm_modules",
            download_callback=mock_callback,
        )
        dep = _make_dep_ref(repo_url="owner/missing-repo")
        result = resolver._try_load_dependency_package(dep)
        assert result is None
        assert len(called_with) == 1

    def test_callback_not_invoked_for_already_installed(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        apm_modules = tmp_path / "apm_modules"
        install_path = apm_modules / "owner" / "installed-repo"
        install_path.mkdir(parents=True)
        _write_apm_yml(install_path / "apm.yml", {"name": "installed-repo", "version": "1.0.0"})

        callback_calls: list[Any] = []

        def mock_callback(dep_ref, apm_modules_dir, parent_chain="", parent_pkg=None):
            callback_calls.append(dep_ref)

        resolver = APMDependencyResolver(
            apm_modules_dir=apm_modules,
            download_callback=mock_callback,
        )
        dep = _make_dep_ref(repo_url="owner/installed-repo")
        result = resolver._try_load_dependency_package(dep)
        # Callback not called since package is already present
        assert len(callback_calls) == 0
        assert result is not None

    def test_skill_md_without_apm_yml_returns_package(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver

        apm_modules = tmp_path / "apm_modules"
        install_path = apm_modules / "owner" / "skill-pkg"
        install_path.mkdir(parents=True)
        (install_path / "SKILL.md").write_text("# Skill", encoding="utf-8")

        resolver = APMDependencyResolver(apm_modules_dir=apm_modules)
        dep = _make_dep_ref(repo_url="owner/skill-pkg")
        result = resolver._try_load_dependency_package(dep)
        assert result is not None
        assert result.name == dep.get_display_name()

    def test_remote_parent_rejects_local_path_dep(self, tmp_path: Path) -> None:
        from apm_cli.deps.apm_resolver import APMDependencyResolver
        from apm_cli.models.apm_package import APMPackage

        resolver = APMDependencyResolver(apm_modules_dir=tmp_path / "apm_modules")
        remote_parent = APMPackage(
            name="remote-pkg", version="1.0.0", source="https://github.com/owner/repo"
        )
        local_dep = _make_dep_ref(
            is_local=True, local_path="./sensitive", repo_url="_local/sensitive"
        )

        result = resolver._try_load_dependency_package(local_dep, parent_pkg=remote_parent)
        assert result is None
        # Rejection must be recorded so integrate phase can skip it
        assert local_dep.get_unique_key() in resolver._rejected_remote_local_keys


# ============================================================================
# SECTION 3 - ScriptRunner
# ============================================================================


def _make_script_runner(use_color: bool = False) -> Any:
    """Build a ScriptRunner with a mock PromptCompiler."""
    from apm_cli.core.script_runner import ScriptRunner

    runner = ScriptRunner(use_color=use_color)
    runner.compiler = MagicMock()
    return runner


class TestScriptRunnerDetectRuntime:
    """_detect_runtime identifies runtimes from command strings."""

    def test_detects_copilot(self) -> None:
        runner = _make_script_runner()
        assert runner._detect_runtime("copilot -p file.prompt.md") == "copilot"

    def test_detects_codex(self) -> None:
        runner = _make_script_runner()
        assert runner._detect_runtime("codex exec prompt") == "codex"

    def test_detects_llm(self) -> None:
        runner = _make_script_runner()
        assert runner._detect_runtime("llm 'do something'") == "llm"

    def test_detects_gemini(self) -> None:
        runner = _make_script_runner()
        assert runner._detect_runtime("gemini -p file.prompt.md") == "gemini"

    def test_unknown_falls_back(self) -> None:
        runner = _make_script_runner()
        assert runner._detect_runtime("python script.py") == "unknown"

    def test_case_insensitive(self) -> None:
        runner = _make_script_runner()
        assert runner._detect_runtime("COPILOT run") == "copilot"


class TestScriptRunnerBuildCommandBuilders:
    """_build_*_command methods produce correctly formatted commands."""

    def test_build_codex_no_args(self) -> None:
        runner = _make_script_runner()
        result = runner._build_codex_command("", "", None)
        assert result == "codex exec"

    def test_build_codex_with_args_before(self) -> None:
        runner = _make_script_runner()
        result = runner._build_codex_command("-s workspace", "", None)
        assert result == "codex exec -s workspace"

    def test_build_codex_with_env_prefix(self) -> None:
        runner = _make_script_runner()
        result = runner._build_codex_command("", "", "DEBUG=1")
        assert result == "DEBUG=1 codex exec"

    def test_build_copilot_strips_p_flag(self) -> None:
        runner = _make_script_runner()
        result = runner._build_copilot_command("-p --log-level all", "", None)
        # -p should be removed; --log-level all preserved
        assert "-p" not in result
        assert "--log-level all" in result

    def test_build_llm_with_args(self) -> None:
        runner = _make_script_runner()
        result = runner._build_llm_command("--model gpt-4", "", None)
        assert "llm" in result
        assert "--model gpt-4" in result

    def test_build_gemini_strips_p_flag(self) -> None:
        runner = _make_script_runner()
        result = runner._build_gemini_command("-p", "", None)
        assert result.strip() == "gemini"

    def test_build_gemini_with_env_prefix(self) -> None:
        runner = _make_script_runner()
        result = runner._build_gemini_command("", "", "ENV=val")
        assert result.startswith("ENV=val")


class TestScriptRunnerTransformRuntimeCommand:
    """_transform_runtime_command maps prompt references to runtime invocations."""

    def test_bare_prompt_file_becomes_codex_exec(self) -> None:
        runner = _make_script_runner()
        result = runner._transform_runtime_command(
            "code.prompt.md", "code.prompt.md", "compiled content", "code.txt"
        )
        assert result == "codex exec"

    def test_copilot_command_transformed(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        cmd = "copilot code.prompt.md"
        result = runner._transform_runtime_command(cmd, "code.prompt.md", "content", "code.txt")
        assert "copilot" in result

    def test_fallback_replaces_path(self) -> None:
        runner = _make_script_runner()
        cmd = "cat code.prompt.md"
        result = runner._transform_runtime_command(
            cmd, "code.prompt.md", "content", "/tmp/code.txt"
        )
        assert "code.prompt.md" not in result
        assert "/tmp/code.txt" in result


class TestScriptRunnerGenerateRuntimeCommand:
    """_generate_runtime_command produces runtime-specific invocations."""

    def test_copilot_command_format(self) -> None:
        runner = _make_script_runner()
        cmd = runner._generate_runtime_command("copilot", Path("test.prompt.md"))
        assert "copilot" in cmd
        assert "test.prompt.md" in cmd

    def test_codex_command_format(self) -> None:
        runner = _make_script_runner()
        cmd = runner._generate_runtime_command("codex", Path("test.prompt.md"))
        assert "codex" in cmd
        assert "test.prompt.md" in cmd

    def test_gemini_command_format(self) -> None:
        runner = _make_script_runner()
        cmd = runner._generate_runtime_command("gemini", Path("test.prompt.md"))
        assert "gemini" in cmd

    def test_unsupported_runtime_raises(self) -> None:
        runner = _make_script_runner()
        with pytest.raises(ValueError, match="Unsupported runtime"):
            runner._generate_runtime_command("unknown_rt", Path("test.prompt.md"))


class TestScriptRunnerListScripts:
    """list_scripts returns scripts dict from apm.yml."""

    def test_lists_scripts_from_apm_yml(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        apm_yml = tmp_path / "apm.yml"
        _write_apm_yml(
            apm_yml,
            {
                "name": "test-project",
                "version": "1.0.0",
                "scripts": {"build": "echo build", "test": "echo test"},
            },
        )
        os.chdir(tmp_path)
        result = runner.list_scripts()
        assert "build" in result
        assert "test" in result

    def test_returns_empty_dict_when_no_apm_yml(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        os.chdir(tmp_path)
        result = runner.list_scripts()
        assert result == {}


class TestScriptRunnerDiscoverPromptFile:
    """_discover_prompt_file searches local directories and dependencies."""

    def test_finds_local_prompt_at_root(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        prompt = tmp_path / "my-script.prompt.md"
        prompt.write_text("# My Script", encoding="utf-8")
        os.chdir(tmp_path)
        result = runner._discover_prompt_file("my-script")
        assert result == Path("my-script.prompt.md")

    def test_finds_prompt_in_apm_prompts_dir(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        prompts_dir = tmp_path / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "my-script.prompt.md").write_text("content", encoding="utf-8")
        os.chdir(tmp_path)
        result = runner._discover_prompt_file("my-script")
        assert result is not None

    def test_finds_prompt_in_github_prompts_dir(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "gh-script.prompt.md").write_text("content", encoding="utf-8")
        os.chdir(tmp_path)
        result = runner._discover_prompt_file("gh-script")
        assert result is not None

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        os.chdir(tmp_path)
        result = runner._discover_prompt_file("nonexistent-script")
        assert result is None

    def test_finds_prompt_in_apm_modules(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        pkg_prompts = tmp_path / "apm_modules" / "org" / "pkg" / ".apm" / "prompts"
        pkg_prompts.mkdir(parents=True)
        (pkg_prompts / "remote-prompt.prompt.md").write_text("content", encoding="utf-8")
        os.chdir(tmp_path)
        result = runner._discover_prompt_file("remote-prompt")
        assert result is not None

    def test_collision_raises_runtime_error(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        # Create two matching prompt files in different packages
        for pkg in ["pkg-a", "pkg-b"]:
            pkg_prompts = tmp_path / "apm_modules" / "org" / pkg / ".apm" / "prompts"
            pkg_prompts.mkdir(parents=True)
            (pkg_prompts / "same-name.prompt.md").write_text("content", encoding="utf-8")
        os.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="Multiple prompts"):
            runner._discover_prompt_file("same-name")


class TestScriptRunnerDiscoverQualifiedPrompt:
    """_discover_qualified_prompt handles owner/repo/name paths."""

    def test_finds_qualified_prompt(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        pkg_prompts = tmp_path / "apm_modules" / "myorg" / "mypkg" / ".apm" / "prompts"
        pkg_prompts.mkdir(parents=True)
        (pkg_prompts / "myScript.prompt.md").write_text("content", encoding="utf-8")
        os.chdir(tmp_path)
        result = runner._discover_qualified_prompt("myorg/mypkg/myScript")
        assert result is not None

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        os.chdir(tmp_path)
        result = runner._discover_qualified_prompt("nonexistent/pkg/script")
        assert result is None

    def test_returns_none_for_single_part(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        os.chdir(tmp_path)
        result = runner._discover_qualified_prompt("singlepart")
        assert result is None


class TestPromptCompilerCollectDependencyDirs:
    """PromptCompiler._collect_dependency_dirs traverses apm_modules."""

    def _make_compiler(self) -> Any:
        from apm_cli.core.script_runner import PromptCompiler

        return PromptCompiler()

    def test_collects_org_repo_pairs(self, tmp_path: Path) -> None:
        compiler = self._make_compiler()
        (tmp_path / "apm_modules" / "org1" / "repo1").mkdir(parents=True)
        (tmp_path / "apm_modules" / "org2" / "repo2").mkdir(parents=True)
        dirs = compiler._collect_dependency_dirs(tmp_path / "apm_modules")
        names = [(o, r) for o, r, _ in dirs]
        assert ("org1", "repo1") in names
        assert ("org2", "repo2") in names

    def test_returns_empty_when_no_apm_modules(self, tmp_path: Path) -> None:
        compiler = self._make_compiler()
        dirs = compiler._collect_dependency_dirs(tmp_path / "apm_modules")
        assert dirs == []

    def test_ignores_hidden_dirs(self, tmp_path: Path) -> None:
        compiler = self._make_compiler()
        (tmp_path / "apm_modules" / ".hidden" / "repo").mkdir(parents=True)
        (tmp_path / "apm_modules" / "visible" / "repo").mkdir(parents=True)
        dirs = compiler._collect_dependency_dirs(tmp_path / "apm_modules")
        names = [o for o, _, _ in dirs]
        assert ".hidden" not in names


class TestPromptCompilerSubstituteParameters:
    """PromptCompiler._substitute_parameters replaces ${input:key} placeholders."""

    def _make_compiler(self) -> Any:
        from apm_cli.core.script_runner import PromptCompiler

        return PromptCompiler()

    def test_substitutes_single_placeholder(self) -> None:
        compiler = self._make_compiler()
        result = compiler._substitute_parameters("Hello ${input:name}!", {"name": "World"})
        assert result == "Hello World!"

    def test_multiple_placeholders(self) -> None:
        compiler = self._make_compiler()
        content = "Repo: ${input:repo}, Branch: ${input:branch}"
        result = compiler._substitute_parameters(content, {"repo": "my-repo", "branch": "main"})
        assert result == "Repo: my-repo, Branch: main"

    def test_unknown_placeholder_left_intact(self) -> None:
        compiler = self._make_compiler()
        content = "value: ${input:unknown}"
        result = compiler._substitute_parameters(content, {})
        assert result == content


class TestScriptRunnerIsVirtualPackageReference:
    """_is_virtual_package_reference identifies virtual package refs."""

    def test_simple_name_is_not_virtual(self) -> None:
        runner = _make_script_runner()
        assert runner._is_virtual_package_reference("simple-script") is False

    def test_owner_slash_repo_alone_is_not_virtual(self) -> None:
        runner = _make_script_runner()
        assert runner._is_virtual_package_reference("owner/repo") is False

    def test_virtual_file_path_is_virtual(self) -> None:
        runner = _make_script_runner()
        assert runner._is_virtual_package_reference("owner/repo/prompts/file.prompt.md") is True

    def test_invalid_string_returns_false(self) -> None:
        runner = _make_script_runner()
        # contains control characters that parse will reject
        assert runner._is_virtual_package_reference("owner/repo\x00exploit") is False


class TestScriptRunnerCreateMinimalConfig:
    """_create_minimal_config writes a valid apm.yml."""

    def test_creates_apm_yml(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        os.chdir(tmp_path)
        runner._create_minimal_config()
        apm_yml = tmp_path / "apm.yml"
        assert apm_yml.exists()
        data = yaml.safe_load(apm_yml.read_text())
        assert "name" in data
        assert data["version"] == "1.0.0"


class TestScriptRunnerAddDependencyToConfig:
    """_add_dependency_to_config updates apm.yml dependencies section."""

    def test_adds_new_dependency(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        apm_yml = tmp_path / "apm.yml"
        _write_apm_yml(apm_yml, {"name": "myproject", "version": "1.0.0"})
        os.chdir(tmp_path)
        runner._add_dependency_to_config("owner/repo/prompts/test.prompt.md")
        data = yaml.safe_load(apm_yml.read_text())
        assert "owner/repo/prompts/test.prompt.md" in data["dependencies"]["apm"]

    def test_no_duplicate_added(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        apm_yml = tmp_path / "apm.yml"
        _write_apm_yml(
            apm_yml,
            {
                "name": "p",
                "version": "1.0.0",
                "dependencies": {"apm": ["owner/repo/prompts/test.prompt.md"]},
            },
        )
        os.chdir(tmp_path)
        runner._add_dependency_to_config("owner/repo/prompts/test.prompt.md")
        data = yaml.safe_load(apm_yml.read_text())
        assert data["dependencies"]["apm"].count("owner/repo/prompts/test.prompt.md") == 1

    def test_skips_when_no_apm_yml(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        os.chdir(tmp_path)
        runner._add_dependency_to_config("owner/repo/prompts/file.prompt.md")  # must not raise


class TestScriptRunnerRunScript:
    """run_script dispatches to explicit scripts, discovers prompts, or raises."""

    def test_raises_without_apm_yml(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        os.chdir(tmp_path)
        with pytest.raises(RuntimeError, match=r"No apm\.yml"):
            runner.run_script("missing-script", {})

    def test_runs_explicit_script_via_shell(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        _write_apm_yml(
            tmp_path / "apm.yml",
            {
                "name": "p",
                "version": "1.0.0",
                "scripts": {"greet": "echo hello"},
            },
        )
        os.chdir(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = runner.run_script("greet", {})
        assert result is True

    def test_raises_when_script_not_found(self, tmp_path: Path) -> None:
        runner = _make_script_runner()
        _write_apm_yml(tmp_path / "apm.yml", {"name": "p", "version": "1.0.0"})
        os.chdir(tmp_path)
        with pytest.raises(RuntimeError, match="not found"):
            runner.run_script("nonexistent", {})


class TestPromptCompilerSubstituteAndCompile:
    """PromptCompiler.compile substitutes params and writes compiled output."""

    def test_compile_substitutes_params(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        compiler = PromptCompiler()
        compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text("Hello ${input:name}!", encoding="utf-8")
        os.chdir(tmp_path)
        out_path = compiler.compile(str(prompt_file), {"name": "World"})
        out = Path(out_path).read_text(encoding="utf-8")
        assert "Hello World!" in out

    def test_compile_strips_frontmatter(self, tmp_path: Path) -> None:
        from apm_cli.core.script_runner import PromptCompiler

        compiler = PromptCompiler()
        compiler.compiled_dir = tmp_path / ".apm" / "compiled"
        prompt_file = tmp_path / "fm.prompt.md"
        prompt_file.write_text("---\ntitle: test\n---\nContent here", encoding="utf-8")
        os.chdir(tmp_path)
        out_path = compiler.compile(str(prompt_file), {})
        out = Path(out_path).read_text(encoding="utf-8")
        assert "Content here" in out
        assert "title: test" not in out


# ============================================================================
# SECTION 4 - plugin_parser
# ============================================================================


class TestParsePluginManifest:
    """parse_plugin_manifest reads and validates plugin.json files."""

    def test_parse_minimal_manifest(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import parse_plugin_manifest

        pj = tmp_path / "plugin.json"
        pj.write_text('{"name": "minimal"}', encoding="utf-8")
        result = parse_plugin_manifest(pj)
        assert result["name"] == "minimal"

    def test_parse_full_manifest(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import parse_plugin_manifest

        pj = tmp_path / "plugin.json"
        manifest = {
            "name": "test-plugin",
            "version": "1.2.3",
            "description": "A test",
            "author": {"name": "Alice"},
            "license": "MIT",
            "tags": ["tag1"],
        }
        pj.write_text(json.dumps(manifest), encoding="utf-8")
        result = parse_plugin_manifest(pj)
        assert result["version"] == "1.2.3"

    def test_raises_for_missing_file(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import parse_plugin_manifest

        with pytest.raises(FileNotFoundError):
            parse_plugin_manifest(tmp_path / "nonexistent.json")

    def test_raises_for_invalid_json(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import parse_plugin_manifest

        pj = tmp_path / "plugin.json"
        pj.write_text("{not valid json}", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_plugin_manifest(pj)

    def test_missing_name_does_not_raise(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import parse_plugin_manifest

        pj = tmp_path / "plugin.json"
        pj.write_text('{"version": "1.0"}', encoding="utf-8")
        result = parse_plugin_manifest(pj)
        assert "version" in result


class TestExtractMcpServers:
    """_extract_mcp_servers resolves mcpServers from manifests."""

    def test_inline_dict_used_directly(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _extract_mcp_servers

        manifest: dict[str, Any] = {
            "mcpServers": {"my-server": {"command": "npx", "args": ["server"]}}
        }
        result = _extract_mcp_servers(tmp_path, manifest)
        assert "my-server" in result

    def test_fallback_to_mcp_json(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _extract_mcp_servers

        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"server-a": {"command": "npx"}}}), encoding="utf-8"
        )
        result = _extract_mcp_servers(tmp_path, {})
        assert "server-a" in result

    def test_falls_back_to_github_mcp_json(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _extract_mcp_servers

        gh_dir = tmp_path / ".github"
        gh_dir.mkdir()
        mcp_json = gh_dir / ".mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"gh-server": {"url": "http://localhost:3000"}}}),
            encoding="utf-8",
        )
        result = _extract_mcp_servers(tmp_path, {})
        assert "gh-server" in result

    def test_list_of_mcp_files_merged(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _extract_mcp_servers

        file1 = tmp_path / "mcp1.json"
        file1.write_text(
            json.dumps({"mcpServers": {"srv1": {"command": "cmd1"}}}), encoding="utf-8"
        )
        file2 = tmp_path / "mcp2.json"
        file2.write_text(
            json.dumps({"mcpServers": {"srv2": {"command": "cmd2"}}}), encoding="utf-8"
        )
        manifest: dict[str, Any] = {"mcpServers": ["mcp1.json", "mcp2.json"]}
        result = _extract_mcp_servers(tmp_path, manifest)
        assert "srv1" in result
        assert "srv2" in result

    def test_symlinked_mcp_json_skipped(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _extract_mcp_servers

        real_file = tmp_path / "real.json"
        real_file.write_text(
            json.dumps({"mcpServers": {"bad": {"command": "evil"}}}), encoding="utf-8"
        )
        symlink = tmp_path / ".mcp.json"
        symlink.symlink_to(real_file)
        # Symlink should be skipped
        result = _extract_mcp_servers(tmp_path, {})
        assert "bad" not in result

    def test_substitutes_plugin_root_placeholder(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _extract_mcp_servers

        manifest: dict[str, Any] = {
            "mcpServers": {"srv": {"command": "${CLAUDE_PLUGIN_ROOT}/run.sh"}}
        }
        result = _extract_mcp_servers(tmp_path, manifest)
        assert "${CLAUDE_PLUGIN_ROOT}" not in result["srv"]["command"]
        assert str(tmp_path.resolve()) in result["srv"]["command"]

    def test_unsupported_mcp_servers_type_returns_empty(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _extract_mcp_servers

        manifest: dict[str, Any] = {"mcpServers": 42}
        result = _extract_mcp_servers(tmp_path, manifest)
        assert result == {}


class TestMcpServersToDeps:
    """_mcp_servers_to_apm_deps converts server configs to dependency dicts."""

    def test_command_server_becomes_stdio(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _mcp_servers_to_apm_deps

        servers: dict[str, Any] = {"my-stdio": {"command": "npx", "args": ["-y", "@my/mcp-server"]}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert len(deps) == 1
        assert deps[0]["transport"] == "stdio"
        assert deps[0]["command"] == "npx"

    def test_url_server_becomes_http(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _mcp_servers_to_apm_deps

        servers: dict[str, Any] = {"my-http": {"url": "https://mcp.example.com/server"}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert len(deps) == 1
        assert deps[0]["transport"] == "http"
        assert deps[0]["url"] == "https://mcp.example.com/server"

    def test_sse_transport_preserved(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _mcp_servers_to_apm_deps

        servers: dict[str, Any] = {"my-sse": {"url": "https://mcp.example.com/sse", "type": "sse"}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps[0]["transport"] == "sse"

    def test_unknown_transport_falls_back_to_http(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _mcp_servers_to_apm_deps

        servers: dict[str, Any] = {
            "weird": {"url": "https://mcp.example.com/ws", "type": "websocket"}
        }
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps[0]["transport"] == "http"

    def test_server_without_command_or_url_skipped(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _mcp_servers_to_apm_deps

        servers: dict[str, Any] = {"incomplete": {"env": {"KEY": "val"}}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps == []

    def test_non_dict_server_skipped(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _mcp_servers_to_apm_deps

        servers: dict[str, Any] = {"bad": "not a dict"}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps == []

    def test_registry_field_set_to_false(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _mcp_servers_to_apm_deps

        servers: dict[str, Any] = {"srv": {"command": "node", "args": ["server.js"]}}
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps[0]["registry"] is False

    def test_env_and_tools_forwarded(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _mcp_servers_to_apm_deps

        servers: dict[str, Any] = {
            "srv": {
                "command": "node",
                "env": {"API_KEY": "${API_KEY}"},
                "tools": ["read", "write"],
            }
        }
        deps = _mcp_servers_to_apm_deps(servers, tmp_path)
        assert deps[0]["env"] == {"API_KEY": "${API_KEY}"}
        assert deps[0]["tools"] == ["read", "write"]


class TestGenerateApmYml:
    """_generate_apm_yml creates valid YAML from plugin metadata."""

    def test_generates_name_and_version(self) -> None:
        from apm_cli.deps.plugin_parser import _generate_apm_yml

        manifest: dict[str, Any] = {"name": "my-plugin", "version": "1.0.0"}
        content = _generate_apm_yml(manifest)
        data = yaml.safe_load(content)
        assert data["name"] == "my-plugin"
        assert data["version"] == "1.0.0"

    def test_defaults_version_to_zero(self) -> None:
        from apm_cli.deps.plugin_parser import _generate_apm_yml

        content = _generate_apm_yml({"name": "no-version"})
        data = yaml.safe_load(content)
        assert data["version"] == "0.0.0"

    def test_author_string_accepted(self) -> None:
        from apm_cli.deps.plugin_parser import _generate_apm_yml

        content = _generate_apm_yml({"name": "p", "author": "Alice"})
        data = yaml.safe_load(content)
        assert data["author"] == "Alice"

    def test_author_dict_uses_name_field(self) -> None:
        from apm_cli.deps.plugin_parser import _generate_apm_yml

        content = _generate_apm_yml({"name": "p", "author": {"name": "Bob", "email": "b@c.com"}})
        data = yaml.safe_load(content)
        assert data["author"] == "Bob"

    def test_dependencies_wrapped_in_apm_key(self) -> None:
        from apm_cli.deps.plugin_parser import _generate_apm_yml

        content = _generate_apm_yml({"name": "p", "dependencies": ["dep-a", "dep-b"]})
        data = yaml.safe_load(content)
        assert "apm" in data["dependencies"]

    def test_mcp_deps_injected(self) -> None:
        from apm_cli.deps.plugin_parser import _generate_apm_yml

        mcp_deps = [{"name": "my-server", "transport": "stdio", "command": "npx"}]
        content = _generate_apm_yml({"name": "p", "_mcp_deps": mcp_deps})
        data = yaml.safe_load(content)
        assert "mcp" in data["dependencies"]

    def test_type_set_to_hybrid(self) -> None:
        from apm_cli.deps.plugin_parser import _generate_apm_yml

        content = _generate_apm_yml({"name": "p"})
        data = yaml.safe_load(content)
        assert data["type"] == "hybrid"


class TestSynthesizeApmYmlFromPlugin:
    """synthesize_apm_yml_from_plugin creates an apm.yml in the plugin directory."""

    def test_creates_apm_yml(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin

        manifest: dict[str, Any] = {"name": "test-plugin", "version": "1.0.0"}
        path = synthesize_apm_yml_from_plugin(tmp_path, manifest)
        assert path.exists()
        data = yaml.safe_load(path.read_text())
        assert data["name"] == "test-plugin"

    def test_maps_agents_directory(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "helper.md").write_text("# Helper", encoding="utf-8")
        synthesize_apm_yml_from_plugin(tmp_path, {"name": "p"})
        assert (tmp_path / ".apm" / "agents" / "helper.md").exists()

    def test_maps_skills_directory(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin

        skills_dir = tmp_path / "skills" / "my-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("# Skill", encoding="utf-8")
        synthesize_apm_yml_from_plugin(tmp_path, {"name": "p"})
        assert (tmp_path / ".apm" / "skills").exists()

    def test_maps_commands_to_prompts(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin

        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "my-cmd.md").write_text("# Cmd", encoding="utf-8")
        synthesize_apm_yml_from_plugin(tmp_path, {"name": "p"})
        prompts_dir = tmp_path / ".apm" / "prompts"
        assert prompts_dir.exists()
        # .md should be normalized to .prompt.md
        prompts = list(prompts_dir.rglob("*.prompt.md"))
        assert any("my-cmd" in p.name for p in prompts)

    def test_passthrough_mcp_json_copied(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin

        mcp_json = tmp_path / ".mcp.json"
        mcp_json.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "npx"}}}), encoding="utf-8"
        )
        synthesize_apm_yml_from_plugin(tmp_path, {"name": "p"})
        assert (tmp_path / ".apm" / ".mcp.json").exists()

    def test_passthrough_settings_json_copied(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_apm_yml_from_plugin

        settings = tmp_path / "settings.json"
        settings.write_text('{"theme": "dark"}', encoding="utf-8")
        synthesize_apm_yml_from_plugin(tmp_path, {"name": "p"})
        assert (tmp_path / ".apm" / "settings.json").exists()


class TestNormalizePluginDirectory:
    """normalize_plugin_directory handles both with-manifest and no-manifest cases."""

    def test_with_manifest(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import normalize_plugin_directory

        pj = tmp_path / "plugin.json"
        pj.write_text('{"name": "plugin-from-manifest"}', encoding="utf-8")
        path = normalize_plugin_directory(tmp_path, pj)
        data = yaml.safe_load(path.read_text())
        assert data["name"] == "plugin-from-manifest"

    def test_without_manifest_uses_dir_name(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import normalize_plugin_directory

        path = normalize_plugin_directory(tmp_path, None)
        data = yaml.safe_load(path.read_text())
        assert data["name"] == tmp_path.name

    def test_with_invalid_manifest_falls_back_to_dir_name(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import normalize_plugin_directory

        pj = tmp_path / "plugin.json"
        pj.write_text("{bad json", encoding="utf-8")
        path = normalize_plugin_directory(tmp_path, pj)
        data = yaml.safe_load(path.read_text())
        assert data["name"] == tmp_path.name


class TestValidatePluginPackage:
    """validate_plugin_package detects valid Claude plugin directories."""

    def test_valid_with_plugin_json(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import validate_plugin_package

        (tmp_path / "plugin.json").write_text('{"name": "p"}', encoding="utf-8")
        assert validate_plugin_package(tmp_path) is True

    def test_valid_with_agents_directory(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import validate_plugin_package

        (tmp_path / "agents").mkdir()
        assert validate_plugin_package(tmp_path) is True

    def test_valid_with_commands_directory(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import validate_plugin_package

        (tmp_path / "commands").mkdir()
        assert validate_plugin_package(tmp_path) is True

    def test_invalid_empty_dir(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import validate_plugin_package

        assert validate_plugin_package(tmp_path) is False

    def test_invalid_plugin_json_without_name(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import validate_plugin_package

        (tmp_path / "plugin.json").write_text('{"version": "1.0"}', encoding="utf-8")
        # Falls back to component-directory scan
        assert validate_plugin_package(tmp_path) is False

    def test_valid_with_skills_directory(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import validate_plugin_package

        (tmp_path / "skills").mkdir()
        assert validate_plugin_package(tmp_path) is True

    def test_valid_with_hooks_directory(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import validate_plugin_package

        (tmp_path / "hooks").mkdir()
        assert validate_plugin_package(tmp_path) is True


class TestSynthesizePluginJsonFromApmYml:
    """synthesize_plugin_json_from_apm_yml maps apm.yml fields to plugin.json shape."""

    def test_produces_correct_name(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_plugin_json_from_apm_yml

        apm_yml = tmp_path / "apm.yml"
        _write_apm_yml(apm_yml, {"name": "my-plugin", "version": "1.2.3"})
        result = synthesize_plugin_json_from_apm_yml(apm_yml)
        assert result["name"] == "my-plugin"
        assert result["version"] == "1.2.3"

    def test_author_string_mapped_to_object(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_plugin_json_from_apm_yml

        apm_yml = tmp_path / "apm.yml"
        _write_apm_yml(apm_yml, {"name": "p", "author": "Alice"})
        result = synthesize_plugin_json_from_apm_yml(apm_yml)
        assert result["author"] == {"name": "Alice"}

    def test_raises_for_missing_name(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_plugin_json_from_apm_yml

        apm_yml = tmp_path / "apm.yml"
        _write_apm_yml(apm_yml, {"version": "1.0.0"})
        with pytest.raises(ValueError, match="name"):
            synthesize_plugin_json_from_apm_yml(apm_yml)

    def test_raises_for_missing_file(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_plugin_json_from_apm_yml

        with pytest.raises(FileNotFoundError):
            synthesize_plugin_json_from_apm_yml(tmp_path / "nonexistent.yml")

    def test_license_preserved(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_plugin_json_from_apm_yml

        apm_yml = tmp_path / "apm.yml"
        _write_apm_yml(apm_yml, {"name": "p", "license": "MIT"})
        result = synthesize_plugin_json_from_apm_yml(apm_yml)
        assert result["license"] == "MIT"

    def test_optional_fields_not_present_when_absent(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import synthesize_plugin_json_from_apm_yml

        apm_yml = tmp_path / "apm.yml"
        _write_apm_yml(apm_yml, {"name": "bare"})
        result = synthesize_plugin_json_from_apm_yml(apm_yml)
        assert "version" not in result
        assert "author" not in result


class TestMapPluginArtifactsHooks:
    """_map_plugin_artifacts handles hooks as inline dict, file, or directory."""

    def test_inline_hooks_object_written(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _map_plugin_artifacts

        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        manifest: dict[str, Any] = {
            "name": "p",
            "hooks": {"PostToolUse": [{"type": "command", "command": "echo done"}]},
        }
        _map_plugin_artifacts(tmp_path, apm_dir, manifest)
        hooks_json = apm_dir / "hooks" / "hooks.json"
        assert hooks_json.exists()
        data = json.loads(hooks_json.read_text())
        assert "PostToolUse" in data

    def test_hooks_file_path_copied(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _map_plugin_artifacts

        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        hooks_file = tmp_path / "hooks.json"
        hooks_file.write_text('{"Stop": []}', encoding="utf-8")
        manifest: dict[str, Any] = {"name": "p", "hooks": "hooks.json"}
        _map_plugin_artifacts(tmp_path, apm_dir, manifest)
        assert (apm_dir / "hooks" / "hooks.json").exists()

    def test_hooks_directory_copied(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _map_plugin_artifacts

        apm_dir = tmp_path / ".apm"
        apm_dir.mkdir()
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "pre.sh").write_text("#!/bin/bash", encoding="utf-8")
        manifest: dict[str, Any] = {"name": "p"}
        _map_plugin_artifacts(tmp_path, apm_dir, manifest)
        assert (apm_dir / "hooks").exists()


class TestIsWithinPlugin:
    """_is_within_plugin guards against path-traversal attacks."""

    def test_valid_path_within_plugin_root(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _is_within_plugin

        candidate = tmp_path / "sub" / "file.md"
        candidate.parent.mkdir(parents=True)
        candidate.write_text("content", encoding="utf-8")
        assert _is_within_plugin(candidate, tmp_path, component="agents") is True

    def test_escaping_path_rejected(self, tmp_path: Path) -> None:
        from apm_cli.deps.plugin_parser import _is_within_plugin

        outside = tmp_path.parent / "escape.md"
        outside.write_text("evil", encoding="utf-8")
        assert _is_within_plugin(outside, tmp_path, component="agents") is False


class TestReadMcpJson:
    """_read_mcp_json parses JSON MCP config files correctly."""

    def test_reads_mcp_servers(self, tmp_path: Path) -> None:
        import logging

        from apm_cli.deps.plugin_parser import _read_mcp_json

        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"srv": {"command": "npx"}}}), encoding="utf-8")
        result = _read_mcp_json(path, logging.getLogger("test"))
        assert "srv" in result

    def test_returns_empty_for_invalid_json(self, tmp_path: Path) -> None:
        import logging

        from apm_cli.deps.plugin_parser import _read_mcp_json

        path = tmp_path / "bad.json"
        path.write_text("{not valid}", encoding="utf-8")
        result = _read_mcp_json(path, logging.getLogger("test"))
        assert result == {}

    def test_returns_empty_when_no_mcp_servers_key(self, tmp_path: Path) -> None:
        import logging

        from apm_cli.deps.plugin_parser import _read_mcp_json

        path = tmp_path / "no-servers.json"
        path.write_text('{"other": "data"}', encoding="utf-8")
        result = _read_mcp_json(path, logging.getLogger("test"))
        assert result == {}

    def test_returns_empty_for_non_dict_root(self, tmp_path: Path) -> None:
        import logging

        from apm_cli.deps.plugin_parser import _read_mcp_json

        path = tmp_path / "array.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        result = _read_mcp_json(path, logging.getLogger("test"))
        assert result == {}


class TestSubstitutePluginRoot:
    """_substitute_plugin_root replaces placeholder in nested structures."""

    def test_substitutes_in_string_values(self) -> None:
        import logging

        from apm_cli.deps.plugin_parser import _substitute_plugin_root

        servers: dict[str, Any] = {"s": {"command": "${CLAUDE_PLUGIN_ROOT}/run.sh"}}
        result = _substitute_plugin_root(servers, "/abs/path", logging.getLogger("test"))
        assert result["s"]["command"] == "/abs/path/run.sh"

    def test_substitutes_in_nested_list(self) -> None:
        import logging

        from apm_cli.deps.plugin_parser import _substitute_plugin_root

        servers: dict[str, Any] = {"s": {"args": ["${CLAUDE_PLUGIN_ROOT}/bin", "--port", "3000"]}}
        result = _substitute_plugin_root(servers, "/root", logging.getLogger("test"))
        assert result["s"]["args"][0] == "/root/bin"

    def test_non_placeholder_strings_unchanged(self) -> None:
        import logging

        from apm_cli.deps.plugin_parser import _substitute_plugin_root

        servers: dict[str, Any] = {"s": {"command": "npx"}}
        result = _substitute_plugin_root(servers, "/root", logging.getLogger("test"))
        assert result["s"]["command"] == "npx"
