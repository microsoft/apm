"""Tests for GitHubPackageDownloader.list_remote_refs() and helpers (filesystem, process and CLI boundary cases)."""

import os
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch  # noqa: F401

import pytest

from apm_cli.core.auth import AuthResolver
from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.dependency.reference import DependencyReference

pytestmark = pytest.mark.component

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


# ---------------------------------------------------------------------------
# list_remote_refs -- Azure DevOps (git ls-remote path)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auth token resolution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# RemoteRef dataclass basics
# ---------------------------------------------------------------------------
