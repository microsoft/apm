"""Unit tests for validation.py ADO bearer auth plumbing (#1015).

Tests that:
1. auth_scheme from the resolved dep context reaches _build_repo_url.
2. _dep_ctx.git_env (GIT_CONFIG_* overrides) is merged into subprocess env.
3. Auth-related git ls-remote failures raise AuthenticationError.
4. Non-auth failures (DNS, timeout) return False.
5. PAT regression: auth_scheme="basic" still embeds token in URL.
"""

import base64
import subprocess
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core.auth import AuthResolver, BearerFallbackOutcome
from apm_cli.install.errors import AuthenticationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dep_ref(host="dev.azure.com", repo_url="myorg/myproject/_git/myrepo"):
    """Build a minimal DependencyReference-like mock."""
    dep = MagicMock()
    dep.host = host
    dep.port = None
    dep.repo_url = repo_url
    dep.reference = "main"
    dep.alias = None
    dep.is_azure_devops.return_value = True
    dep.explicit_scheme = None
    dep.is_insecure = False
    dep.ado_organization = "myorg"
    dep.ado_project = "myproject"
    dep.ado_repo = "myrepo"
    return dep


def _make_auth_ctx(token="test-pat", auth_scheme="basic", git_env=None):  # noqa: S107
    """Build an AuthContext-shaped mock."""
    ctx = MagicMock()
    ctx.token = token
    ctx.auth_scheme = auth_scheme
    ctx.git_env = git_env or {}
    ctx.host_info.kind = "ado"
    ctx.source = "ADO_APM_PAT" if auth_scheme == "basic" and token else "AAD_BEARER_AZ_CLI"
    return ctx


