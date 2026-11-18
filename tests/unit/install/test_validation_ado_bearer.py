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

from apm_cli.core.auth import AuthResolver
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
    resolver.classify_host.side_effect = AuthResolver.classify_host

    def _single_attempt(_host, operation, **_kwargs):
        return operation(
            auth_ctx.token,
            AuthResolver.git_env_for_context(auth_ctx, base_env=auth_ctx.git_env),
        )

    resolver.try_with_fallback.side_effect = _single_attempt
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


class _AvailableBearerProvider:
    def is_available(self) -> bool:
        return True

    def get_bearer_token(self) -> str:
        return "selected-az-bearer"

    last_error = None


class TestAdoValidationCanonicalFallback:
    """ADO validation walks the AuthResolver chain and never asks native helpers."""

    def test_pat_401_uses_resolver_bearer_step_without_native_helper(self, monkeypatch):
        monkeypatch.setenv("ADO_APM_PAT", "stale-pat")
        provider = _AvailableBearerProvider()
        monkeypatch.setattr("apm_cli.core.azure_cli.get_bearer_provider", lambda: provider)
        monkeypatch.setattr(
            AuthResolver,
            "build_native_git_credential_env",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("ADO validation must not construct a native helper environment")
            ),
        )
        outcomes = [
            subprocess.CompletedProcess(
                ["git"], 128, stdout="", stderr="fatal: Authentication failed (401)"
            ),
            subprocess.CompletedProcess(["git"], 0, stdout="abc\trefs/heads/main", stderr=""),
        ]

        from apm_cli.install.validation import _validate_package_exists

        with patch("apm_cli.utils.git_env.git_remote_refs", side_effect=outcomes) as remote:
            result = _validate_package_exists(
                "dev.azure.com/myorg/myproject/_git/myrepo",
                auth_resolver=AuthResolver(),
            )

        assert result is True
        assert remote.call_count == 2
        fallback_env = remote.call_args_list[1].kwargs["env"]
        values = {
            value for key, value in fallback_env.items() if key.startswith("GIT_CONFIG_VALUE_")
        }
        assert "Authorization: Bearer selected-az-bearer" in values

    def test_exhausted_chain_raises_with_attempt_log(self, monkeypatch):
        monkeypatch.setenv("ADO_APM_PAT", "stale-pat")
        provider = _AvailableBearerProvider()
        monkeypatch.setattr("apm_cli.core.azure_cli.get_bearer_provider", lambda: provider)
        monkeypatch.setattr(
            "apm_cli.core.token_manager.GitHubTokenManager.resolve_git_helper_credential",
            staticmethod(lambda *_a, **_k: None),
        )
        failed = subprocess.CompletedProcess(
            ["git"], 128, stdout="", stderr="fatal: Authentication failed (401)"
        )

        from apm_cli.install.validation import _validate_package_exists

        with (
            patch("apm_cli.utils.git_env.git_remote_refs", return_value=failed),
            pytest.raises(AuthenticationError) as raised,
        ):
            _validate_package_exists(
                "dev.azure.com/myorg/myproject/_git/myrepo",
                auth_resolver=AuthResolver(),
            )

        context = raised.value.diagnostic_context
        steps = [
            context.find(name)
            for name in (
                "ADO_APM_PAT was rejected",
                "az CLI bearer was rejected",
                "git credential fill returned no credential",
            )
        ]
        assert all(position >= 0 for position in steps), context
        assert steps == sorted(steps), context


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
