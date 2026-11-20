"""Integration tests for github_downloader -- phase 3 wave 4.

Covers uncovered lines / branches in:
  src/apm_cli/deps/github_downloader.py

Strategy: hermetic -- all external I/O is mocked.
"""

from __future__ import annotations

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
        """The real materializer persists the frontmatter description."""
        from apm_cli.deps.virtual_file import materialize_virtual_file_package
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.models.dependency import DependencyReference

        dep_ref = DependencyReference.parse("owner/repo/.instructions.md")
        file_content = b"---\ndescription: 'My custom description'\n---\n# Instructions\n"
        target = tmp_path / "pkg"
        package = materialize_virtual_file_package(
            dep_ref, target, ".instructions.md", file_content
        )
        assert package.description == "My custom description"
        assert APMPackage.from_apm_yml(target / "apm.yml").description == package.description
        assert (target / ".apm/instructions/.instructions.md").read_bytes() == file_content


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
        dep_ref.is_insecure = False
        dep_ref.get_unique_key.return_value = "owner/repo@main"
        dep_ref.to_github_url.return_value = f"https://gitlab.com/{repo_url}"
        return dep_ref

    def test_persistent_cache_hit_used(self, tmp_path):
        """Lines 1082-1090: persistent cache hit -> uses cached checkout."""
        dep_ref = self._make_dep_ref("spiritt/tenants/spiritt/agent-cfg")

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
            "https://gitlab.com/spiritt/tenants/spiritt/agent-cfg"
        )

        # Regression: the subdir-aware sparse_paths and locked_sha (when
        # available) MUST propagate to the persistent cache so the
        # variant-keyed shard ((sha, sparse_paths)) is actually hit and
        # the cold-path bloat fix from #1433 is not silently bypassed.
        call_kwargs = persistent_cache.get_checkout.call_args.kwargs
        assert call_kwargs["env"] == dl._git_env_dict()
        assert "sparse_paths" in call_kwargs
        assert call_kwargs["sparse_paths"] == ["packages/my-pkg"]
        # locked_sha is plumbed via the kwarg; presence (not value) is the
        # contract this test defends.
        assert "locked_sha" in call_kwargs

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


# ---------------------------------------------------------------------------
# download_subdirectory -- PermissionError / OSError paths
# (lines 1299-1321)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GitHub downloader: _build_noninteractive_git_env stub
# ---------------------------------------------------------------------------


class TestBuildNoninteractiveGitEnv:
    def test_delegates_to_git_auth_env_builder(self):
        """Lines 419-440: builds GIT_ASKPASS env dict."""
        dl = _make_downloader()
        dl.github_token = "tok123"

        with patch("apm_cli.deps.git_auth_env.GitAuthEnvBuilder") as MockBuilder:
            MockBuilder.noninteractive_env.return_value = {"GIT_ASKPASS": "/dev/null"}
            result = dl._build_noninteractive_git_env()

        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# _git_env_dict delegate
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# download_subdirectory -- commit SHA clone path
# (lines 1208, 1227-1235)
# ---------------------------------------------------------------------------
