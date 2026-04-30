"""Tests for GitHubPackageDownloader.list_remote_refs() and helpers."""

from unittest.mock import MagicMock, PropertyMock, patch  # noqa: F401

import pytest
from git.exc import GitCommandError

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.types import GitReferenceType, RemoteRef

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
        downloader = GitHubPackageDownloader(auth_resolver=mock_auth)
    return downloader


# ---------------------------------------------------------------------------
# _parse_ls_remote_output
# ---------------------------------------------------------------------------


class TestParseLsRemoteOutput:
    """Tests for the static ls-remote parser."""

    def test_tags_and_branches(self):
        refs = GitHubPackageDownloader._parse_ls_remote_output(SAMPLE_LS_REMOTE)
        names = {r.name for r in refs}
        assert "main" in names
        assert "feature/xyz" in names
        assert "v1.0.0" in names
        assert "v2.0.0" in names
        assert "v0.9.0" in names

        tag_refs = [r for r in refs if r.ref_type == GitReferenceType.TAG]
        branch_refs = [r for r in refs if r.ref_type == GitReferenceType.BRANCH]
        assert len(tag_refs) == 3
        assert len(branch_refs) == 2

    def test_deref_handling(self):
        """^{} lines should override tag-object SHAs with the commit SHA."""
        refs = GitHubPackageDownloader._parse_ls_remote_output(SAMPLE_LS_REMOTE_WITH_DEREF)
        tag_map = {r.name: r.commit_sha for r in refs if r.ref_type == GitReferenceType.TAG}
        # The ^{} commit SHA should be used, not the tag object SHA
        assert tag_map["v1.0.0"] == "com1111111111111111111111111111111111111"
        assert tag_map["v2.0.0"] == "com2222222222222222222222222222222222222"
        # No ^{} entry should appear as a separate ref
        assert "v1.0.0^{}" not in tag_map
        assert "v2.0.0^{}" not in tag_map

    def test_empty_output(self):
        assert GitHubPackageDownloader._parse_ls_remote_output("") == []

    def test_blank_lines_ignored(self):
        output = "\n\naaa1111111111111111111111111111111111111\trefs/heads/main\n\n"
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        assert len(refs) == 1
        assert refs[0].name == "main"

    def test_malformed_lines_skipped(self):
        output = "no-tab-here\naaa1111111111111111111111111111111111111\trefs/heads/main\n"
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        assert len(refs) == 1

    def test_tag_without_deref(self):
        """A lightweight tag (no ^{} line) keeps its own SHA."""
        output = "abc1234567890123456789012345678901234567\trefs/tags/v3.0.0\n"
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        assert len(refs) == 1
        assert refs[0].commit_sha == "abc1234567890123456789012345678901234567"

    def test_mixed_semver_and_non_semver_tags_parsed(self):
        """Parser correctly identifies both semver and non-semver tag names."""
        output = (
            "aaa1111111111111111111111111111111111111\trefs/tags/v1.0.0\n"
            "bbb2222222222222222222222222222222222222\trefs/tags/latest\n"
            "ccc3333333333333333333333333333333333333\trefs/tags/stable\n"
            "ddd4444444444444444444444444444444444444\trefs/heads/main\n"
        )
        refs = GitHubPackageDownloader._parse_ls_remote_output(output)
        names = {r.name for r in refs}
        assert "v1.0.0" in names
        assert "latest" in names
        assert "stable" in names
        assert "main" in names
        tag_refs = [r for r in refs if r.ref_type == GitReferenceType.TAG]
        assert len(tag_refs) == 3


# ---------------------------------------------------------------------------
# _semver_sort_key / _sort_remote_refs
# ---------------------------------------------------------------------------


