"""Tests for GitHubPackageDownloader.list_remote_refs() and helpers (filesystem, process and CLI boundary cases)."""

import os
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch  # noqa: F401

import pytest
from git.exc import GitCommandError

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.git_remote_ops import RemoteRefParseError
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-host-credential-resolution", "P-OWN-ls-remote-tag-commits"),
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_LS_REMOTE = (
    "aaa1111111111111111111111111111111111111\trefs/heads/main\n"
    "bbb2222222222222222222222222222222222222\trefs/heads/feature/xyz\n"
    "ccc3333333333333333333333333333333333333\trefs/tags/v1.0.0\n"
    "ddd4444444444444444444444444444444444444\trefs/tags/v2.0.0\n"
    "eee5555555555555555555555555555555555555\trefs/tags/v0.9.0\n"
)

SAMPLE_LS_REMOTE_WITH_DEREF = (
    "tag1111111111111111111111111111111111111\trefs/tags/v1.0.0\n"
    "com1111111111111111111111111111111111111\trefs/tags/v1.0.0^{}\n"
    "tag2222222222222222222222222222222222222\trefs/tags/v2.0.0\n"
    "com2222222222222222222222222222222222222\trefs/tags/v2.0.0^{}\n"
    "aaa1111111111111111111111111111111111111\trefs/heads/main\n"
)


def _make_dep_ref(host=None, ado=False, artifactory=False, repo_url="owner/repo"):
    """Build a minimal DependencyReference for testing."""
    kwargs = dict(repo_url=repo_url, host=host)
    if ado:
        kwargs.update(
            host=host or "dev.azure.com",
            ado_organization="myorg",
            ado_project="myproj",
            ado_repo="myrepo",
        )
    if artifactory:
        kwargs["artifactory_prefix"] = "artifactory/github"
    return DependencyReference(**kwargs)


def _build_downloader():
    """Build a GitHubPackageDownloader with mocked auth."""
    with patch("apm_cli.deps.github_downloader.AuthResolver") as MockAuth:
        mock_auth = MockAuth.return_value
        mock_auth._token_manager = MagicMock()
        mock_auth._token_manager.setup_environment.return_value = {
            "PATH": "/usr/bin",
        }
        mock_auth._token_manager.get_token_for_purpose.return_value = None
        mock_auth.build_error_context.return_value = "Check your auth setup."
        mock_auth.build_noninteractive_git_env.side_effect = (
            AuthResolver.build_noninteractive_git_env
        )

        # Single-attempt chain: run the operation once with the caller's env so
        # tests can drive the ADO path without configuring credential sources.
        def _single_attempt(_host, operation, **kwargs):
            return operation(None, dict(kwargs.get("base_env") or {}))

        mock_auth.try_with_fallback.side_effect = _single_attempt
        downloader = GitHubPackageDownloader(auth_resolver=mock_auth)
    return downloader


# ---------------------------------------------------------------------------
# _parse_ls_remote_output
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _semver_sort_key / _sort_remote_refs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# list_remote_refs -- Artifactory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# list_remote_refs -- GitHub / generic (git ls-remote path)
# ---------------------------------------------------------------------------


