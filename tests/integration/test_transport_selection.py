"""Unit tests for src/apm_cli/deps/transport_selection.py.

Covers the selection matrix from issue microsoft/apm#778:

* explicit ssh:// URLs are strict (no HTTPS fallback) -- closes #661
* explicit https:// URLs are strict (no SSH fallback)
* explicit http:// URLs are strict and never use token auth
* shorthand consults git insteadOf rewrites and prefers SSH on rewrite hits
  -- closes #328
* shorthand defaults to HTTPS-only when no rewrite, no CLI pref, no env
* CLI / env overrides for shorthand: --ssh / --https, APM_GIT_PROTOCOL
* APM_ALLOW_PROTOCOL_FALLBACK / --allow-protocol-fallback restores the
  legacy permissive cross-protocol chain so today's clones still succeed
* token presence drives whether auth-HTTPS is in the plan
* per-instance cache: insteadOf rewrites are loaded once
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional  # noqa: F401, UP035
from unittest.mock import patch

import pytest

from apm_cli.deps.transport_selection import (
    GitConfigInsteadOfResolver,
    InsteadOfResolver,  # noqa: F401
    NoOpInsteadOfResolver,  # noqa: F401
    TransportAttempt,  # noqa: F401
    TransportPlan,
    TransportSelector,
)
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.utils.git_env import GitUrlRewriteError, get_git_executable, git_network_env

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dep(spec: str) -> DependencyReference:
    return DependencyReference.parse(spec)


def _scheme_labels(plan: TransportPlan) -> list[str]:
    return [a.scheme for a in plan.attempts]


def _real_gitconfig_resolver_plan(
    tmp_path: Path,
    *,
    rewrite_base: str,
    rewrite_prefix: str,
    candidate_url: str = "https://github.com/owner/repo",
    command_header: str | None = None,
) -> TransportPlan:
    """Return one transport plan using the production Git config resolver."""
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "gitconfig"
    config.write_text(
        f'[url "{rewrite_base}"]\n\tinsteadOf = {rewrite_prefix}\n',
        encoding="ascii",
    )
    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": str(config),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if command_header is not None:
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": command_header,
            }
        )
    with patch.dict(os.environ, env, clear=True):
        return TransportSelector(insteadof_resolver=GitConfigInsteadOfResolver()).select(
            dep_ref=_dep("owner/repo"),
            has_token=True,
            candidate_url=candidate_url,
        )


def _real_git_headers(env: dict[str, str], remote_url: str, cwd: Path) -> list[str]:
    """Return the header values real Git selects for one URL."""
    result = subprocess.run(
        (
            get_git_executable(),
            "config",
            "--get-urlmatch",
            "http.extraHeader",
            remote_url,
        ),
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )
    assert result.returncode in {0, 1}, result.stderr
    return result.stdout.splitlines()


# ---------------------------------------------------------------------------
# Selection matrix
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Resolver caching
# ---------------------------------------------------------------------------


class TestGitConfigInsteadOfResolver:
    def test_lookup_cached_per_instance(self):
        """`git config --get-regexp` is shelled out at most once per instance."""
        resolver = GitConfigInsteadOfResolver()
        with patch("apm_cli.utils.git_env._git_config_run") as run:
            # Simulate one rewrite rule: https://github.com/ -> git@github.com:
            run.return_value.returncode = 0
            run.return_value.stdout = (
                b"command\0url.git@github.com:.insteadof\nhttps://github.com/\0"
            )
            resolver.resolve("https://github.com/owner/repo")
            resolver.resolve("https://github.com/other/proj")
            resolver.resolve("https://gitlab.com/acme/lib")
            assert run.call_count == 1

    def test_uses_sanitized_normal_env(self):
        """The resolver keeps user config while removing repository state.

        The downloader's locked-down git_env sets GIT_CONFIG_GLOBAL=/dev/null,
        which would suppress user insteadOf rewrites. Issue #328 stays broken
        unless the resolver starts from the normal environment.
        """
        resolver = GitConfigInsteadOfResolver()
        with (
            patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": "/home/test/.gitconfig",
                    "GIT_DIR": "/hook/repo.git",
                },
            ),
            patch("apm_cli.utils.git_env._git_config_run") as run,
        ):
            run.return_value.returncode = 0
            run.return_value.stdout = b""
            resolver.resolve("https://github.com/owner/repo")
            _args, kwargs = run.call_args
            assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/home/test/.gitconfig"
            assert "GIT_DIR" not in kwargs["env"]

    def test_resolve_returns_none_when_no_rewrites(self):
        resolver = GitConfigInsteadOfResolver()
        with patch("apm_cli.utils.git_env._git_config_run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = b""
            assert resolver.resolve("https://github.com/owner/repo") is None

    @pytest.mark.parametrize("command_header", [None, "Authorization: Basic sentinel"])
    def test_real_resolver_allows_https_to_scp_ssh_rewrite(
        self,
        tmp_path: Path,
        command_header: str | None,
    ) -> None:
        """A command-scoped HTTP header does not block same-host SCP rewrites."""
        plan = _real_gitconfig_resolver_plan(
            tmp_path,
            rewrite_base="git@github.com:",
            rewrite_prefix="https://github.com/",
            command_header=command_header,
        )

        assert _scheme_labels(plan) == ["ssh"]
        assert plan.strict is True
        assert plan.attempts[0].requested_url == "https://github.com/owner/repo"
        assert plan.attempts[0].effective_url == "git@github.com:owner/repo"
        assert plan.attempts[0].use_token is False

    def test_real_resolver_unmatched_command_header_leaves_https_plan(
        self,
        tmp_path: Path,
    ) -> None:
        """A non-matching header-only config stays on authenticated HTTPS."""
        plan = _real_gitconfig_resolver_plan(
            tmp_path,
            rewrite_base="git@github.com:",
            rewrite_prefix="https://github.com/acme/",
            command_header="Authorization: Basic sentinel",
        )

        assert _scheme_labels(plan) == ["https"]
        assert plan.strict is True
        assert plan.attempts[0].requested_url is None
        assert plan.attempts[0].effective_url is None
        assert plan.attempts[0].use_token is True

    def test_real_resolver_allows_https_to_ssh_url_rewrite(
        self,
        tmp_path: Path,
    ) -> None:
        """The real resolver also accepts same-host ssh:// rewrites."""
        plan = _real_gitconfig_resolver_plan(
            tmp_path,
            rewrite_base="ssh://git@github.com/",
            rewrite_prefix="https://github.com/",
            command_header="Authorization: Basic sentinel",
        )

        assert _scheme_labels(plan) == ["ssh"]
        assert plan.strict is True
        assert plan.attempts[0].requested_url == "https://github.com/owner/repo"
        assert plan.attempts[0].effective_url == "ssh://git@github.com/owner/repo"
        assert plan.attempts[0].use_token is False

    def test_real_resolver_checks_http_authorization_for_https_rewrite(
        self,
        tmp_path: Path,
    ) -> None:
        """HTTP(S) rewrites still enforce the credential origin boundary."""
        with pytest.raises(GitUrlRewriteError, match="different HTTPS origin"):
            _real_gitconfig_resolver_plan(
                tmp_path,
                rewrite_base="https://github.com:8443/",
                rewrite_prefix="https://github.com/",
                command_header="Authorization: Basic sentinel",
            )

    def test_real_resolver_consumer_drops_dummy_http_header_after_scp_rewrite(
        self,
        tmp_path: Path,
    ) -> None:
        """A real selector + git_network_env consumer keeps rewrite but drops HTTP auth."""
        plan = _real_gitconfig_resolver_plan(
            tmp_path,
            rewrite_base="git@github.com:",
            rewrite_prefix="https://github.com/",
            command_header="Authorization: Basic sentinel",
        )

        attempt = plan.attempts[0]
        assert attempt.requested_url == "https://github.com/owner/repo"
        assert attempt.effective_url == "git@github.com:owner/repo"

        env = {
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic sentinel",
        }
        with patch.dict(os.environ, env, clear=True):
            child = git_network_env(attempt.requested_url, env)

        assert _real_git_headers(child, attempt.requested_url, tmp_path) == []
        rewrites = subprocess.run(
            (
                get_git_executable(),
                "config",
                "--null",
                "--get-regexp",
                r"^url\..*\.insteadOf$",
            ),
            check=True,
            capture_output=True,
            env=child,
            cwd=tmp_path,
        )
        assert rewrites.stdout == b"url.git@github.com:.insteadof\nhttps://github.com/\0"