class TestSorting:
    """Tests for semver sorting logic."""

    def test_semver_descending(self):
        refs = [
            RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="a"),
            RemoteRef(name="v2.0.0", ref_type=GitReferenceType.TAG, commit_sha="b"),
            RemoteRef(name="v1.5.0", ref_type=GitReferenceType.TAG, commit_sha="c"),
        ]
        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        names = [r.name for r in sorted_refs]
        assert names == ["v2.0.0", "v1.5.0", "v1.0.0"]

    def test_tags_before_branches(self):
        refs = [
            RemoteRef(name="main", ref_type=GitReferenceType.BRANCH, commit_sha="a"),
            RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="b"),
        ]
        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        assert sorted_refs[0].ref_type == GitReferenceType.TAG
        assert sorted_refs[1].ref_type == GitReferenceType.BRANCH

    def test_branches_alphabetical(self):
        refs = [
            RemoteRef(name="develop", ref_type=GitReferenceType.BRANCH, commit_sha="a"),
            RemoteRef(name="alpha", ref_type=GitReferenceType.BRANCH, commit_sha="b"),
            RemoteRef(name="main", ref_type=GitReferenceType.BRANCH, commit_sha="c"),
        ]
        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        names = [r.name for r in sorted_refs]
        assert names == ["alpha", "develop", "main"]

    def test_non_semver_tags_after_semver(self):
        refs = [
            RemoteRef(name="nightly", ref_type=GitReferenceType.TAG, commit_sha="a"),
            RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="b"),
        ]
        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        names = [r.name for r in sorted_refs]
        assert names == ["v1.0.0", "nightly"]

    def test_semver_without_v_prefix(self):
        refs = [
            RemoteRef(name="1.0.0", ref_type=GitReferenceType.TAG, commit_sha="a"),
            RemoteRef(name="2.0.0", ref_type=GitReferenceType.TAG, commit_sha="b"),
        ]
        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        names = [r.name for r in sorted_refs]
        assert names == ["2.0.0", "1.0.0"]

    def test_semver_with_prerelease(self):
        refs = [
            RemoteRef(name="v1.0.0-beta", ref_type=GitReferenceType.TAG, commit_sha="a"),
            RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="b"),
            RemoteRef(name="v1.0.0-alpha", ref_type=GitReferenceType.TAG, commit_sha="c"),
        ]
        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        names = [r.name for r in sorted_refs]
        # Same major.minor.patch, prerelease suffixes sorted alphabetically
        assert names[0] == "v1.0.0"  # empty suffix sorts before "-alpha", "-beta"

    def test_empty_list(self):
        assert GitHubPackageDownloader._sort_remote_refs([]) == []

    def test_named_non_semver_tags_latest_stable(self):
        """Common symbolic tag names ('latest', 'stable') sort after semver tags."""
        refs = [
            RemoteRef(name="latest", ref_type=GitReferenceType.TAG, commit_sha="a"),
            RemoteRef(name="stable", ref_type=GitReferenceType.TAG, commit_sha="b"),
            RemoteRef(name="v2.0.0", ref_type=GitReferenceType.TAG, commit_sha="c"),
            RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="d"),
        ]
        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        # Semver tags must appear before non-semver tags
        names = [r.name for r in sorted_refs]
        assert names.index("v2.0.0") < names.index("latest")
        assert names.index("v2.0.0") < names.index("stable")
        assert names.index("v1.0.0") < names.index("latest")
        assert names.index("v1.0.0") < names.index("stable")

    def test_all_non_semver_tags_sorted_alphabetically(self):
        """When no semver tags exist, non-semver tags are sorted alphabetically."""
        refs = [
            RemoteRef(name="stable", ref_type=GitReferenceType.TAG, commit_sha="a"),
            RemoteRef(name="latest", ref_type=GitReferenceType.TAG, commit_sha="b"),
            RemoteRef(name="edge", ref_type=GitReferenceType.TAG, commit_sha="c"),
        ]
        sorted_refs = GitHubPackageDownloader._sort_remote_refs(refs)
        names = [r.name for r in sorted_refs]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# list_remote_refs -- Artifactory
# ---------------------------------------------------------------------------


class TestListRemoteRefsArtifactory:
    """Artifactory dependencies should return an empty list."""

    def test_returns_empty(self):
        dl = _build_downloader()
        dep = _make_dep_ref(artifactory=True)
        result = dl.list_remote_refs(dep)
        assert result == []


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
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(
            return_value="https://x-access-token:ghp_test_token@github.com/owner/repo.git"
        )

        mock_git = MockGitCmd.return_value
        mock_git.ls_remote.return_value = SAMPLE_LS_REMOTE

        result = dl.list_remote_refs(dep)

        dl._resolve_dep_token.assert_called_once_with(dep)
        dl._build_repo_url.assert_called_once_with(
            "owner/repo",
            use_ssh=False,
            dep_ref=dep,
            token="ghp_test_token",
            auth_scheme="basic",
        )
        mock_git.ls_remote.assert_called_once()
        # Env should be the locked-down git_env (token present)
        call_kwargs = mock_git.ls_remote.call_args
        assert call_kwargs.kwargs.get("env") is dl.git_env

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
        assert used_env.get("GIT_CONFIG_GLOBAL") == "/dev/null"
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


# ---------------------------------------------------------------------------
# list_remote_refs -- Azure DevOps (git ls-remote path)
# ---------------------------------------------------------------------------