def _make_resolver(auth_ctx=None):
    """Build an AuthResolver mock."""
    resolver = MagicMock()
    if auth_ctx is None:
        auth_ctx = _make_auth_ctx()
    resolver.resolve_for_dep.return_value = auth_ctx
    resolver.git_env_for_context.side_effect = AuthResolver.git_env_for_context
    resolver.git_env_for_remote.side_effect = lambda ctx, _url: AuthResolver.git_env_for_context(
        ctx,
        base_env=ctx.git_env,
    )
    resolver.build_ado_bearer_git_env.side_effect = lambda _ctx, bearer, _url, **_kwargs: (
        AuthResolver._build_git_env(
            bearer,
            scheme="bearer",
            host_kind="ado",
            base_env={},
        )
    )
    resolver.execute_with_bearer_fallback.side_effect = (
        lambda _dep, primary_op, _bearer_op, _is_auth_failure: BearerFallbackOutcome(
            primary_op(),
            False,
        )
    )
    resolver.build_error_context.return_value = "    Diagnostic payload"
    return resolver


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBearerAuthSchemePassedToBuildRepoUrl:
    """auth_scheme='bearer' from resolve_for_dep reaches _build_repo_url."""

    @patch("subprocess.run")
    def test_bearer_scheme_reaches_build_repo_url(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        bearer_ctx = _make_auth_ctx(
            token="eyJhbGciOi.fake",
            auth_scheme="bearer",
            git_env={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": "Authorization: Bearer eyJhbGciOi.fake",
            },
        )
        resolver = _make_resolver(bearer_ctx)

        from apm_cli.install.validation import _validate_package_exists

        result = _validate_package_exists(
            "dev.azure.com/myorg/myproject/_git/myrepo",
            auth_resolver=resolver,
        )
        assert result is True

        # Verify the URL used in git ls-remote does NOT embed the JWT
        # in the userinfo (bearer uses extraheader, not URL embedding).
        call_args = mock_run.call_args
        probe_url = call_args[0][0][-1]  # last element of the command list
        parsed = urllib.parse.urlparse(probe_url)
        # Bearer tokens should NOT appear as username in the URL
        assert parsed.username != "eyJhbGciOi.fake"


class TestBearerGitEnvMergedIntoSubprocess:
    """_dep_ctx.git_env (GIT_CONFIG_*) overrides are in subprocess env."""

    @patch("subprocess.run")
    def test_git_config_keys_present_in_env(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        bearer_ctx = _make_auth_ctx(
            token="test-bearer",
            auth_scheme="bearer",
            git_env={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": "Authorization: Bearer eyJtoken",
            },
        )
        resolver = _make_resolver(bearer_ctx)

        from apm_cli.install.validation import _validate_package_exists

        _validate_package_exists(
            "dev.azure.com/myorg/myproject/_git/myrepo",
            auth_resolver=resolver,
        )

        call_kwargs = mock_run.call_args
        # subprocess.run is called with env= keyword or positional
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env", {})
        entries = {
            env.get(f"GIT_CONFIG_KEY_{index}", ""): env.get(f"GIT_CONFIG_VALUE_{index}", "")
            for index in range(int(env.get("GIT_CONFIG_COUNT", "0")))
        }
        header = next(
            value
            for key, value in entries.items()
            if key.lower().endswith(".extraheader") and value.startswith("Authorization: ")
        )
        scheme, credential = header.split(" ", 2)[1:]
        assert scheme == "Bearer"
        assert credential == "test-bearer"


class TestAdoAuthFailureRaisesAuthenticationError:
    """git ls-remote 401/403 raises AuthenticationError with diagnostics."""

    @patch("subprocess.run")
    @patch("apm_cli.core.azure_cli.get_bearer_provider")
    def test_401_raises_authentication_error(self, mock_provider_fn, mock_run):
        # Make bearer fallback unavailable so auth failure is not masked
        mock_provider = MagicMock()
        mock_provider.is_available.return_value = False
        mock_provider_fn.return_value = mock_provider

        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: Authentication failed for 'https://dev.azure.com/...' (401)",
            stdout="",
        )
        resolver = _make_resolver()

        from apm_cli.install.validation import _validate_package_exists

        with pytest.raises(AuthenticationError) as exc_info:
            _validate_package_exists(
                "dev.azure.com/myorg/myproject/_git/myrepo",
                auth_resolver=resolver,
            )

        assert exc_info.value.diagnostic_context != ""
        # Bounded full-phrase assertion (CodeQL: avoid arbitrary-
        # position substring match; our tests.instructions.md bans
        # bare URL/host substring checks).
        assert str(exc_info.value) == "Authentication failed for dev.azure.com"

    @patch("subprocess.run")
    @patch("apm_cli.core.azure_cli.get_bearer_provider")
    def test_403_raises_authentication_error(self, mock_provider_fn, mock_run):
        mock_provider = MagicMock()
        mock_provider.is_available.return_value = False
        mock_provider_fn.return_value = mock_provider

        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: unable to access '...': The requested URL returned error: 403",
            stdout="",
        )
        resolver = _make_resolver()

        from apm_cli.install.validation import _validate_package_exists

        with pytest.raises(AuthenticationError):
            _validate_package_exists(
                "dev.azure.com/myorg/myproject/_git/myrepo",
                auth_resolver=resolver,
            )


