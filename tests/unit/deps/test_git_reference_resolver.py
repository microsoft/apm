"""Unit tests for ``apm_cli.deps.git_reference_resolver``.

Cover the resolver's decision tree in isolation by injecting a minimal
``_DownloaderContext`` stub. Avoids the heavyweight
``GitHubPackageDownloader`` setup; targets the seam introduced by the
extraction.

Tagged portability-by-manifest and secure-by-default. If the fallback
logic silently drifts, ``apm install`` resolves tags non-deterministically
with no automated signal.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from git.exc import GitCommandError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from apm_cli.deps.git_reference_resolver import GitReferenceResolver
from apm_cli.models.dependency.reference import DependencyReference

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _dep(
    *,
    host: str | None = None,
    repo_url: str = "owner/repo",
    ado: bool = False,
    artifactory: bool = False,
    reference: str | None = None,
):
    kwargs: dict = {"repo_url": repo_url, "host": host, "reference": reference}
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


def _ctx(
    *,
    token: str | None = None,
    auth_scheme: str = "basic",
    is_artifactory_proxy: bool = False,
    artifactory_base: tuple | None = None,
):
    """Build a stub host (downloader context) with the resolver's Protocol."""
    auth_ctx = (
        types.SimpleNamespace(
            auth_scheme=auth_scheme,
            git_env={"GIT_HTTP_EXTRAHEADER": "Authorization: Bearer jwt"},
        )
        if auth_scheme == "bearer"
        else None
    )

    auth_resolver = MagicMock()
    auth_resolver.classify_host.return_value = types.SimpleNamespace(
        host="generic.example.com", display_name="generic.example.com"
    )
    auth_resolver.build_error_context.return_value = "Check auth."
    auth_resolver._build_git_env.return_value = {
        "GIT_HTTP_EXTRAHEADER": "Authorization: Bearer jwt"
    }
    # Default: pass through primary op result (no bearer fallback).
    auth_resolver.execute_with_bearer_fallback.side_effect = (
        lambda dep_ref, primary, bearer, is_fail: types.SimpleNamespace(
            outcome=primary(), bearer_attempted=False
        )
    )

    host = MagicMock()
    host.auth_resolver = auth_resolver
    host.git_env = {"GIT_TERMINAL_PROMPT": "0"}
    host.shared_clone_cache = None
    host._resolve_dep_token.return_value = token
    host._resolve_dep_auth_ctx.return_value = auth_ctx
    host._build_noninteractive_git_env.return_value = {"GIT_TERMINAL_PROMPT": "0"}
    host._build_repo_url.return_value = "https://example.com/owner/repo.git"
    host._sanitize_git_error.side_effect = lambda s: s
    host._parse_artifactory_base_url.return_value = artifactory_base
    host._should_use_artifactory_proxy.return_value = is_artifactory_proxy
    host._parse_ls_remote_output.side_effect = lambda out: [
        types.SimpleNamespace(name=line.split("\t")[1], commit_sha=line.split("\t")[0])
        for line in out.strip().splitlines()
        if "\t" in line
    ]
    host._sort_remote_refs.side_effect = lambda refs: refs
    return host


# ---------------------------------------------------------------------------
# resolve_commit_sha_for_ref
# ---------------------------------------------------------------------------


class TestResolveCommitShaForRef:
    def test_artifactory_short_circuits_to_none(self):
        host = _ctx()
        resolver = GitReferenceResolver(host)
        assert resolver.resolve_commit_sha_for_ref(_dep(artifactory=True), "main") is None

    def test_ado_short_circuits_to_none(self):
        host = _ctx()
        resolver = GitReferenceResolver(host)
        assert resolver.resolve_commit_sha_for_ref(_dep(ado=True), "main") is None

    def test_full_sha_returned_lowercase(self):
        host = _ctx()
        resolver = GitReferenceResolver(host)
        sha = "AbC1234567890123456789012345678901234567"
        result = resolver.resolve_commit_sha_for_ref(_dep(host="github.com"), sha)
        assert result == sha.lower()

    def test_tag_resolved_via_commits_api(self):
        host = _ctx()
        response = MagicMock()
        response.status_code = 200
        response.text = "deadbeef" + "0" * 32  # 40 hex chars
        host._resilient_get.return_value = response
        resolver = GitReferenceResolver(host)
        result = resolver.resolve_commit_sha_for_ref(
            _dep(host="github.com", repo_url="owner/repo"), "v1.0.0"
        )
        assert result == "deadbeef" + "0" * 32
        host._resilient_get.assert_called_once()

    def test_404_returns_none(self):
        host = _ctx()
        response = MagicMock()
        response.status_code = 404
        response.text = "Not Found"
        host._resilient_get.return_value = response
        resolver = GitReferenceResolver(host)
        assert resolver.resolve_commit_sha_for_ref(_dep(host="github.com"), "nonexistent") is None

    def test_unexpected_body_returns_none(self):
        host = _ctx()
        response = MagicMock()
        response.status_code = 200
        response.text = "<html>not a sha</html>"
        host._resilient_get.return_value = response
        resolver = GitReferenceResolver(host)
        assert resolver.resolve_commit_sha_for_ref(_dep(host="github.com"), "v1") is None

    def test_network_exception_returns_none(self):
        host = _ctx()
        host._resilient_get.side_effect = RuntimeError("network down")
        resolver = GitReferenceResolver(host)
        assert resolver.resolve_commit_sha_for_ref(_dep(host="github.com"), "v1") is None

    def test_no_commits_api_returns_none(self):
        # Generic backend returns None for build_commits_api_url
        host = _ctx()
        resolver = GitReferenceResolver(host)
        # Use a generic-host dep -- generic backend returns None for commits API
        assert resolver.resolve_commit_sha_for_ref(_dep(host="git.example.com"), "main") is None


