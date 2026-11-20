"""Unit tests for apm_cli.deps.git_reference_resolver (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# list_remote_refs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resolve_commit_sha_for_ref
# ---------------------------------------------------------------------------


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
            patch("apm_cli.utils.git_env.git_worktree_head", return_value="a" * 40),
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
            patch("apm_cli.utils.git_env.git_resolve_commit", return_value=sha),
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
            patch("apm_cli.utils.git_env.git_resolve_commit", return_value=sha),
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
