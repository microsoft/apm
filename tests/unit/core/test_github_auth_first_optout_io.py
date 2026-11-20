"""Tests for the github-auth-first opt-out and TLS-config inheritance (issue #2545) (filesystem, process and CLI boundary cases)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo

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


_PRIVATE_TOKEN = "private-token"


def _github_ctx(token: str | None = _PRIVATE_TOKEN) -> AuthContext:
    return AuthContext(
        token=token,
        source="test",
        token_type="unknown",
        host_info=HostInfo(
            host="github.com",
            kind="github",
            has_public_repos=True,
            api_base="https://api.github.com",
        ),
        git_env={},
    )


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

    def test_non_isolated_env_is_not_injected(self) -> None:
        """Without config isolation Git already reads the real config, so no
        process-scoped TLS entries are added on top of it."""
        with patch(
            "apm_cli.utils.git_env.real_git_tls_config",
            return_value={"http.sslcainfo": "/ambient.pem"},
        ) as mock_tls:
            env = AuthResolver.git_env_for_context(_github_ctx(), base_env={"PATH": "/usr/bin"})
        assert "http.sslcainfo" not in _indexed_git_config(env)
        mock_tls.assert_not_called()

    def test_opted_out_no_token_last_resort_keeps_tls_in_isolated_env(self) -> None:
        resolver = AuthResolver(github_auth_first=True)
        envs: list[dict[str, str]] = []

        def operation(token: str | None, env: dict[str, str]) -> str:
            envs.append(env)
            return "ok"

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                resolver._token_manager, "resolve_credential_from_gh_cli", return_value=None
            ),
            patch.object(resolver._token_manager, "resolve_credential_from_git", return_value=None),
            patch(
                "apm_cli.utils.git_env.real_git_tls_config",
                return_value={"http.sslcainfo": "/corp.pem"},
            ),
            patch("apm_cli.utils.console._rich_warning"),
        ):
            resolver.try_with_fallback(
                "github.com",
                operation,
                org="acme",
                unauth_first=True,
                base_env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/isolated"},
            )

        assert len(envs) == 1
        assert _indexed_git_config(envs[0])["http.sslcainfo"] == "/corp.pem"

    def test_opt_out_warning_not_emitted_for_auth_first_callers(self) -> None:
        """API/marketplace callers pass unauth_first=False; their order is
        unchanged by the opt-out, so the forwarding warning is not shown."""
        resolver = AuthResolver(github_auth_first=True)
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "tok"}, clear=True),
            patch("apm_cli.utils.console._rich_warning") as mock_warn,
        ):
            resolver.try_with_fallback("github.com", lambda token, env: token)
        mock_warn.assert_not_called()

    def test_opted_out_resolve_for_dep_scopes_credential_lookup_by_path(self) -> None:
        from apm_cli.models.dependency.reference import DependencyReference

        dep_ref = DependencyReference.parse("acme/widgets")
        for opted_out, expected_path in ((True, "acme/widgets"), (False, None)):
            resolver = AuthResolver(github_auth_first=opted_out)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("apm_cli.config.get_github_auth_first", return_value=False),
                patch.object(resolver, "resolve", return_value=_github_ctx()) as mock_resolve,
            ):
                resolver.resolve_for_dep(dep_ref)
            assert mock_resolve.call_args.kwargs["path"] == expected_path

    def test_opted_out_error_context_keeps_repository_path(self) -> None:
        resolver = AuthResolver(github_auth_first=True)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(resolver, "resolve", return_value=_github_ctx(None)) as mock_resolve,
        ):
            resolver.build_error_context("github.com", "clone", org="acme", dep_url="acme/widgets")
        assert mock_resolve.call_args.kwargs["path"] == "acme/widgets"
