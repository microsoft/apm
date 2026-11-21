"""Integration tests for github_downloader.

Covers uncovered lines / branches in:
  src/apm_cli/deps/github_downloader.py

Strategy: hermetic -- all external I/O is mocked.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from git import Actor, Repo

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.github_downloader import GitHubPackageDownloader

# ---------------------------------------------------------------------------
# Helpers to build a minimal downloader without full __init__ setup
# ---------------------------------------------------------------------------


def _make_downloader() -> GitHubPackageDownloader:
    """Construct a downloader with minimal external dependencies mocked."""
    dl = GitHubPackageDownloader.__new__(GitHubPackageDownloader)
    dl.auth_resolver = MagicMock()
    dl.auth_resolver.build_noninteractive_git_env.side_effect = (
        AuthResolver.build_noninteractive_git_env
    )
    dl.token_manager = MagicMock()
    dl.git_env = {}
    dl.github_token = None
    dl._transport_selector = MagicMock()
    dl._protocol_pref = None
    dl._allow_fallback = False
    dl._fallback_port_warned = set()
    import threading

    dl._fallback_port_warned_lock = threading.Lock()
    dl._strategies = MagicMock()
    dl._artifactory = MagicMock()
    dl._refs = MagicMock()
    dl._clone_engine = MagicMock()
    dl.shared_clone_cache = None
    dl.persistent_git_cache = None
    dl._tiered_resolver = None
    return dl


def _configure_http_remote_env(downloader: GitHubPackageDownloader) -> None:
    """Make the resolver mock return the canonical plaintext-HTTP fence."""
    downloader.auth_resolver.git_env_for_remote.return_value = (
        AuthResolver.build_noninteractive_git_env(
            base_env=downloader.git_env,
            host_kind="gitlab",
            preserve_config_isolation=True,
            suppress_credential_helpers=True,
        )
    )


def _commit_cached_checkout(checkout: Path) -> None:
    """Commit every file in *checkout*, as a persistent-cache checkout carries .git.

    The cache fast path validates tracked links against the checkout's Git
    objects (#3457), so a fixture without .git falls through to a clone.
    """
    actor = Actor("APM Test", "apm-test@example.invalid")
    cached_repo = Repo.init(checkout)
    cached_repo.index.add([path.name for path in checkout.iterdir() if path.name != ".git"])
    cached_repo.index.commit("seed cache", author=actor, committer=actor)
    cached_repo.close()


# ---------------------------------------------------------------------------
# GitProgressReporter._get_op_name -- lines 143, 145, 149, 151, 153
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitHubPackageDownloader._get_clone_engine -- lazy construction
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitHubPackageDownloader._resolve_dep_token
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitHubPackageDownloader._resolve_dep_auth_ctx
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitHubPackageDownloader._is_generic_dependency_host
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_virtual_file_package -- frontmatter parsing (lines 899-915)
# ---------------------------------------------------------------------------


class TestDownloadVirtualFilePackage:
    """Test virtual file package download, particularly frontmatter parsing."""

    def _make_dep_ref(
        self,
        virtual_path: str = "owner/repo/.instructions.md",
        repo_url: str = "owner/repo",
    ) -> MagicMock:
        dep_ref = MagicMock()
        dep_ref.virtual_path = virtual_path
        dep_ref.repo_url = repo_url
        dep_ref.get_virtual_package_name.return_value = "owner-repo"
        dep_ref.reference = None
        return dep_ref

    def test_frontmatter_description_extracted(self, tmp_path):
        """Remote file download uses the real metadata materializer."""
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.models.dependency import DependencyReference

        dep_ref = DependencyReference.parse("owner/repo/.instructions.md")
        file_content = b"---\ndescription: 'My custom description'\n---\n# Instructions\n"
        dl = _make_downloader()
        dl.download_raw_file = MagicMock(return_value=file_content)
        dl._resolve_commit_sha_for_ref = MagicMock(return_value="a" * 40)
        target = tmp_path / "pkg"
        result = dl.download_virtual_file_package(dep_ref, target)
        assert result.package.description == "My custom description"
        assert APMPackage.from_apm_yml(target / "apm.yml").description == result.package.description
        assert (target / ".apm/instructions/.instructions.md").read_bytes() == file_content
        dl.download_raw_file.assert_called_once_with(dep_ref, ".instructions.md", "main")
        assert result.resolved_reference.resolved_commit == "a" * 40


# ---------------------------------------------------------------------------
# download_subdirectory -- persistent cache paths (lines 1082-1090)
# ---------------------------------------------------------------------------


class TestDownloadSubdirectoryPersistentCache:
    def _make_dep_ref(self, repo_url: str = "owner/repo") -> MagicMock:
        dep_ref = MagicMock()
        dep_ref.repo_url = repo_url
        dep_ref.host = "github.com"
        dep_ref.virtual_path = "packages/my-pkg"
        dep_ref.reference = "main"
        dep_ref.get_unique_key.return_value = "owner/repo@main"
        dep_ref.to_github_url.return_value = f"https://gitlab.com/{repo_url}"
        return dep_ref

    def test_persistent_cache_hit_used(self, tmp_path):
        """Lines 1082-1090: persistent cache hit -> uses cached checkout."""
        dep_ref = self._make_dep_ref("spiritt/tenants/spiritt/agent-cfg")
        dep_ref.is_insecure = True
        dep_ref.to_github_url.return_value = "http://gitlab.com/spiritt/tenants/spiritt/agent-cfg"

        cached_checkout = tmp_path / "cached"
        cached_checkout.mkdir()
        # Put a valid subdirectory in the cache
        pkg_dir = cached_checkout / "packages" / "my-pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "apm.yml").write_bytes(b"name: my-pkg\nversion: 1.0.0\n")
        actor = Actor("APM Test", "apm-test@example.invalid")
        cached_repo = Repo.init(cached_checkout)
        cached_repo.index.add(["packages/my-pkg/apm.yml"])
        cached_repo.index.commit(
            "seed cache",
            author=actor,
            committer=actor,
        )

        target_path = tmp_path / "target"

        dl = _make_downloader()
        dl.shared_clone_cache = None
        dl.git_env.update(
            {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "store",
                "GIT_CONFIG_KEY_1": "http.extraheader",
                "GIT_CONFIG_VALUE_1": "Authorization: Bearer secret",
                "GIT_CONFIG_PARAMETERS": "'credential.helper=store'",
                "GIT_HTTP_EXTRAHEADER": "Authorization: Basic secret",
            }
        )
        _configure_http_remote_env(dl)

        persistent_cache = MagicMock()
        persistent_cache.get_checkout.return_value = cached_checkout
        dl.persistent_git_cache = persistent_cache

        with (
            patch("apm_cli.deps.github_downloader.validate_apm_package") as mock_validate,
            patch("apm_cli.deps.github_downloader.Repo") as mock_repo_cls,
            patch("apm_cli.utils.path_security.ensure_path_within"),
            patch(
                "apm_cli.utils.file_ops.robust_copy2",
            ),
            patch(
                "apm_cli.utils.file_ops.robust_copytree",
            ),
            patch("apm_cli.deps.package_validator.stamp_plugin_version"),
            patch("apm_cli.deps.github_downloader._rmtree"),
        ):
            mock_result = MagicMock()
            mock_result.is_valid = True
            mock_result.package = MagicMock()
            mock_result.package_type = MagicMock()
            mock_validate.return_value = mock_result

            mock_repo = MagicMock()
            mock_repo.head.commit.hexsha = "abc123def456" * 3 + "ab"
            mock_repo_cls.return_value = mock_repo

            target_path.mkdir(parents=True, exist_ok=True)

            dl.download_subdirectory_package(
                dep_ref=dep_ref,
                target_path=target_path,
            )

        assert persistent_cache.get_checkout.call_args.args[0] == (
            "http://gitlab.com/spiritt/tenants/spiritt/agent-cfg"
        )
        cache_env = persistent_cache.get_checkout.call_args.kwargs["env"]
        assert cache_env["GIT_ASKPASS"] == "echo"
        assert cache_env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert cache_env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert cache_env["GIT_CONFIG_COUNT"] == "1"
        assert cache_env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert cache_env["GIT_CONFIG_VALUE_0"] == ""
        assert cache_env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_KEY_1" not in cache_env
        assert "GIT_CONFIG_VALUE_1" not in cache_env
        assert "GIT_CONFIG_PARAMETERS" not in cache_env
        assert "GIT_HTTP_EXTRAHEADER" not in cache_env

    def test_persistent_cache_miss_falls_through(self, tmp_path):
        """Lines 1088-1090: cache.get_checkout raises -> falls through to clone."""
        dep_ref = self._make_dep_ref()
        target_path = tmp_path / "target"
        target_path.mkdir()

        dl = _make_downloader()
        dl.shared_clone_cache = None

        persistent_cache = MagicMock()
        persistent_cache.get_checkout.side_effect = Exception("cache miss")
        dl.persistent_git_cache = persistent_cache

        # Patch the rest of the download path so we don't actually clone
        with (
            patch.object(dl, "_try_sparse_checkout", return_value=False) as mock_sparse,
            patch.object(dl, "_clone_with_fallback", side_effect=RuntimeError("no network")),
        ):
            with pytest.raises(RuntimeError, match="no network"):
                dl.download_subdirectory_package(
                    dep_ref=dep_ref,
                    target_path=target_path,
                )

        # Confirm the sparse checkout was attempted (cache miss fell through)
        mock_sparse.assert_called()


class TestDownloadWholeRepositoryPersistentCache:
    def test_cache_identity_preserves_full_nested_gitlab_path(self, tmp_path):
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference.parse(
            "http://gitlab.com/spiritt/tenants/spiritt/agent-cfg.git#main"
        )
        cached_checkout = tmp_path / "cached"
        cached_checkout.mkdir()
        (cached_checkout / "apm.yml").write_text("name: agent-cfg\nversion: 1.0.0\n")
        _commit_cached_checkout(cached_checkout)

        dl = _make_downloader()
        dl.git_env.update(
            {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "store",
                "GIT_CONFIG_KEY_1": "http.extraheader",
                "GIT_CONFIG_VALUE_1": "Authorization: Bearer secret",
                "GIT_CONFIG_PARAMETERS": "'credential.helper=store'",
                "GIT_HTTP_EXTRAHEADER": "Authorization: Basic secret",
            }
        )
        _configure_http_remote_env(dl)
        persistent_cache = MagicMock()
        persistent_cache.get_checkout.return_value = cached_checkout
        dl.persistent_git_cache = persistent_cache

        resolved_ref = MagicMock()
        resolved_ref.resolved_commit = "a" * 40
        resolved_ref.ref_name = "main"

        with (
            patch.object(dl, "_parse_artifactory_base_url", return_value=None),
            patch.object(dl, "_is_artifactory_only", return_value=False),
            patch.object(dl, "resolve_git_reference", return_value=resolved_ref),
            patch("apm_cli.deps.github_downloader.validate_apm_package") as mock_validate,
            patch("apm_cli.utils.file_ops.robust_copy2"),
            patch.object(
                dl,
                "_clone_with_fallback",
                side_effect=AssertionError("cache hit must not clone"),
            ),
        ):
            mock_result = MagicMock()
            mock_result.is_valid = True
            mock_result.package = MagicMock(version="1.0.0")
            mock_result.package_type = MagicMock()
            mock_validate.return_value = mock_result

            dl.download_package(dep_ref, tmp_path / "target")

        assert persistent_cache.get_checkout.call_args.args[0] == (
            "http://gitlab.com/spiritt/tenants/spiritt/agent-cfg"
        )
        cache_env = persistent_cache.get_checkout.call_args.kwargs["env"]
        assert cache_env["GIT_ASKPASS"] == "echo"
        assert cache_env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert cache_env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert cache_env["GIT_CONFIG_COUNT"] == "1"
        assert cache_env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert cache_env["GIT_CONFIG_VALUE_0"] == ""
        assert cache_env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_CONFIG_KEY_1" not in cache_env
        assert "GIT_CONFIG_VALUE_1" not in cache_env
        assert "GIT_CONFIG_PARAMETERS" not in cache_env
        assert "GIT_HTTP_EXTRAHEADER" not in cache_env


class TestDownloadWholeRepositoryPersistentCacheAgentPluginRejection:
    """Regression test: a rejected Agent Plugin on the cache fast path must not
    leave a partially-populated target_path on disk.

    Covers the gap where `except AgentPluginError: raise` skipped the cleanup
    performed by the sibling `except Exception` branch a few lines below.
    """

    def test_rejected_agent_plugin_cleans_up_target_path(self, tmp_path):
        from apm_cli.agent_plugins.errors import AgentPluginManifestError
        from apm_cli.models.apm_package import DependencyReference

        dep_ref = DependencyReference.parse("http://gitlab.com/owner/native-plugin.git#main")
        cached_checkout = tmp_path / "cached"
        cached_checkout.mkdir()
        (cached_checkout / "plugin.json").write_text("{}", encoding="utf-8")
        _commit_cached_checkout(cached_checkout)

        dl = _make_downloader()
        persistent_cache = MagicMock()
        persistent_cache.get_checkout.return_value = cached_checkout
        dl.persistent_git_cache = persistent_cache

        resolved_ref = MagicMock()
        resolved_ref.resolved_commit = "a" * 40
        resolved_ref.ref_name = "main"

        target_path = tmp_path / "target"

        with (
            patch.object(dl, "_parse_artifactory_base_url", return_value=None),
            patch.object(dl, "_is_artifactory_only", return_value=False),
            patch.object(dl, "resolve_git_reference", return_value=resolved_ref),
            patch("apm_cli.utils.file_ops.robust_copy2"),
            patch("apm_cli.utils.file_ops.robust_copytree"),
            patch(
                "apm_cli.bundle.local_bundle.route_agent_plugin_package",
                side_effect=AgentPluginManifestError("unsupported schema"),
            ),
            patch("apm_cli.deps.github_downloader._rmtree") as mock_rmtree,
            patch.object(
                dl,
                "_clone_with_fallback",
                side_effect=AssertionError("cache hit must not clone"),
            ),
        ):
            with pytest.raises(AgentPluginManifestError):
                dl.download_package(dep_ref, target_path)

        assert any(call.args[0] == target_path for call in mock_rmtree.call_args_list), (
            f"target_path was not cleaned up; _rmtree calls: {mock_rmtree.call_args_list}"
        )


# ---------------------------------------------------------------------------
# download_subdirectory -- PermissionError / OSError paths
# (lines 1299-1321)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitHub downloader: _build_noninteractive_git_env stub
# ---------------------------------------------------------------------------


class TestBuildNoninteractiveGitEnv:
    def test_delegates_with_downloader_git_environment(self):
        """Noninteractive attempts derive from the downloader-owned Git env."""
        dl = _make_downloader()
        dl.git_env = {"GIT_CONFIG_GLOBAL": "/caller/gitconfig"}
        expected = {"GIT_TERMINAL_PROMPT": "0"}
        dl.auth_resolver.build_noninteractive_git_env.side_effect = None
        dl.auth_resolver.build_noninteractive_git_env.return_value = expected

        result = dl._build_noninteractive_git_env(
            preserve_config_isolation=True,
            suppress_credential_helpers=True,
        )

        assert result is expected
        dl.auth_resolver.build_noninteractive_git_env.assert_called_once_with(
            base_env=dl.git_env,
            preserve_config_isolation=True,
            suppress_credential_helpers=True,
        )


# ---------------------------------------------------------------------------
# _git_env_dict delegate
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_subdirectory -- commit SHA clone path
# (lines 1208, 1227-1235)
# ---------------------------------------------------------------------------
