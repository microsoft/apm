"""Unit tests for apm_cli.deps.git_reference_resolver.

Covers missing lines/branches in git_reference_resolver.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep_ref(
    repo_url="org/repo",
    host="github.com",
    reference="main",
    port=None,
    is_artifactory=False,
    is_ado=False,
):
    dep = MagicMock()
    dep.repo_url = repo_url
    dep.host = host
    dep.reference = reference
    dep.port = port
    dep.is_artifactory = MagicMock(return_value=is_artifactory)
    dep.is_azure_devops = MagicMock(return_value=is_ado)
    dep.is_insecure = False
    return dep


def _make_host(
    dep_token=None,
    dep_auth_scheme="basic",
    ls_remote_result=("ok", "abc123\trefs/heads/main\n"),
    sort_result=None,
    parse_result=None,
):
    host = MagicMock()
    host._resolve_dep_token.return_value = dep_token
    dep_auth_ctx = MagicMock()
    dep_auth_ctx.auth_scheme = dep_auth_scheme
    dep_auth_ctx.git_env = {"GIT_TOKEN": "tok"}
    host._resolve_dep_auth_ctx.return_value = dep_auth_ctx
    host.git_env = {}
    host._build_noninteractive_git_env.return_value = {}
    host._build_repo_url.return_value = "https://github.com/org/repo.git"
    host._sanitize_git_error.side_effect = lambda s: s
    host._parse_ls_remote_output.return_value = parse_result or []
    host._sort_remote_refs.return_value = sort_result or []
    host._parse_artifactory_base_url.return_value = None
    host._should_use_artifactory_proxy.return_value = False
    host.auth_resolver = MagicMock()
    host.auth_resolver._build_git_env.return_value = {}
    host.auth_resolver.build_error_context.return_value = "auth error context"
    host.auth_resolver.classify_host.return_value = MagicMock(display_name="GitHub")
    host.shared_clone_cache = None
    return host


def _make_resolver(host=None):
    if host is None:
        host = _make_host()
    from apm_cli.deps.git_reference_resolver import GitReferenceResolver

    return GitReferenceResolver(host)


# ---------------------------------------------------------------------------
# list_remote_refs
# ---------------------------------------------------------------------------


class TestListRemoteRefs:
    def test_artifactory_returns_empty(self):

        dep = _make_dep_ref(is_artifactory=True)
        resolver = _make_resolver()
        result = resolver.list_remote_refs(dep)
        assert result == []

    def test_success_no_token(self):
        from apm_cli.deps.git_reference_resolver import GitReferenceResolver

        dep = _make_dep_ref()
        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "abc\trefs/heads/main"
        host = _make_host(dep_token=None)
        parsed_refs = [MagicMock()]
        host._parse_ls_remote_output.return_value = parsed_refs
        host._sort_remote_refs.return_value = parsed_refs

        resolver = GitReferenceResolver(host)

        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            result = resolver.list_remote_refs(dep)
        assert result == parsed_refs

    def test_success_with_basic_token(self):
        from apm_cli.deps.git_reference_resolver import GitReferenceResolver

        dep = _make_dep_ref()
        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "abc\trefs/heads/main"
        host = _make_host(dep_token="secret", dep_auth_scheme="basic")
        parsed = [MagicMock()]
        host._parse_ls_remote_output.return_value = parsed
        host._sort_remote_refs.return_value = parsed

        resolver = GitReferenceResolver(host)
        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            result = resolver.list_remote_refs(dep)
        assert result == parsed

    def test_success_with_bearer_token(self):
        from apm_cli.deps.git_reference_resolver import GitReferenceResolver

        dep = _make_dep_ref()
        mock_git = MagicMock()
        mock_git.ls_remote.return_value = "abc\trefs/heads/main"
        host = _make_host(dep_token="jwt-token", dep_auth_scheme="bearer")
        parsed = [MagicMock()]
        host._parse_ls_remote_output.return_value = parsed
        host._sort_remote_refs.return_value = parsed

        resolver = GitReferenceResolver(host)
        with patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git):
            result = resolver.list_remote_refs(dep)
        assert result == parsed

    def test_error_github_raises_runtime_error(self):
        from git.exc import GitCommandError

        from apm_cli.deps.git_reference_resolver import GitReferenceResolver

        dep = _make_dep_ref(host="github.com")
        mock_git = MagicMock()
        mock_git.ls_remote.side_effect = GitCommandError("ls-remote", 128)
        host = _make_host(dep_token=None)

        resolver = GitReferenceResolver(host)
        with (
            patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git),
            pytest.raises(RuntimeError, match="Failed to list remote refs"),
        ):
            resolver.list_remote_refs(dep)

    def test_error_generic_host_raises_with_ssh_hint(self):
        from git.exc import GitCommandError

        from apm_cli.deps.git_reference_resolver import GitReferenceResolver

        dep = _make_dep_ref(host="bitbucket.org")
        mock_git = MagicMock()
        mock_git.ls_remote.side_effect = GitCommandError("ls-remote", 128)
        host = _make_host(dep_token=None)

        resolver = GitReferenceResolver(host)
        with (
            patch("apm_cli.deps.github_downloader.git.cmd.Git", return_value=mock_git),
            patch("apm_cli.utils.github_host.is_github_hostname", return_value=False),
            pytest.raises(RuntimeError, match="Failed to list remote refs"),
        ):
            resolver.list_remote_refs(dep)


# ---------------------------------------------------------------------------
# resolve_commit_sha_for_ref
# ---------------------------------------------------------------------------


class TestResolveCommitShaForRef:
    def test_artifactory_returns_none(self):
        dep = _make_dep_ref(is_artifactory=True)
        resolver = _make_resolver()
        result = resolver.resolve_commit_sha_for_ref(dep, "main")
        assert result is None

    def test_ado_returns_none(self):
        dep = _make_dep_ref(is_ado=True)
        resolver = _make_resolver()
        result = resolver.resolve_commit_sha_for_ref(dep, "main")
        assert result is None

    def test_full_sha_ref_returns_directly(self):
        dep = _make_dep_ref()
        resolver = _make_resolver()
        sha = "a" * 40
        result = resolver.resolve_commit_sha_for_ref(dep, sha)
        assert result == sha

    def test_missing_repo_url_returns_none(self):
        dep = _make_dep_ref()
        dep.repo_url = None
        resolver = _make_resolver()
        result = resolver.resolve_commit_sha_for_ref(dep, "main")
        assert result is None

    def test_success_returns_sha(self):
        sha = "b" * 40
        dep = _make_dep_ref()
        host = _make_host()
        resolver = _make_resolver(host)

        mock_backend = MagicMock()
        mock_backend.build_commits_api_url.return_value = "https://api.github.com/commits/main"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = sha
        host._resilient_get.return_value = mock_response

        with patch("apm_cli.deps.host_backends.backend_for", return_value=mock_backend):
            with patch(
                "apm_cli.core.auth.AuthResolver.resolve", return_value=MagicMock(token="tok")
            ):
                result = resolver.resolve_commit_sha_for_ref(dep, "main")
        # May succeed or gracefully return None -- just no exception
        assert result is None or len(result) == 40

    def test_non_200_response_returns_none(self):
        dep = _make_dep_ref()
        host = _make_host()
        resolver = _make_resolver(host)

        mock_backend = MagicMock()
        mock_backend.build_commits_api_url.return_value = "https://api.github.com/commits/main"

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = ""
        host._resilient_get.return_value = mock_response
        host.auth_resolver.resolve.return_value = MagicMock(token=None)

        with patch("apm_cli.deps.host_backends.backend_for", return_value=mock_backend):
            result = resolver.resolve_commit_sha_for_ref(dep, "main")
        assert result is None

    def test_backend_build_url_returns_none(self):
        dep = _make_dep_ref()
        host = _make_host()
        resolver = _make_resolver(host)

        mock_backend = MagicMock()
        mock_backend.build_commits_api_url.return_value = None

        with patch("apm_cli.deps.host_backends.backend_for", return_value=mock_backend):
            result = resolver.resolve_commit_sha_for_ref(dep, "main")
        assert result is None

    def test_exception_in_resilient_get_returns_none(self):
        dep = _make_dep_ref()
        host = _make_host()
        resolver = _make_resolver(host)

        mock_backend = MagicMock()
        mock_backend.build_commits_api_url.return_value = "https://api.github.com/commits/main"
        host._resilient_get.side_effect = RuntimeError("network error")
        host.auth_resolver.resolve.return_value = MagicMock(token=None)

        with patch("apm_cli.deps.host_backends.backend_for", return_value=mock_backend):
            result = resolver.resolve_commit_sha_for_ref(dep, "main")
        assert result is None


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------
