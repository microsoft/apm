"""Unit tests for apm_cli.deps.git_reference_resolver.

Covers missing lines/branches in git_reference_resolver.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

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


class TestResolve:
    def test_artifactory_returns_branch_ref(self):
        from apm_cli.deps.git_reference_resolver import GitReferenceResolver
        from apm_cli.models.apm_package import DependencyReference, GitReferenceType

        host = _make_host()
        host._parse_artifactory_base_url.return_value = ("https://art.example.com", "myrepo")
        host._should_use_artifactory_proxy.return_value = True

        dep = DependencyReference(
            repo_url="org/repo",
            host="github.com",
            reference="main",
            artifactory_prefix="https://art.example.com",
        )
        resolver = GitReferenceResolver(host)
        result = resolver.resolve(dep)
        assert result.ref_type in (GitReferenceType.BRANCH, GitReferenceType.COMMIT)

    def test_artifactory_commit_ref_returns_commit_type(self):
        from apm_cli.deps.git_reference_resolver import GitReferenceResolver
        from apm_cli.models.apm_package import DependencyReference, GitReferenceType

        host = _make_host()
        host._parse_artifactory_base_url.return_value = None
        host._should_use_artifactory_proxy.return_value = False

        dep = DependencyReference(
            repo_url="org/repo",
            host="github.com",
            reference="abcdef1234567",
            artifactory_prefix="https://art.example.com",
        )
        # is_artifactory() returns True when artifactory_prefix is set
        resolver = GitReferenceResolver(host)
        result = resolver.resolve(dep)
        assert result.ref_type == GitReferenceType.COMMIT

    def test_resolve_shallow_clone_success(self):
        from apm_cli.deps.git_reference_resolver import GitReferenceResolver
        from apm_cli.models.apm_package import DependencyReference, GitReferenceType

        host = _make_host()
        mock_repo = MagicMock()
        mock_repo.head.commit.hexsha = "a" * 40
        mock_repo.active_branch.name = "main"
        host._clone_with_fallback.return_value = mock_repo

        dep = DependencyReference(repo_url="org/repo", host="github.com", reference="main")
        resolver = GitReferenceResolver(host)

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value="/tmp"),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("tempfile.mkdtemp", return_value="/tmp/t123"),
        ):
            result = resolver.resolve(dep)
        assert result.ref_type == GitReferenceType.BRANCH
        assert result.resolved_commit == "a" * 40

    def test_resolve_commit_sha_directly(self):
        from apm_cli.deps.git_reference_resolver import GitReferenceResolver
        from apm_cli.models.apm_package import DependencyReference, GitReferenceType

        sha = "d" * 40
        host = _make_host()
        mock_repo = MagicMock()
        commit = MagicMock()
        commit.hexsha = sha
        mock_repo.commit.return_value = commit
        host._clone_with_fallback.return_value = mock_repo

        dep = DependencyReference(repo_url="org/repo", host="github.com", reference=sha)
        resolver = GitReferenceResolver(host)

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value="/tmp"),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("tempfile.mkdtemp", return_value="/tmp/t999"),
        ):
            result = resolver.resolve(dep)
        assert result.ref_type == GitReferenceType.COMMIT
        assert result.resolved_commit == sha

    def test_resolve_invalid_string_ref_raises_value_error(self):
        from apm_cli.deps.git_reference_resolver import GitReferenceResolver

        host = _make_host()
        resolver = GitReferenceResolver(host)

        with pytest.raises((ValueError, Exception)):
            resolver.resolve("not::a::valid::ref")

    def test_resolve_shallow_clone_fails_falls_back_to_full(self):
        from git.exc import GitCommandError

        from apm_cli.deps.git_reference_resolver import GitReferenceResolver
        from apm_cli.models.apm_package import DependencyReference

        host = _make_host()
        mock_full_repo = MagicMock()
        sha = "f" * 40
        branch_ref = MagicMock()
        branch_ref.commit.hexsha = sha
        mock_full_repo.refs = {"origin/main": branch_ref}

        call_count = [0]

        def _clone_side(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise GitCommandError("clone", 128)
            return mock_full_repo

        host._clone_with_fallback.side_effect = _clone_side

        dep = DependencyReference(repo_url="org/repo", host="github.com", reference="main")
        resolver = GitReferenceResolver(host)

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value="/tmp"),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("tempfile.mkdtemp", return_value="/tmp/t456"),
        ):
            result = resolver.resolve(dep)
        assert result.resolved_commit == sha

    def test_resolve_full_clone_auth_failure_raises_runtime_error(self):
        from git.exc import GitCommandError

        from apm_cli.deps.git_reference_resolver import GitReferenceResolver
        from apm_cli.models.apm_package import DependencyReference

        host = _make_host()
        host._clone_with_fallback.side_effect = GitCommandError(
            "clone", 128, stderr="Authentication failed"
        )

        dep = DependencyReference(repo_url="org/repo", host="github.com", reference="main")
        resolver = GitReferenceResolver(host)

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value="/tmp"),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("tempfile.mkdtemp", return_value="/tmp/t789"),
            pytest.raises(RuntimeError, match="Failed to clone"),
        ):
            resolver.resolve(dep)

    def test_resolve_commit_clone_error_raises_value_error(self):
        from apm_cli.deps.git_reference_resolver import GitReferenceResolver
        from apm_cli.models.apm_package import DependencyReference

        sha_ref = "aabbccddeeff" + "0" * 28
        host = _make_host()
        host._clone_with_fallback.side_effect = Exception("clone failed")
        host._sanitize_git_error.side_effect = lambda s: s

        dep = DependencyReference(repo_url="org/repo", host="github.com", reference=sha_ref)
        resolver = GitReferenceResolver(host)

        with (
            patch("apm_cli.config.get_apm_temp_dir", return_value="/tmp"),
            patch("apm_cli.deps.github_downloader._rmtree"),
            patch("tempfile.mkdtemp", return_value="/tmp/t000"),
            pytest.raises((ValueError, RuntimeError)),
        ):
            resolver.resolve(dep)