class TestAdoValidationCanonicalFallback:
    """ADO validation never asks Git credential helpers for authentication."""

    def test_pat_401_uses_resolver_bearer_fallback_without_native_helper(self):
        pat_ctx = _make_auth_ctx(token="stale-pat", auth_scheme="basic")
        resolver = _make_resolver(pat_ctx)
        bearer_calls: list[str] = []
        outcomes = [
            MagicMock(
                returncode=128,
                stderr="fatal: Authentication failed (401)",
                stdout="",
            ),
            MagicMock(returncode=0, stderr="", stdout="refs/heads/main"),
        ]

        def execute_with_bearer_fallback(dep_ref, primary_op, bearer_op, is_auth_failure):
            primary = primary_op()
            assert is_auth_failure(primary) is True
            fallback = bearer_op("selected-az-bearer")
            bearer_calls.append("selected-az-bearer")
            return type(
                "Outcome",
                (),
                {"outcome": fallback, "bearer_attempted": True},
            )()

        resolver.execute_with_bearer_fallback.side_effect = execute_with_bearer_fallback
        resolver.build_native_git_credential_env.side_effect = AssertionError(
            "ADO validation must not construct a native helper environment"
        )

        from apm_cli.install.validation import _validate_package_exists

        with patch(
            "apm_cli.utils.git_env.git_remote_refs",
            side_effect=outcomes,
        ) as remote:
            result = _validate_package_exists(
                "dev.azure.com/myorg/myproject/_git/myrepo",
                auth_resolver=resolver,
            )

        assert result is True
        assert bearer_calls == ["selected-az-bearer"]
        assert remote.call_count == 2
        resolver.execute_with_bearer_fallback.assert_called_once()
        resolver.build_native_git_credential_env.assert_not_called()
        fallback_env = remote.call_args_list[1].kwargs["env"]
        values = {
            value for key, value in fallback_env.items() if key.startswith("GIT_CONFIG_VALUE_")
        }
        assert "Authorization: Bearer selected-az-bearer" in values

    def test_both_401_failures_report_that_bearer_was_attempted(self):
        resolver = _make_resolver(_make_auth_ctx(token="stale-pat", auth_scheme="basic"))
        failed = MagicMock(
            returncode=128,
            stderr="fatal: Authentication failed (401)",
            stdout="",
        )

        def attempted_fallback(_dep, primary_op, _bearer_op, _is_auth_failure):
            return BearerFallbackOutcome(primary_op(), True)

        resolver.execute_with_bearer_fallback.side_effect = attempted_fallback

        from apm_cli.install.validation import _validate_package_exists

        with (
            patch("apm_cli.utils.git_env.git_remote_refs", return_value=failed),
            pytest.raises(AuthenticationError),
        ):
            _validate_package_exists(
                "dev.azure.com/myorg/myproject/_git/myrepo",
                auth_resolver=resolver,
            )

        assert resolver.build_error_context.call_args.kwargs["bearer_also_failed"] is True


class TestAdoNonAuthFailureReturnsFalse:
    """DNS / timeout / 404 returns False (no exception)."""

    @patch("subprocess.run")
    def test_dns_failure_returns_false(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=128,
            stderr="fatal: unable to access '...': Could not resolve host: dev.azure.com",
            stdout="",
        )
        resolver = _make_resolver()

        from apm_cli.install.validation import _validate_package_exists

        result = _validate_package_exists(
            "dev.azure.com/myorg/myproject/_git/myrepo",
            auth_resolver=resolver,
        )
        assert result is False

    def test_verbose_git_stderr_redacts_bare_pat(self):
        secret = "github_pat_" + "A" * 30
        resolver = _make_resolver(_make_auth_ctx(token="bearer", auth_scheme="bearer"))
        logs: list[str] = []

        from apm_cli.install.validation import _validate_ado_git_package

        with patch(
            "apm_cli.utils.git_env.git_remote_refs",
            return_value=subprocess.CompletedProcess(
                ["git", "ls-remote"],
                128,
                stdout="",
                stderr=f"fatal: network failure while processing {secret}",
            ),
        ):
            result = _validate_ado_git_package(
                _make_dep_ref(),
                resolver,
                logs.append,
                "my-package",
                None,
            )

        assert result is False
        assert secret not in "\n".join(logs)


class TestPatRegressionBasicScheme:
    """PAT users use a Basic header with a credential-free URL."""

    @patch("subprocess.run")
    def test_basic_scheme_uses_header_not_url(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        pat_ctx = _make_auth_ctx(token="my-pat-token", auth_scheme="basic")
        resolver = _make_resolver(pat_ctx)

        from apm_cli.install.validation import _validate_package_exists

        _validate_package_exists(
            "dev.azure.com/myorg/myproject/_git/myrepo",
            auth_resolver=resolver,
        )

        call_args = mock_run.call_args
        probe_url = call_args[0][0][-1]
        parsed = urllib.parse.urlparse(probe_url)
        assert parsed.username is None
        assert parsed.password is None
        env = call_args.kwargs["env"]
        values = [
            value
            for key, value in env.items()
            if key.startswith("GIT_CONFIG_VALUE_") and value.startswith("Authorization: Basic ")
        ]
        assert len(values) == 1
        encoded = values[0].split(" ", 2)[2]
        assert base64.b64decode(encoded).decode() == ":my-pat-token"
