"""Unit tests for --update auth pre-flight probe in pipeline.py (#1015)."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install.errors import AuthenticationError


def _make_dep(host="dev.azure.com", repo_url="myorg/myproject/_git/myrepo"):
    dep = MagicMock()
    dep.host = host
    dep.repo_url = repo_url
    dep.port = None
    dep.is_azure_devops.return_value = True
    dep.explicit_scheme = None
    dep.is_insecure = False
    dep.ado_organization = "myorg"
    dep.ado_project = "myproject"
    dep.ado_repo = "myrepo"
    return dep


def _make_ctx(update_refs=True, deps=None):
    ctx = MagicMock()
    ctx.deps_to_install = deps or [_make_dep()]
    ctx.update_refs = update_refs
    return ctx


def _make_resolver(auth_scheme="basic", token="pat", git_env=None):
    resolver = MagicMock()
    dep_ctx = MagicMock()
    dep_ctx.token = token
    dep_ctx.auth_scheme = auth_scheme
    dep_ctx.git_env = git_env or {}
    resolver.resolve_for_dep.return_value = dep_ctx
    resolver.build_error_context.return_value = "    Diagnostic payload"
    return resolver


class TestUpdatePreflightRejectsBadAuth:
    """Pre-flight raises AuthenticationError when git ls-remote returns 401."""

    @patch("subprocess.run")
    def test_auth_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: Authentication failed (401)",
            stdout="",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx()
        resolver = _make_resolver()

        with pytest.raises(AuthenticationError) as exc_info:
            _preflight_auth_check(ctx, resolver, verbose=False)

        assert "No files were modified" in exc_info.value.diagnostic_context
        assert "apm.yml" in exc_info.value.diagnostic_context

    @patch("subprocess.run")
    def test_auth_failure_message_mentions_host(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: unable to access (403)",
            stdout="",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx()
        resolver = _make_resolver()

        with pytest.raises(AuthenticationError) as exc_info:
            _preflight_auth_check(ctx, resolver, verbose=False)

        # Bounded full-phrase assertion (see CodeQL note in test_validation_ado_bearer.py).
        assert str(exc_info.value) == "Authentication failed for dev.azure.com"


class TestUpdatePreflightPassesGoodAuth:
    """Pre-flight succeeds when git ls-remote returns rc=0."""

    @patch("subprocess.run")
    def test_good_auth_no_exception(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stderr="", stdout="abc123\trefs/heads/main\n",
        )
        from apm_cli.install.pipeline import _preflight_auth_check

        ctx = _make_ctx()
        resolver = _make_resolver()

        # Should not raise
        _preflight_auth_check(ctx, resolver, verbose=False)


class TestPreflightSkippedForGitHubDeps:
    """github.com deps are skipped (they use the API probe with unauth fallback)."""

    @patch("subprocess.run")
    def test_github_deps_skipped(self, mock_run):
        dep = _make_dep(host="github.com", repo_url="owner/repo")
        ctx = _make_ctx(deps=[dep])
        resolver = _make_resolver()

        from apm_cli.install.pipeline import _preflight_auth_check

        _preflight_auth_check(ctx, resolver, verbose=False)
        mock_run.assert_not_called()


class TestPreflightClustersDeduplicate:
    """Multiple deps on the same (host, org) only probe once."""

    @patch("subprocess.run")
    def test_deduplication(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        dep1 = _make_dep(host="dev.azure.com", repo_url="myorg/projA/_git/repoA")
        dep2 = _make_dep(host="dev.azure.com", repo_url="myorg/projB/_git/repoB")
        ctx = _make_ctx(deps=[dep1, dep2])
        resolver = _make_resolver()

        from apm_cli.install.pipeline import _preflight_auth_check

        _preflight_auth_check(ctx, resolver, verbose=False)
        assert mock_run.call_count == 1