class TestListRemoteRefsADO:
    """Tests for ADO deps going through the unified git ls-remote path."""

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_ado_uses_git_ls_remote(self, MockGitCmd):
        """ADO deps use git ls-remote, not a REST API call."""
        dl = _build_downloader()
        dep = _make_dep_ref(ado=True)

        dl._resolve_dep_token = MagicMock(return_value="ado_pat_token")
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(
            return_value="https://ado_pat_token@dev.azure.com/myorg/myproj/_git/myrepo",
        )

        mock_git = MockGitCmd.return_value
        mock_git.ls_remote.return_value = SAMPLE_LS_REMOTE

        result = dl.list_remote_refs(dep)

        dl._resolve_dep_token.assert_called_once_with(dep)
        dl._build_repo_url.assert_called_once_with(
            "owner/repo",
            use_ssh=False,
            dep_ref=dep,
            token="ado_pat_token",
            auth_scheme="basic",
        )
        mock_git.ls_remote.assert_called_once()

        # Verify sorted output
        tag_names = [r.name for r in result if r.ref_type == GitReferenceType.TAG]
        branch_names = [r.name for r in result if r.ref_type == GitReferenceType.BRANCH]
        assert tag_names == ["v2.0.0", "v1.0.0", "v0.9.0"]
        assert branch_names == ["feature/xyz", "main"]

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_ado_git_error_raises_runtime_error(self, MockGitCmd):
        """ADO git ls-remote failure is wrapped in RuntimeError with auth context."""
        dl = _build_downloader()
        dep = _make_dep_ref(ado=True)

        dl._resolve_dep_token = MagicMock(return_value="ado_pat")
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(
            return_value="https://ado_pat@dev.azure.com/myorg/myproj/_git/myrepo",
        )

        mock_git = MockGitCmd.return_value
        mock_git.ls_remote.side_effect = GitCommandError("ls-remote", 128)

        with pytest.raises(RuntimeError, match="Failed to list remote refs"):
            dl.list_remote_refs(dep)

        dl.auth_resolver.build_error_context.assert_called_once()


# ---------------------------------------------------------------------------
# Auth token resolution
# ---------------------------------------------------------------------------


class TestAuthTokenResolution:
    """Verify correct token resolution per host type."""

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_github_host_resolves_token(self, MockGitCmd):
        dl = _build_downloader()
        dep = _make_dep_ref(host="github.com")
        dl._resolve_dep_token = MagicMock(return_value="ghp_tok")
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(return_value="https://github.com/o/r.git")
        MockGitCmd.return_value.ls_remote.return_value = ""

        dl.list_remote_refs(dep)

        dl._resolve_dep_token.assert_called_once_with(dep)

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_ado_host_resolves_token(self, MockGitCmd):
        dl = _build_downloader()
        dep = _make_dep_ref(ado=True)
        dl._resolve_dep_token = MagicMock(return_value="ado_tok")
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(
            return_value="https://ado_tok@dev.azure.com/myorg/myproj/_git/myrepo",
        )
        MockGitCmd.return_value.ls_remote.return_value = ""

        dl.list_remote_refs(dep)

        dl._resolve_dep_token.assert_called_once_with(dep)

    @patch("apm_cli.deps.github_downloader.git.cmd.Git")
    def test_generic_host_returns_none_token(self, MockGitCmd):
        dl = _build_downloader()
        dep = _make_dep_ref(host="gitlab.example.com")
        dl._resolve_dep_token = MagicMock(return_value=None)
        dl._resolve_dep_auth_ctx = MagicMock(return_value=None)
        dl._build_repo_url = MagicMock(return_value="https://gitlab.example.com/o/r.git")
        MockGitCmd.return_value.ls_remote.return_value = ""

        dl.list_remote_refs(dep)

        dl._resolve_dep_token.assert_called_once_with(dep)
        # _build_repo_url should receive token=None for generic hosts
        dl._build_repo_url.assert_called_once_with(
            "owner/repo",
            use_ssh=False,
            dep_ref=dep,
            token=None,
            auth_scheme="basic",
        )


# ---------------------------------------------------------------------------
# RemoteRef dataclass basics
# ---------------------------------------------------------------------------


class TestRemoteRefDataclass:
    """Smoke tests for the RemoteRef dataclass."""

    def test_fields(self):
        r = RemoteRef(name="v1.0.0", ref_type=GitReferenceType.TAG, commit_sha="abc")
        assert r.name == "v1.0.0"
        assert r.ref_type == GitReferenceType.TAG
        assert r.commit_sha == "abc"

    def test_equality(self):
        a = RemoteRef(name="main", ref_type=GitReferenceType.BRANCH, commit_sha="x")
        b = RemoteRef(name="main", ref_type=GitReferenceType.BRANCH, commit_sha="x")
        assert a == b

    def test_import_from_apm_package(self):
        """RemoteRef should be importable via the backward-compat re-export path."""
        from apm_cli.models.apm_package import RemoteRef as Imported

        assert Imported is RemoteRef