class TestListRemoteRefsGitHub:
    """Tests for the git ls-remote code path."""

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_github_with_token(self, MockGitCmd):
        """With a resolved token, uses locked-down env and authenticated URL."""
        dl = _build_downloader()
        dep = _make_dep_ref(host="github.com")

        dl._resolve_dep_token = MagicMock(return_value="ghp_test_token")
        auth_ctx = MagicMock(token="ghp_test_token", auth_scheme="basic")
        auth_ctx.host_info.kind = "github"
        dl._resolve_dep_auth_ctx = MagicMock(return_value=auth_ctx)
        dl.auth_resolver.git_env_for_context.side_effect = AuthResolver.git_env_for_context
        dl._build_repo_url = MagicMock(return_value="https://github.com/owner/repo.git")

        mock_git = MockGitCmd.return_value
        mock_git.ls_remote.return_value = SAMPLE_LS_REMOTE

        result = dl.list_remote_refs(dep)

        dl._resolve_dep_token.assert_called_once_with(dep)
        dl._build_repo_url.assert_any_call(
            "owner/repo",
            use_ssh=False,
            dep_ref=dep,
        )
        mock_git.ls_remote.assert_called_once()
        # The token travels through transient Git config, not URL userinfo.
        call_kwargs = mock_git.ls_remote.call_args
        env = call_kwargs.kwargs["env"]
        assert "GIT_TOKEN" not in env
        assert any(
            value.startswith("Authorization: Basic ")
            for key, value in env.items()
            if key.startswith("GIT_CONFIG_VALUE_")
        )

        # Result is sorted: tags first (descending), then branches alpha
        tag_names = [r.name for r in result if r.ref_type == GitReferenceType.TAG]
        branch_names = [r.name for r in result if r.ref_type == GitReferenceType.BRANCH]
        assert tag_names == ["v2.0.0", "v1.0.0", "v0.9.0"]
        assert branch_names == ["feature/xyz", "main"]

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_generic_host_no_token(self, MockGitCmd):
        """Generic host without token relaxes env (removes GIT_ASKPASS etc.)."""
        dl = _build_downloader()
        dep = _make_dep_ref(host="gitlab.example.com")

        dl._resolve_dep_token = MagicMock(return_value=None)
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(return_value="https://gitlab.example.com/owner/repo.git")
        # Ensure git_env has the keys that should be removed
        dl.git_env["GIT_ASKPASS"] = "echo"
        dl.git_env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        dl.git_env["GIT_CONFIG_NOSYSTEM"] = "1"

        mock_git = MockGitCmd.return_value
        mock_git.ls_remote.return_value = SAMPLE_LS_REMOTE

        dl.list_remote_refs(dep)

        call_kwargs = mock_git.ls_remote.call_args
        used_env = call_kwargs.kwargs.get("env")
        assert "GIT_ASKPASS" not in used_env
        assert "GIT_CONFIG_GLOBAL" not in used_env
        assert "GIT_CONFIG_NOSYSTEM" not in used_env
        assert used_env.get("GIT_TERMINAL_PROMPT") == "0"

    @pytest.mark.windows_compat
    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_insecure_http_host_no_token_suppresses_credential_helpers(self, MockGitCmd):
        """HTTP ls-remote must block credential helpers and preserve config isolation."""
        dl = _build_downloader()
        dep = _make_dep_ref(host="gitlab.example.com")
        dep.is_insecure = True

        dl._resolve_dep_token = MagicMock(return_value=None)
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(return_value="http://gitlab.example.com/owner/repo.git")
        dl.git_env["GIT_ASKPASS"] = "echo"
        dl.git_env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        dl.git_env["GIT_CONFIG_NOSYSTEM"] = "1"

        mock_git = MockGitCmd.return_value
        mock_git.ls_remote.return_value = SAMPLE_LS_REMOTE

        dl.list_remote_refs(dep)

        call_kwargs = mock_git.ls_remote.call_args
        used_env = call_kwargs.kwargs.get("env")
        assert used_env.get("GIT_ASKPASS") == "echo"
        global_config = Path(used_env["GIT_CONFIG_GLOBAL"])
        if os.name == "nt":
            assert global_config != Path(os.devnull)
            assert global_config.is_file()
            assert global_config.read_bytes() == b""
        else:
            assert global_config == Path(os.devnull)
        assert used_env.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert used_env.get("GIT_CONFIG_COUNT") == "1"
        assert used_env.get("GIT_CONFIG_KEY_0") == "credential.helper"
        assert used_env.get("GIT_CONFIG_VALUE_0") == ""
        assert used_env.get("GIT_TERMINAL_PROMPT") == "0"

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_git_command_error_raises_runtime_error(self, MockGitCmd):
        """GitCommandError is wrapped in RuntimeError with auth context."""
        dl = _build_downloader()
        dep = _make_dep_ref(host="github.com")

        dl._resolve_dep_token = MagicMock(return_value="ghp_token")
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(return_value="https://github.com/owner/repo.git")

        mock_git = MockGitCmd.return_value
        mock_git.ls_remote.side_effect = GitCommandError("ls-remote", 128)

        with pytest.raises(RuntimeError, match="Failed to list remote refs"):
            dl.list_remote_refs(dep)

        dl.auth_resolver.build_error_context.assert_called_once()

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_deref_tags_use_commit_sha(self, MockGitCmd):
        """Annotated tags use the commit SHA from the ^{} line."""
        dl = _build_downloader()
        dep = _make_dep_ref(host="github.com")

        dl._resolve_dep_token = MagicMock(return_value="tok")
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(return_value="https://github.com/owner/repo.git")

        mock_git = MockGitCmd.return_value
        mock_git.ls_remote.return_value = SAMPLE_LS_REMOTE_WITH_DEREF

        result = dl.list_remote_refs(dep)
        tag_map = {r.name: r.commit_sha for r in result if r.ref_type == GitReferenceType.TAG}
        assert tag_map["v1.0.0"] == "com1111111111111111111111111111111111111"
        assert tag_map["v2.0.0"] == "com2222222222222222222222222222222222222"

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_malformed_tag_output_is_fatal(self, MockGitCmd):
        """A tag-only response cannot downgrade malformed transport output to no tags."""
        dl = _build_downloader()
        dep = _make_dep_ref(host="github.com")
        dl._resolve_dep_token = MagicMock(return_value="tok")
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(return_value="https://github.com/owner/repo.git")
        MockGitCmd.return_value.ls_remote.return_value = "not a git ref"

        with pytest.raises(RemoteRefParseError, match="Malformed git ls-remote tag output"):
            dl.list_remote_tag_refs(dep)

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_unsupported_tag_sha_width_is_fatal_before_tag_selection(self, MockGitCmd):
        """Unsupported remote hashes cannot become a nonfatal no-tag outcome."""
        dl = _build_downloader()
        dep = _make_dep_ref(host="github.com")
        dl._resolve_dep_token = MagicMock(return_value="tok")
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(return_value="https://github.com/owner/repo.git")
        MockGitCmd.return_value.ls_remote.return_value = f"{'a' * 64}\trefs/tags/not-a-release"

        with pytest.raises(RemoteRefParseError, match="Malformed git ls-remote tag output"):
            dl.list_remote_tag_refs(dep)


# ---------------------------------------------------------------------------
# list_remote_refs -- Azure DevOps (git ls-remote path)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auth token resolution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RemoteRef dataclass basics
# ---------------------------------------------------------------------------
