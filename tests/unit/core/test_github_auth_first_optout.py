"""Tests for the github-auth-first opt-out and TLS-config inheritance (issue #2545).

Covers:
- ``AuthResolver.uses_public_github_anonymous_first`` CLI flag > env var >
  persisted config > default (False) resolution for exact-host github.com.
- The non-github.com short-circuit (always False regardless of the opt-out).
- ``AuthResolver.emit_github_auth_first_diagnostic`` one-time warning, both
  via a wired ``DiagnosticCollector`` and the ``_rich_warning`` fallback.
- ``AuthResolver.build_public_github_anonymous_git_env`` inheriting
  ``http.sslBackend``/``http.sslCAInfo`` from :func:`real_git_tls_config`.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo

# ---------------------------------------------------------------------------
# uses_public_github_anonymous_first -- resolution order
# ---------------------------------------------------------------------------


class TestUsesPublicGithubAnonymousFirstOptOut:
    def test_default_is_anonymous_first_for_github_com(self) -> None:
        """No CLI flag, env var, or config -> unchanged default (True)."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("apm_cli.config.get_github_auth_first", return_value=False):
                resolver = AuthResolver()
                assert resolver.uses_public_github_anonymous_first("github.com") is True

    def test_cli_flag_true_skips_anonymous_attempt(self) -> None:
        """AuthResolver(github_auth_first=True) opts out regardless of env/config."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("apm_cli.config.get_github_auth_first", return_value=False):
                resolver = AuthResolver(github_auth_first=True)
                assert resolver.uses_public_github_anonymous_first("github.com") is False

    def test_env_var_true_skips_anonymous_attempt(self) -> None:
        """APM_GITHUB_AUTH_FIRST=1 opts out without a CLI flag."""
        with patch.dict(os.environ, {"APM_GITHUB_AUTH_FIRST": "1"}, clear=True):
            resolver = AuthResolver()
            assert resolver.uses_public_github_anonymous_first("github.com") is False

    @pytest.mark.parametrize("env_val", ["0", "false", "no", "off"])
    def test_env_var_explicit_falsy_overrides_persisted_config_true(self, env_val: str) -> None:
        """Explicit falsy env value wins over a persisted config of True."""
        with patch.dict(os.environ, {"APM_GITHUB_AUTH_FIRST": env_val}, clear=True):
            with patch("apm_cli.config.get_github_auth_first", return_value=True):
                resolver = AuthResolver()
                assert resolver.uses_public_github_anonymous_first("github.com") is True

    def test_persisted_config_true_used_when_cli_and_env_absent(self) -> None:
        """github_auth_first=True in ~/.apm/config.json opts out when unset elsewhere."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("apm_cli.config.get_github_auth_first", return_value=True):
                resolver = AuthResolver()
                assert resolver.uses_public_github_anonymous_first("github.com") is False

    def test_non_github_host_always_false_regardless_of_opt_out(self) -> None:
        """GHE/ADO/GitLab/generic hosts never use the anonymous-first attempt."""
        with patch.dict(os.environ, {}, clear=True):
            with patch("apm_cli.config.get_github_auth_first", return_value=False):
                resolver = AuthResolver(github_auth_first=True)
                assert resolver.uses_public_github_anonymous_first("dev.azure.com") is False
                assert resolver.uses_public_github_anonymous_first("gitlab.com") is False


# ---------------------------------------------------------------------------
# emit_github_auth_first_diagnostic -- one-time warning
# ---------------------------------------------------------------------------


