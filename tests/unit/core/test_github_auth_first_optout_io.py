"""Tests for the github-auth-first opt-out and TLS-config inheritance (issue #2545) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from apm_cli.core.auth import AuthResolver

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# uses_public_github_anonymous_first -- resolution order
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# emit_github_auth_first_diagnostic -- one-time warning
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# build_public_github_anonymous_git_env -- ambient TLS config inheritance
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# try_with_fallback -- credentialed github.com attempts also inherit TLS
# config (issue #2545 follow-up: --auth-first alone did not fix the
# TLS-inspecting-proxy failure for users who already have a token, because
# the credentialed retry built its env from the isolated hardened base).
# ---------------------------------------------------------------------------


def _indexed_git_config(env: dict[str, str]) -> dict[str, str]:
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}


# ---------------------------------------------------------------------------
# Scope regression traps (issue #2545 apm-scope:v1 done-when)
# ---------------------------------------------------------------------------


class TestScopeRegressionTraps:
    def test_user_git_config_ca_reaches_anonymous_env_without_credential_helper(
        self, tmp_path
    ) -> None:
        """A CA configured only in the user's global Git config (next to a
        credential helper) reaches the isolated anonymous env through a real
        `git config` probe, while credential helpers stay disabled."""
        from apm_cli.utils.git_env import reset_git_cache

        user_config = tmp_path / "user.gitconfig"
        user_config.write_text(
            "[http]\n"
            "\tsslCAInfo = /corp/proxy-ca.pem\n"
            "\tsslCAPath = /corp/certs\n"
            "\tsslBackend = openssl\n"
            "[credential]\n"
            "\thelper = store\n",
            encoding="utf-8",
        )
        caller_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "GIT_CONFIG_GLOBAL": str(user_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        reset_git_cache()
        try:
            with patch.dict(os.environ, caller_env, clear=True):
                env = AuthResolver.build_public_github_anonymous_git_env()
        finally:
            reset_git_cache()

        config = {key.lower(): value for key, value in _indexed_git_config(env).items()}
        assert config["http.sslcainfo"] == "/corp/proxy-ca.pem"
        assert config["http.sslcapath"] == "/corp/certs"
        assert config["http.sslbackend"] == "openssl"
        assert config["credential.helper"] == ""
        assert env["GIT_CONFIG_GLOBAL"] != str(user_config)
        assert "store" not in config.values()
