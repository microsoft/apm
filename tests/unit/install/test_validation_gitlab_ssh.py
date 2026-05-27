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

    def test_gitlab_git_at_shorthand_probes_ssh_url(self, monkeypatch) -> None:
        """``git@gitlab.com:owner/repo.git#ref`` validates via SSH.

        Before the fix, the GitLab branch of ``_validate_package_exists``
        unconditionally probed ``[https://gitlab.com/owner/repo]`` even
        when the user typed ``git@gitlab.com:...``. With no token, the
        probe failed and the validator raised ``AuthenticationError``.
        After the fix, the SSH URL is probed first when
        ``explicit_scheme == 'ssh'``, matching the clone path's behavior.
        """
        monkeypatch.delenv("APM_ALLOW_PROTOCOL_FALLBACK", raising=False)
        monkeypatch.delenv("GITLAB_APM_PAT", raising=False)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        resolver = _make_gitlab_resolver()
        dep_ref = DependencyReference.parse("git@gitlab.com:owner/repo.git#main")

        with patch("subprocess.run", return_value=_ok_run()) as mock_run:
            result = validation._validate_package_exists(
                "git@gitlab.com:owner/repo.git#main",
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
        assert urls[0].startswith("git@gitlab.com:"), (
            f"expected SCP-shorthand SSH URL, got {urls[0]!r}"
        )
