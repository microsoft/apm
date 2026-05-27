"""Tests for GitLab SSH validation honoring ``explicit_scheme`` (issue #1501).

Regression: ``apm install git@gitlab.com:owner/repo.git#ref`` raised
"Authentication failed for gitlab.com / No token available" when the user
had no GITLAB_APM_PAT/GITLAB_TOKEN configured but did have an SSH key.
The validator unconditionally probed authenticated HTTPS for GitLab hosts
and ignored ``explicit_scheme=='ssh'`` from the ``git@host:path`` SCP
shorthand. The clone path used by ``apm install`` from ``apm.yml`` honors
SSH transport directly, so the same dep installed successfully when
declared in ``apm.yml`` but failed via direct CLI invocation.

After the fix, an explicit SSH scheme (from ``ssh://`` or ``git@`` shorthand)
on a GitLab host probes the SSH URL, mirroring the explicit-ssh branch in
the generic-host arm of ``_validate_package_exists``.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install import validation
from apm_cli.models.apm_package import DependencyReference


def _make_gitlab_resolver():
    """Resolver mock returning a GitLab classification with no token."""
    resolver = MagicMock()
    host_info = MagicMock()
    host_info.api_base = "https://gitlab.com"
    host_info.display_name = "gitlab.com"
    host_info.kind = "gitlab"
    host_info.has_public_repos = False
    host_info.host = "gitlab.com"
    host_info.port = None
    resolver.classify_host.return_value = host_info
    ctx = MagicMock(
        source="none",
        token_type="unknown",
        token=None,
        auth_scheme="basic",
        git_env={},
    )
    resolver.resolve.return_value = ctx
    resolver.resolve_for_dep.return_value = ctx
    resolver.build_error_context.return_value = "No token available."
    return resolver


def _ok_run():
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout="abc\trefs/heads/main\n", stderr=""
    )


def _scheme_of(url: str) -> str:
    return url.split("://", 1)[0] if "://" in url else "ssh"


def _probe_urls(mock_run) -> list:
    return [call.args[0][-1] for call in mock_run.call_args_list]


class TestGitLabExplicitSshValidation:
    """GitLab dep with ``git@`` shorthand must probe SSH, not HTTPS-only."""

    @pytest.mark.parametrize(
        "dependency_str",
        [
            "git@gitlab.com:owner/repo.git#main",
            "ssh://git@gitlab.com/owner/repo.git#main",
        ],
        ids=["scp_shorthand", "ssh_scheme_url"],
    )
    def test_gitlab_git_at_shorthand_probes_ssh_url(self, monkeypatch, dependency_str: str) -> None:
        """``git@gitlab.com:owner/repo.git#ref`` validates via SSH.

        Before the fix, the GitLab branch of ``_validate_package_exists``
        unconditionally probed ``[https://gitlab.com/owner/repo]`` even
        when the user typed ``git@gitlab.com:...``. With no token, the
        probe failed and the validator raised ``AuthenticationError``.
        After the fix, the SSH URL is probed first when
        ``explicit_scheme == 'ssh'``, matching the clone path's behavior.

        Both the SCP shorthand (``git@host:path``) and the ``ssh://``
        URL form parse to ``explicit_scheme == 'ssh'`` but exercise
        different regex branches in ``DependencyReference.parse``, so
        both forms are asserted here to seal the new GitLab branch.
        """
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        monkeypatch.delenv("GITLAB_APM_PAT", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        resolver = _make_gitlab_resolver()
        dep_ref = DependencyReference.parse(dependency_str)
        assert dep_ref.explicit_scheme == "ssh", (
            f"precondition: {dependency_str!r} must parse as explicit SSH; "
            f"got {dep_ref.explicit_scheme!r}"
        )

        with patch("subprocess.run", return_value=_ok_run()) as mock_run:
            result = validation._validate_package_exists(
                dependency_str,
                verbose=False,
                auth_resolver=resolver,
                dep_ref=dep_ref,
            )

        assert result is True
        urls = _probe_urls(mock_run)
        assert urls, "expected at least one git ls-remote probe"
        assert _scheme_of(urls[0]) == "ssh", (
            f"explicit ssh scheme must probe SSH first; got {urls!r}"
        )

    def test_gitlab_ssh_with_fallback_probes_ssh_then_https(self, monkeypatch) -> None:
        """``APM_ALLOW_PROTOCOL_FALLBACK=1`` keeps SSH-first ordering.

        The new GitLab explicit-SSH branch builds two candidate URLs when
        ``APM_ALLOW_PROTOCOL_FALLBACK=1`` is set:
        ``[ssh_url, package_url]``. Order matters for security: SSH must
        precede HTTPS so SSH-key users never silently fall back to a
        token-bearing HTTPS probe when SSH would have succeeded.
        Without this assertion a future refactor could swap the order
        and no test would catch it.

        Probe behavior here: SSH fails (returncode=1), HTTPS succeeds
        (returncode=0). Validation must return True after trying SSH
        first, then HTTPS second.
        """
        monkeypatch.setenv("APM_ALLOW_PROTOCOL_FALLBACK", "1")
        monkeypatch.delenv("GITLAB_APM_PAT", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        resolver = _make_gitlab_resolver()
        dep_ref = DependencyReference.parse("git@gitlab.com:owner/repo.git#main")

        fail_run = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Permission denied (publickey)."
        )
        with patch("subprocess.run", side_effect=[fail_run, _ok_run()]) as mock_run:
            result = validation._validate_package_exists(
                "git@gitlab.com:owner/repo.git#main",
                verbose=False,
                auth_resolver=resolver,
                dep_ref=dep_ref,
            )

        assert result is True
        urls = _probe_urls(mock_run)
        assert len(urls) == 2, (
            f"with APM_ALLOW_PROTOCOL_FALLBACK=1, expected exactly 2 probes "
            f"(ssh then https); got {urls!r}"
        )
        assert _scheme_of(urls[0]) == "ssh", (
            f"first probe must be SSH (SSH-first ordering); got {urls!r}"
        )
        assert _scheme_of(urls[1]) == "https", f"second probe must be HTTPS fallback; got {urls!r}"