class TestEmitGithubAuthFirstDiagnostic:
    def test_emits_once_via_rich_warning_fallback(self) -> None:
        resolver = AuthResolver()
        with patch("apm_cli.utils.console._rich_warning") as mock_warn:
            resolver.emit_github_auth_first_diagnostic()
            resolver.emit_github_auth_first_diagnostic()
        # One emission -> _rich_warning called twice (msg + recovery detail);
        # the dedup'd second call adds nothing (mirrors TestStalePATDiagnosticDedup).
        assert mock_warn.call_count == 2
        assert "github-auth-first" in mock_warn.call_args_list[0].args[0]
        assert "apm config unset github-auth-first" in mock_warn.call_args_list[1].args[0]

    def test_emits_via_diagnostics_when_logger_wired(self) -> None:
        resolver = AuthResolver()
        diag = MagicMock()
        logger = MagicMock()
        logger.diagnostics = diag
        resolver.set_logger(logger)

        resolver.emit_github_auth_first_diagnostic()

        diag.warn.assert_called_once()
        # always_show_detail=True: the recovery instructions are folded into
        # the always-rendered message, not a `detail=` kwarg the
        # DiagnosticCollector would otherwise hide without --verbose.
        assert "github-auth-first" in diag.warn.call_args.args[0]
        assert "apm config unset github-auth-first" in diag.warn.call_args.args[0]
        assert not diag.warn.call_args.kwargs.get("detail")

    def test_triggered_once_through_uses_public_github_anonymous_first(self) -> None:
        """Calling the decision method N times for the opt-out warns once."""
        with patch.dict(os.environ, {"APM_GITHUB_AUTH_FIRST": "1"}, clear=True):
            resolver = AuthResolver()
            with patch("apm_cli.utils.console._rich_warning") as mock_warn:
                for _ in range(3):
                    resolver.uses_public_github_anonymous_first("github.com")
            assert mock_warn.call_count == 2

    def test_no_warning_when_opt_out_inactive(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("apm_cli.config.get_github_auth_first", return_value=False):
                resolver = AuthResolver()
                with patch("apm_cli.utils.console._rich_warning") as mock_warn:
                    resolver.uses_public_github_anonymous_first("github.com")
                mock_warn.assert_not_called()


# ---------------------------------------------------------------------------
# build_public_github_anonymous_git_env -- ambient TLS config inheritance
# ---------------------------------------------------------------------------


class TestBuildAnonymousGitEnvTlsInheritance:
    def test_inherits_ssl_backend_and_ca_info(self) -> None:
        with patch(
            "apm_cli.utils.git_env.real_git_tls_config",
            return_value={"http.sslbackend": "schannel", "http.sslcainfo": "/ca/corp.pem"},
        ):
            env = AuthResolver.build_public_github_anonymous_git_env()

        count = int(env["GIT_CONFIG_COUNT"])
        config = {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)
        }
        assert config["http.sslbackend"] == "schannel"
        assert config["http.sslcainfo"] == "/ca/corp.pem"
        # credential.helper stays forced empty regardless of TLS inheritance.
        assert config["credential.helper"] == ""

    def test_no_ambient_tls_config_leaves_env_unchanged(self) -> None:
        with patch("apm_cli.utils.git_env.real_git_tls_config", return_value={}):
            env = AuthResolver.build_public_github_anonymous_git_env()

        count = int(env["GIT_CONFIG_COUNT"])
        keys = {env[f"GIT_CONFIG_KEY_{i}"] for i in range(count)}
        assert "http.sslbackend" not in keys
        assert "http.sslcainfo" not in keys

    def test_tls_probe_failure_does_not_block_anonymous_env(self) -> None:
        """A best-effort probe failure yields {} upstream; env building still succeeds."""
        with patch("apm_cli.utils.git_env.real_git_tls_config", return_value={}):
            env = AuthResolver.build_public_github_anonymous_git_env()
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"

    def test_ambient_value_does_not_override_callers_explicit_config(self) -> None:
        """Issue #2545 follow-up: GIT_CONFIG_* is last-one-wins in Git, so
        blindly appending the ambient value after a caller's explicit
        http.sslBackend/http.sslCAInfo would silently override their
        choice. Ambient inheritance must only fill a gap."""
        base_env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.sslCAInfo",
            "GIT_CONFIG_VALUE_0": "/explicit/caller-ca.pem",
        }
        with patch(
            "apm_cli.utils.git_env.real_git_tls_config",
            return_value={"http.sslbackend": "schannel", "http.sslcainfo": "/ambient-ca.pem"},
        ):
            env = AuthResolver.build_public_github_anonymous_git_env(base_env=base_env)

        config = _indexed_git_config(env)
        # The caller's explicit http.sslCAInfo survives untouched...
        assert config["http.sslCAInfo"] == "/explicit/caller-ca.pem"
        # ...http.sslcainfo (case-insensitive duplicate) is not also added...
        assert list(config.values()).count("/ambient-ca.pem") == 0
        # ...while the key the caller never set is still filled from ambient.
        assert config["http.sslbackend"] == "schannel"


# ---------------------------------------------------------------------------
# try_with_fallback -- credentialed github.com attempts also inherit TLS
# config (issue #2545 follow-up: --auth-first alone did not fix the
# TLS-inspecting-proxy failure for users who already have a token, because
# the credentialed retry built its env from the isolated hardened base).
# ---------------------------------------------------------------------------


def _indexed_git_config(env: dict[str, str]) -> dict[str, str]:
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}


