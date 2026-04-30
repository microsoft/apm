"""Regression tests for issue #798 -- is_generic error paths must render host:port.

When a non-GitHub/non-ADO host fails during ``_clone_with_fallback`` or
``list_remote_refs``, the diagnostic hint ``"For private repositories on
{host}, ..."`` previously rendered the bare host and dropped the port.
Users on Bitbucket Datacenter (custom port 7999) or any self-hosted host
on a non-default port saw a message that hid the very detail they
needed to verify their credentials against.

The fix routes both call sites through
``AuthResolver.classify_host(...).display_name``, so the generic branch
shares port rendering with the adjacent ADO / auth branches (which
already used ``build_error_context`` -> ``host_info.display_name``).
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from git.exc import GitCommandError

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference


def _make_downloader():
    """Build a GitHubPackageDownloader with a real AuthResolver.

    The real resolver is required here: the regression is specifically
    about ``classify_host`` feeding ``HostInfo.display_name`` into the
    rendered error message. A mocked resolver would bypass the exact
    integration under test.
    """
    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
            return_value=None,
        ),
    ):
        dl = GitHubPackageDownloader()
    dl.auth_resolver._cache.clear()
    dl._resolve_dep_token = MagicMock(return_value=None)
    return dl


def _diagnostic_prefix(msg: str) -> str:
    """Strip the `` Last error: ...`` tail so assertions only see our hint.

    The sanitized git-command text can echo arbitrary hostnames; we
    don't want a bare-host regression hiding behind that noise.
    """
    return msg.split(" Last error:", 1)[0]


class TestGenericHostCloneErrorPort:
    """``_clone_with_fallback`` generic branch (github_downloader.py ~L864)."""

    def _clone_error(self, dep) -> str:
        dl = _make_downloader()

        def _fake_clone(*_args, **_kwargs):
            raise GitCommandError("clone", 128)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.Repo") as MockRepo,
        ):
            MockRepo.clone_from.side_effect = _fake_clone
            target = Path(tempfile.mkdtemp())
            try:
                with pytest.raises(RuntimeError) as exc_info:
                    dl._clone_with_fallback(dep.repo_url, target, dep_ref=dep)
                return str(exc_info.value)
            finally:
                shutil.rmtree(target, ignore_errors=True)

    def test_ssh_custom_port_surfaces_in_error(self):
        """Bitbucket-DC-style ssh://host:7999/... -> hint names host:7999."""
        dep = DependencyReference.parse("ssh://git@bitbucket.example.com:7999/project/repo.git")
        assert dep.port == 7999

        prefix = _diagnostic_prefix(self._clone_error(dep))
        assert "For private repositories on bitbucket.example.com:7999," in prefix

    def test_https_custom_port_surfaces_in_error(self):
        """https://host:7990/... -> hint names host:7990."""
        dep = DependencyReference.parse("https://bitbucket.example.com:7990/project/repo.git")
        assert dep.port == 7990

        prefix = _diagnostic_prefix(self._clone_error(dep))
        assert "For private repositories on bitbucket.example.com:7990," in prefix

    def test_no_port_renders_bare_host(self):
        """Default-port dep has no port suffix -- no regression for common case."""
        dep = DependencyReference.parse("https://gitlab.example.com/team/repo.git")
        assert dep.port is None

        prefix = _diagnostic_prefix(self._clone_error(dep))
        assert "For private repositories on gitlab.example.com," in prefix
        assert "gitlab.example.com:" not in prefix, (
            f"bare-host case must not synthesise a stray ':' suffix: {prefix!r}"
        )


class TestGenericHostLsRemoteErrorPort:
    """``list_remote_refs`` generic branch (github_downloader.py ~L1035)."""

    def _ls_remote_error(self, dep) -> str:
        dl = _make_downloader()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "apm_cli.core.token_manager.GitHubTokenManager.resolve_credential_from_git",
                return_value=None,
            ),
            patch("apm_cli.deps.github_downloader.git.cmd.Git") as MockGitCmd,
        ):
            mock_git = MockGitCmd.return_value
            mock_git.ls_remote.side_effect = GitCommandError("ls-remote", 128)
            with pytest.raises(RuntimeError) as exc_info:
                dl.list_remote_refs(dep)
            return str(exc_info.value)

    def test_ssh_custom_port_surfaces_in_error(self):
        dep = DependencyReference.parse("ssh://git@bitbucket.example.com:7999/project/repo.git")
        assert dep.port == 7999

        prefix = _diagnostic_prefix(self._ls_remote_error(dep))
        assert "For private repositories on bitbucket.example.com:7999," in prefix

    def test_https_custom_port_surfaces_in_error(self):
        dep = DependencyReference.parse("https://bitbucket.example.com:7990/project/repo.git")
        assert dep.port == 7990

        prefix = _diagnostic_prefix(self._ls_remote_error(dep))
        assert "For private repositories on bitbucket.example.com:7990," in prefix

    def test_no_port_renders_bare_host(self):
        dep = DependencyReference.parse("https://gitlab.example.com/team/repo.git")
        assert dep.port is None

        prefix = _diagnostic_prefix(self._ls_remote_error(dep))
        assert "For private repositories on gitlab.example.com," in prefix
        assert "gitlab.example.com:" not in prefix, (
            f"bare-host case must not synthesise a stray ':' suffix: {prefix!r}"
        )