# ---------------------------------------------------------------------------
# list_remote_refs
# ---------------------------------------------------------------------------


class TestListRemoteRefs:
    SAMPLE = (
        "aaa1111111111111111111111111111111111111\trefs/heads/main\n"
        "bbb2222222222222222222222222222222222222\trefs/tags/v1.0.0\n"
    )

    def test_artifactory_returns_empty_list(self):
        host = _ctx()
        resolver = GitReferenceResolver(host)
        assert resolver.list_remote_refs(_dep(artifactory=True)) == []

    def test_successful_ls_remote(self):
        host = _ctx(token="ghp_xxx")
        with patch("apm_cli.deps.github_downloader.git.cmd.Git") as MockGit:
            MockGit.return_value.ls_remote.return_value = self.SAMPLE
            resolver = GitReferenceResolver(host)
            refs = resolver.list_remote_refs(_dep(host="github.com"))
        assert len(refs) == 2
        assert refs[0].name == "refs/heads/main"

    def test_git_command_error_raises_runtime_error(self):
        host = _ctx(token="ghp_xxx")
        with patch("apm_cli.deps.github_downloader.git.cmd.Git") as MockGit:
            MockGit.return_value.ls_remote.side_effect = GitCommandError(
                "ls-remote", 128, b"Authentication failed"
            )
            resolver = GitReferenceResolver(host)
            with pytest.raises(RuntimeError, match=r"Failed to list remote refs"):
                resolver.list_remote_refs(_dep(host="github.com"))

    def test_ado_basic_with_token_uses_bearer_fallback(self):
        host = _ctx(token="ado_pat", auth_scheme="basic")
        # Wire bearer fallback to be invoked: primary fails with auth signal,
        # bearer succeeds.
        bearer_outcome = ("ok", self.SAMPLE)

        def _fallback(dep_ref, primary, bearer, is_fail):
            primary()  # call but ignore -- simulate auth failure
            return types.SimpleNamespace(outcome=bearer_outcome, bearer_attempted=True)

        host.auth_resolver.execute_with_bearer_fallback.side_effect = _fallback

        with patch("apm_cli.deps.github_downloader.git.cmd.Git") as MockGit:
            MockGit.return_value.ls_remote.return_value = self.SAMPLE
            resolver = GitReferenceResolver(host)
            refs = resolver.list_remote_refs(_dep(ado=True))
        # Bearer path returned the parsed sample.
        assert len(refs) == 2
        host.auth_resolver.execute_with_bearer_fallback.assert_called_once()

    def test_unauthenticated_uses_noninteractive_env(self):
        host = _ctx(token=None)
        with patch("apm_cli.deps.github_downloader.git.cmd.Git") as MockGit:
            MockGit.return_value.ls_remote.return_value = self.SAMPLE
            resolver = GitReferenceResolver(host)
            resolver.list_remote_refs(_dep(host="github.com"))
        host._build_noninteractive_git_env.assert_called_once()

    def test_error_message_sanitized(self):
        host = _ctx(token="ghp_xxx")
        host._sanitize_git_error.side_effect = lambda s: "[REDACTED] sanitized"
        with patch("apm_cli.deps.github_downloader.git.cmd.Git") as MockGit:
            MockGit.return_value.ls_remote.side_effect = GitCommandError(
                "ls-remote", 128, b"Authentication failed: ghp_secret_token"
            )
            resolver = GitReferenceResolver(host)
            with pytest.raises(RuntimeError, match=r"\[REDACTED\] sanitized"):
                resolver.list_remote_refs(_dep(host="github.com"))


# ---------------------------------------------------------------------------
# resolve (clone-and-introspect path)
# ---------------------------------------------------------------------------


class TestResolveArtifactoryShortCircuit:
    def test_artifactory_returns_branch_resolution_without_clone(self):
        host = _ctx()
        resolver = GitReferenceResolver(host)
        result = resolver.resolve(_dep(artifactory=True, reference="main"))
        assert result.ref_name == "main"
        assert result.resolved_commit is None
        # Never tried to clone.
        host._clone_with_fallback.assert_not_called()

    def test_artifactory_proxy_returns_branch_resolution_without_clone(self):
        host = _ctx(is_artifactory_proxy=True, artifactory_base=("a.example.com", "p", "https"))
        resolver = GitReferenceResolver(host)
        result = resolver.resolve(_dep(host="github.com", reference="v1.0.0"))
        assert result.ref_name == "v1.0.0"
        assert result.resolved_commit is None
        host._clone_with_fallback.assert_not_called()

    def test_artifactory_with_sha_classified_as_commit_type(self):
        host = _ctx()
        sha = "abc1234"  # short SHA still matches commit-shaped regex
        result = GitReferenceResolver(host).resolve(_dep(artifactory=True, reference=sha))
        assert result.ref_name == sha