class _HttpStatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class TestCredentialedGithubEnvTlsInheritance:
    def test_secondary_credential_fallback_inherits_tls_config(self) -> None:
        """issue #2545 follow-up: the secondary chain (gh CLI, then git
        credential fill) tried after the primary token also fails must not
        lose the ambient TLS config either -- it also builds a github.com
        env from an isolated base_env."""
        resolver = AuthResolver()
        base_env = {"GIT_CONFIG_GLOBAL": "/isolated/empty-gitconfig", "GIT_CONFIG_NOSYSTEM": "1"}
        attempts: list[tuple[str | None, dict[str, str]]] = []

        def operation(token: str | None, env: dict[str, str]) -> str:
            attempts.append((token, env))
            if token != "gh-cli-token":
                raise _HttpStatusError(401)
            return "ok"

        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT_ACME": "primary-token"}, clear=True),
            patch("apm_cli.config.get_github_auth_first", return_value=False),
            patch.object(
                resolver._token_manager,
                "resolve_credential_from_gh_cli",
                return_value="gh-cli-token",
            ),
            patch(
                "apm_cli.utils.git_env.real_git_tls_config",
                return_value={"http.sslbackend": "schannel"},
            ),
        ):
            result = resolver.try_with_fallback(
                "github.com",
                operation,
                org="acme",
                path="acme/widgets",
                unauth_first=True,
                base_env=base_env,
            )

        assert result == "ok"
        assert [token for token, _env in attempts] == [None, "primary-token", "gh-cli-token"]
        secondary_env = attempts[-1][1]
        assert _indexed_git_config(secondary_env)["http.sslbackend"] == "schannel"

    def test_authenticated_retry_inherits_tls_config(self) -> None:
        """The 401/403/404-triggered credentialed retry inherits ambient TLS
        config even though base_env is an isolated hardened environment."""
        resolver = AuthResolver()
        base_env = {"GIT_CONFIG_GLOBAL": "/isolated/empty-gitconfig", "GIT_CONFIG_NOSYSTEM": "1"}
        attempts: list[tuple[str | None, dict[str, str]]] = []

        def operation(token: str | None, env: dict[str, str]) -> str:
            attempts.append((token, env))
            if token is None:
                raise _HttpStatusError(404)
            return "private-ok"

        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT_ACME": "private-token"}, clear=True),
            patch("apm_cli.config.get_github_auth_first", return_value=False),
            patch(
                "apm_cli.utils.git_env.real_git_tls_config",
                return_value={"http.sslbackend": "schannel"},
            ),
        ):
            result = resolver.try_with_fallback(
                "github.com",
                operation,
                org="acme",
                path="acme/widgets",
                unauth_first=True,
                base_env=base_env,
            )

        assert result == "private-ok"
        assert [token for token, _env in attempts] == [None, "private-token"]
        credentialed_env = attempts[1][1]
        assert _indexed_git_config(credentialed_env)["http.sslbackend"] == "schannel"

    def test_auth_first_bypass_clone_path_inherits_tls_config(self) -> None:
        """github_downloader.py's --auth-first bypass calls git_env_for_remote()
        directly (not try_with_fallback) once uses_public_github_anonymous_first()
        is False; that direct call must still inherit the ambient TLS config."""
        with patch.dict(os.environ, {"APM_GITHUB_AUTH_FIRST": "1"}, clear=True):
            resolver = AuthResolver()
            assert resolver.uses_public_github_anonymous_first("github.com") is False

            ctx = AuthContext(
                token="private-token",
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
            with patch(
                "apm_cli.utils.git_env.real_git_tls_config",
                return_value={"http.sslcainfo": "/ca/corp.pem"},
            ):
                env = resolver.git_env_for_remote(
                    ctx,
                    "https://github.com/acme/widgets.git",
                    base_env={"GIT_CONFIG_GLOBAL": "/isolated/empty-gitconfig"},
                )

        assert _indexed_git_config(env)["http.sslcainfo"] == "/ca/corp.pem"

    def test_non_github_host_unaffected(self) -> None:
        """ADO/GitLab/generic credentialed envs are never probed for TLS config."""
        resolver = AuthResolver()
        with (
            patch.dict(os.environ, {"ADO_APM_PAT": "pat-token"}, clear=True),
            patch(
                "apm_cli.utils.git_env.real_git_tls_config",
                return_value={"http.sslbackend": "schannel"},
            ) as mock_tls,
        ):

            def operation(token: str | None, env: dict[str, str]) -> tuple:
                return (token, env)

            # base_env set so the credentialed path actually routes through
            # git_env_for_context() (base_env=None short-circuits to
            # ctx.git_env directly, which would make this test a no-op).
            token, env = resolver.try_with_fallback(
                "dev.azure.com", operation, base_env={"PATH": "/usr/bin"}
            )

        assert token == "pat-token"
        assert "http.sslbackend" not in _indexed_git_config(env)
        mock_tls.assert_not_called()

    def test_opted_out_caller_requesting_unauth_first_skips_anonymous_entirely(self) -> None:
        """issue #2545 follow-up: a caller like validation.py always passes
        unauth_first=True for github.com without checking the opt-out itself.
        Before this fix, try_with_fallback() still tried operation(None, ...)
        first regardless of the opt-out, defeating --auth-first for exactly
        the callers that motivated it."""
        with patch.dict(
            os.environ,
            {"APM_GITHUB_AUTH_FIRST": "1", "GITHUB_APM_PAT_ACME": "private-token"},
            clear=True,
        ):
            resolver = AuthResolver()
            attempts: list[str | None] = []

            def operation(token: str | None, _env: dict[str, str]) -> str:
                attempts.append(token)
                return "ok"

            result = resolver.try_with_fallback(
                "github.com",
                operation,
                org="acme",
                path="acme/widgets",
                unauth_first=True,
            )

        assert result == "ok"
        # The bug: attempts == [None, "private-token"] (anonymous tried first).
        assert attempts == ["private-token"]

    def test_opted_out_with_no_token_still_falls_back_to_anonymous(self) -> None:
        """With the opt-out active but no credential resolvable at all, there
        is nothing to jump straight to -- the last-resort anonymous attempt
        must still happen instead of raising with no attempt at all."""
        resolver = AuthResolver()
        attempts: list[str | None] = []

        def operation(token: str | None, _env: dict[str, str]) -> str:
            attempts.append(token)
            return "ok"

        with (
            patch.dict(os.environ, {"APM_GITHUB_AUTH_FIRST": "1"}, clear=True),
            # Never let this test shell out to a real credential helper.
            patch.object(
                resolver._token_manager,
                "resolve_credential_from_gh_cli",
                return_value=None,
            ),
            patch.object(
                resolver._token_manager,
                "resolve_credential_from_git",
                return_value=None,
            ),
        ):
            result = resolver.try_with_fallback(
                "github.com",
                operation,
                org="acme",
                unauth_first=True,
            )

        assert result == "ok"
        assert attempts == [None]

    def test_non_github_caller_unauth_first_behavior_unchanged(self) -> None:
        """The opt-out is exact-host github.com only; a GitLab/generic caller
        that requests unauth_first=True still tries anonymous first."""
        resolver = AuthResolver()
        attempts: list[str | None] = []

        def operation(token: str | None, _env: dict[str, str]) -> str:
            attempts.append(token)
            if token is None:
                raise _HttpStatusError(401)
            return "ok"

        with patch.dict(os.environ, {"GITLAB_APM_PAT": "gl-token"}, clear=True):
            result = resolver.try_with_fallback(
                "gitlab.com",
                operation,
                org="acme",
                unauth_first=True,
            )

        assert result == "ok"
        assert attempts == [None, "gl-token"]

    def test_git_env_for_context_does_not_override_callers_explicit_config(self) -> None:
        """Same precedence protection as the anonymous env, for the
        credentialed path (git_env_for_context)."""
        ctx = AuthContext(
            token="private-token",
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
        base_env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.sslBackend",
            "GIT_CONFIG_VALUE_0": "openssl",
        }
        with patch(
            "apm_cli.utils.git_env.real_git_tls_config",
            return_value={"http.sslbackend": "schannel"},
        ):
            env = AuthResolver.git_env_for_context(ctx, base_env=base_env)

        config = _indexed_git_config(env)
        assert config["http.sslBackend"] == "openssl"
        assert "schannel" not in config.values()

    def test_git_env_for_context_returns_fresh_dict(self) -> None:
        """git_env_for_context() never mutates its base_env or aliases a
        cached AuthContext.git_env -- _build_git_env always copies first."""
        ctx = AuthContext(
            token="private-token",
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
        base_env = {"MARK": "1"}
        with patch(
            "apm_cli.utils.git_env.real_git_tls_config",
            return_value={"http.sslbackend": "schannel"},
        ):
            env1 = AuthResolver.git_env_for_context(ctx, base_env=base_env)
            env2 = AuthResolver.git_env_for_context(ctx, base_env=base_env)
        assert base_env == {"MARK": "1"}, "base_env must not be mutated in place"
        assert env1 is not env2
        assert list(_indexed_git_config(env1).values()).count("schannel") == 1
        assert list(_indexed_git_config(env2).values()).count("schannel") == 1
