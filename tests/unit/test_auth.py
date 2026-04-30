"""Unit tests for AuthResolver, HostInfo, and AuthContext."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from apm_cli.core import azure_cli as _azure_cli_mod
from apm_cli.core.auth import AuthContext, AuthResolver, HostInfo  # noqa: F401
from apm_cli.core.token_manager import GitHubTokenManager


@pytest.fixture(autouse=True)
def _reset_bearer_singleton():
    """Reset AzureCliBearerProvider singleton between tests so per-test
    mocks of the class take effect (B3 #852)."""
    _azure_cli_mod._provider_singleton = None
    yield
    _azure_cli_mod._provider_singleton = None


# ---------------------------------------------------------------------------
# TestClassifyHost
# ---------------------------------------------------------------------------


class TestClassifyHost:
    def test_github_com(self):
        hi = AuthResolver.classify_host("github.com")
        assert hi.kind == "github"
        assert hi.has_public_repos is True
        assert hi.api_base == "https://api.github.com"

    def test_ghe_cloud(self):
        hi = AuthResolver.classify_host("contoso.ghe.com")
        assert hi.kind == "ghe_cloud"
        assert hi.has_public_repos is False
        assert hi.api_base == "https://contoso.ghe.com/api/v3"

    def test_ado(self):
        hi = AuthResolver.classify_host("dev.azure.com")
        assert hi.kind == "ado"

    def test_visualstudio(self):
        hi = AuthResolver.classify_host("myorg.visualstudio.com")
        assert hi.kind == "ado"

    def test_ghes_via_env(self):
        """GITHUB_HOST set to a custom FQDN → GHES."""
        with patch.dict(os.environ, {"GITHUB_HOST": "github.mycompany.com"}):
            hi = AuthResolver.classify_host("github.mycompany.com")
            assert hi.kind == "ghes"

    def test_generic_fqdn(self):
        hi = AuthResolver.classify_host("gitlab.com")
        assert hi.kind == "generic"

    def test_case_insensitive(self):
        hi = AuthResolver.classify_host("GitHub.COM")
        assert hi.kind == "github"


# ---------------------------------------------------------------------------
# TestDetectTokenType
# ---------------------------------------------------------------------------


class TestDetectTokenType:
    def test_fine_grained(self):
        assert AuthResolver.detect_token_type("github_pat_abc123") == "fine-grained"

    def test_classic(self):
        assert AuthResolver.detect_token_type("ghp_abc123") == "classic"

    def test_oauth_user(self):
        assert AuthResolver.detect_token_type("ghu_abc123") == "oauth"

    def test_oauth_app(self):
        assert AuthResolver.detect_token_type("gho_abc123") == "oauth"

    def test_github_app_install(self):
        assert AuthResolver.detect_token_type("ghs_abc123") == "github-app"

    def test_github_app_refresh(self):
        assert AuthResolver.detect_token_type("ghr_abc123") == "github-app"

    def test_unknown(self):
        assert AuthResolver.detect_token_type("some-random-token") == "unknown"


# ---------------------------------------------------------------------------
# TestResolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_per_org_env_var(self):
        """GITHUB_APM_PAT_MICROSOFT takes precedence for org 'microsoft'."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_APM_PAT_MICROSOFT": "org-specific-token",
                "GITHUB_APM_PAT": "global-token",
            },
            clear=False,
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com", org="microsoft")
            assert ctx.token == "org-specific-token"
            assert ctx.source == "GITHUB_APM_PAT_MICROSOFT"

    def test_per_org_with_hyphens(self):
        """Org name with hyphens → underscores in env var."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_APM_PAT_CONTOSO_MICROSOFT": "emu-token",
            },
            clear=False,
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com", org="contoso-microsoft")
            assert ctx.token == "emu-token"
            assert ctx.source == "GITHUB_APM_PAT_CONTOSO_MICROSOFT"

    def test_falls_back_to_global(self):
        """No per-org var → falls back to GITHUB_APM_PAT."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_APM_PAT": "global-token",
            },
            clear=True,
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com", org="unknown-org")
            assert ctx.token == "global-token"
            assert ctx.source == "GITHUB_APM_PAT"

    def test_no_token_returns_none(self):
        """No tokens at all → token is None."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                ctx = resolver.resolve("github.com")
                assert ctx.token is None
                assert ctx.source == "none"

    def test_caching(self):
        """Second call returns cached result."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                ctx1 = resolver.resolve("github.com", org="microsoft")
                ctx2 = resolver.resolve("github.com", org="microsoft")
                assert ctx1 is ctx2

    def test_caching_is_singleflight_under_concurrency(self):
        """Concurrent resolve() calls for the same key should populate cache once."""
        resolver = AuthResolver()

        def _slow_resolve_token(host_info, org):
            time.sleep(0.05)
            return ("cred-token", "git-credential-fill", "basic")

        with (
            patch.object(
                AuthResolver, "_resolve_token", side_effect=_slow_resolve_token
            ) as mock_resolve,
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            futures = [pool.submit(resolver.resolve, "github.com", "microsoft") for _ in range(8)]
            contexts = [f.result() for f in futures]

        assert mock_resolve.call_count == 1
        assert all(ctx is contexts[0] for ctx in contexts)

    def test_different_orgs_different_cache(self):
        """Different orgs get different cache entries."""
        with (
            patch.dict(
                os.environ,
                {
                    "GITHUB_APM_PAT_ORG_A": "token-a",
                    "GITHUB_APM_PAT_ORG_B": "token-b",
                },
                clear=True,
            ),
            patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None),
        ):
            resolver = AuthResolver()
            ctx_a = resolver.resolve("github.com", org="org-a")
            ctx_b = resolver.resolve("github.com", org="org-b")
            assert ctx_a.token == "token-a"
            assert ctx_b.token == "token-b"

    def test_ado_token(self):
        """ADO host resolves ADO_APM_PAT."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "ado-token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                ctx = resolver.resolve("dev.azure.com")
                assert ctx.token == "ado-token"

    def test_credential_fallback(self):
        """Falls back to git credential helper when no env vars."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="cred-token"
            ),
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com")
            assert ctx.token == "cred-token"
            assert ctx.source == "git-credential-fill"

    def test_global_var_resolves_for_non_default_host(self):
        """GITHUB_APM_PAT resolves for *.ghe.com (any host, not just default)."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "global-token"}, clear=True):
            resolver = AuthResolver()
            ctx = resolver.resolve("contoso.ghe.com")
            assert ctx.token == "global-token"
            assert ctx.source == "GITHUB_APM_PAT"

    def test_global_var_resolves_for_ghes_host(self):
        """GITHUB_APM_PAT resolves for a GHES host set via GITHUB_HOST."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_HOST": "github.mycompany.com",
                "GITHUB_APM_PAT": "global-token",
            },
            clear=True,
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.mycompany.com")
            assert ctx.token == "global-token"
            assert ctx.source == "GITHUB_APM_PAT"
            assert ctx.host_info.kind == "ghes"

    def test_git_env_has_lockdown(self):
        """Resolved context has git security env vars."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                ctx = resolver.resolve("github.com")
                assert ctx.git_env.get("GIT_TERMINAL_PROMPT") == "0"


# ---------------------------------------------------------------------------
# TestTryWithFallback
# ---------------------------------------------------------------------------


class TestTryWithFallback:
    def test_unauth_first_succeeds(self):
        """Unauth-first: if unauth works, auth is never tried."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                calls = []

                def op(token, env):
                    calls.append(token)
                    return "success"

                result = resolver.try_with_fallback("github.com", op, unauth_first=True)
                assert result == "success"
                assert calls == [None]

    def test_unauth_first_falls_back_to_auth(self):
        """Unauth-first: if unauth fails, retries with token."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                calls = []

                def op(token, env):
                    calls.append(token)
                    if token is None:
                        raise RuntimeError("Unauthorized")
                    return "success"

                result = resolver.try_with_fallback("github.com", op, unauth_first=True)
                assert result == "success"
                assert calls == [None, "token"]

    def test_ghe_cloud_auth_only(self):
        """*.ghe.com: auth-only, no unauth fallback.  Uses global env var."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "global-token"}, clear=True):
            resolver = AuthResolver()
            calls = []

            def op(token, env):
                calls.append(token)
                return "success"

            result = resolver.try_with_fallback("contoso.ghe.com", op, unauth_first=True)
            assert result == "success"
            # GHE Cloud has no public repos → unauth skipped, auth called once
            assert calls == ["global-token"]

    def test_auth_first_succeeds(self):
        """Auth-first (default): auth works, unauth not tried."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                calls = []

                def op(token, env):
                    calls.append(token)
                    return "success"

                result = resolver.try_with_fallback("github.com", op)
                assert result == "success"
                assert calls == ["token"]

    def test_auth_first_falls_back_to_unauth(self):
        """Auth-first: if auth fails on public host, retries unauthenticated."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                calls = []

                def op(token, env):
                    calls.append(token)
                    if token is not None:
                        raise RuntimeError("Token expired")
                    return "success"

                result = resolver.try_with_fallback("github.com", op)
                assert result == "success"
                assert calls == ["token", None]

    def test_no_token_tries_unauth(self):
        """No token available: tries unauthenticated directly."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                calls = []

                def op(token, env):
                    calls.append(token)
                    return "success"

                result = resolver.try_with_fallback("github.com", op)
                assert result == "success"
                assert calls == [None]

    def test_credential_fallback_when_env_token_fails(self):
        """Env token fails on auth-only host → retries with git credential fill."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "wrong-token"}, clear=True):
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="correct-cred"
            ):
                resolver = AuthResolver()
                calls = []

                def op(token, env):
                    calls.append(token)
                    if token == "wrong-token":
                        raise RuntimeError("Bad credentials")
                    return "success"

                result = resolver.try_with_fallback("contoso.ghe.com", op)
                assert result == "success"
                assert calls == ["wrong-token", "correct-cred"]

    def test_no_credential_fallback_when_source_is_credential(self):
        """When token already came from git-credential-fill, no retry on failure."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="cred-token"
            ),
        ):
            resolver = AuthResolver()

            def op(token, env):
                raise RuntimeError("Bad credentials")

            with pytest.raises(RuntimeError, match="Bad credentials"):
                resolver.try_with_fallback("contoso.ghe.com", op)

    def test_credential_fallback_on_auth_first_path(self):
        """Auth-first on public host: auth fails, unauth fails → credential fill kicks in."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "wrong-token"}, clear=True):
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="correct-cred"
            ):
                resolver = AuthResolver()
                calls = []

                def op(token, env):
                    calls.append(token)
                    if token in ("wrong-token", None):
                        raise RuntimeError("Failed")
                    return "success"

                result = resolver.try_with_fallback("github.com", op)
                assert result == "success"
                # auth-first → unauth fallback → credential fill
                assert calls == ["wrong-token", None, "correct-cred"]

    def test_verbose_callback(self):
        """verbose_callback is called at each step."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                messages = []

                def op(token, env):
                    return "ok"

                resolver.try_with_fallback("github.com", op, verbose_callback=messages.append)
                assert len(messages) > 0


# ---------------------------------------------------------------------------
# TestBuildErrorContext
# ---------------------------------------------------------------------------


class TestBuildErrorContext:
    def test_no_token_message(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("github.com", "clone")
                assert "GITHUB_APM_PAT" in msg
                assert "--verbose" in msg

    def test_ghe_cloud_error_context(self):
        """*.ghe.com errors mention enterprise-scoped tokens."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT_CONTOSO": "token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("contoso.ghe.com", "clone", org="contoso")
                assert "enterprise" in msg.lower()

    def test_github_com_error_mentions_emu(self):
        """github.com errors mention EMU/SSO possibility."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("github.com", "clone")
                assert "EMU" in msg or "SAML" in msg

    def test_multi_org_hint(self):
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "token"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("github.com", "clone", org="microsoft")
                assert "GITHUB_APM_PAT_MICROSOFT" in msg

    def test_token_present_shows_source(self):
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_tok"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("github.com", "clone")
                assert "GITHUB_APM_PAT" in msg
                assert "SAML SSO" in msg


# ---------------------------------------------------------------------------
# TestBuildErrorContextADO
# ---------------------------------------------------------------------------


class TestBuildErrorContextADO:
    """build_error_context must give ADO-specific guidance for dev.azure.com hosts.

    Issue #625: missing ADO_APM_PAT is described with a generic GitHub error
    message instead of pointing the user at ADO_APM_PAT and Code (Read) scope.

    Now includes adaptive error cases based on az CLI availability (issue #852).
    """

    def test_ado_no_token_no_az_mentions_ado_pat(self):
        """No ADO_APM_PAT, no az CLI -> Case 1: error message must mention ADO_APM_PAT."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider_cls.return_value.is_available.return_value = False
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone", org="myorg")
                    assert "ADO_APM_PAT" in msg, (
                        f"Expected 'ADO_APM_PAT' in error message, got:\n{msg}"
                    )

    def test_ado_no_token_does_not_suggest_github_remediation(self):
        """ADO error must not suggest GitHub-specific remediation steps."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider_cls.return_value.is_available.return_value = False
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone", org="myorg")
                    assert "gh auth login" not in msg, (
                        f"ADO error message should not mention 'gh auth login', got:\n{msg}"
                    )
                    assert "GITHUB_TOKEN" not in msg, (
                        f"ADO error message should not mention 'GITHUB_TOKEN', got:\n{msg}"
                    )
                    assert "GITHUB_APM_PAT_MYORG" not in msg, (
                        "ADO error message should not mention per-org GitHub PAT hint "
                        f"'GITHUB_APM_PAT_MYORG', got:\n{msg}"
                    )

    def test_ado_no_token_mentions_code_read_scope(self):
        """ADO error must mention Code (Read) scope so user knows what PAT scope to set."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider_cls.return_value.is_available.return_value = False
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone", org="myorg")
                    assert "Code" in msg or "read" in msg.lower(), (
                        f"Expected Code (Read) scope guidance in error message, got:\n{msg}"
                    )

    def test_ado_no_org_no_token_mentions_ado_pat(self):
        """No org argument, no ADO_APM_PAT -> error message must still mention ADO_APM_PAT."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider_cls.return_value.is_available.return_value = False
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone")
                    assert "ADO_APM_PAT" in msg, (
                        f"Expected 'ADO_APM_PAT' in error message, got:\n{msg}"
                    )

    def test_ado_with_token_still_shows_source(self):
        """When an ADO token IS present but clone fails, source info is shown."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "mypat"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider_cls.return_value.is_available.return_value = False
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone", org="myorg")
                    assert "ADO_APM_PAT" in msg, (
                        f"Expected token source 'ADO_APM_PAT' in error message, got:\n{msg}"
                    )

    def test_ado_with_token_mentions_scope_guidance(self):
        """When an ADO token is present but auth fails, PAT validity/scope hint is shown."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "mypat"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider_cls.return_value.is_available.return_value = False
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone", org="myorg")
                    assert "Code (Read)" in msg, (
                        f"Expected Code (Read) scope guidance in error message, got:\n{msg}"
                    )

    def test_ado_with_token_does_not_suggest_github_remediation(self):
        """When an ADO token is present but auth fails, GitHub SAML guidance must not appear."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "mypat"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider_cls.return_value.is_available.return_value = False
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone", org="myorg")
                    assert "SAML" not in msg, f"ADO error should not mention SAML, got:\n{msg}"
                    assert "github.com/settings/tokens" not in msg, (
                        f"ADO error should not mention github.com/settings/tokens, got:\n{msg}"
                    )

    def test_visualstudio_com_gets_ado_remediation(self):
        """Legacy *.visualstudio.com hosts are also ADO and must get ADO-specific guidance."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider_cls.return_value.is_available.return_value = False
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("myorg.visualstudio.com", "clone")
                    assert "ADO_APM_PAT" in msg, (
                        f"Expected 'ADO_APM_PAT' in error message, got:\n{msg}"
                    )
                    assert "gh auth login" not in msg, (
                        f"ADO error should not mention 'gh auth login', got:\n{msg}"
                    )
                    assert "SAML" not in msg, f"ADO error should not mention SAML, got:\n{msg}"

    def test_ado_no_pat_az_available_not_logged_in(self):
        """Case 3: no PAT, az on PATH but not logged in -> suggest az login."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider = mock_provider_cls.return_value
                    mock_provider.is_available.return_value = True
                    mock_provider.get_current_tenant_id.return_value = None
                    from apm_cli.core.azure_cli import AzureCliBearerError

                    mock_provider.get_bearer_token.side_effect = AzureCliBearerError(
                        "not logged in", kind="not_logged_in"
                    )
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone")
                    assert "az login" in msg
                    assert "ADO_APM_PAT" in msg

    def test_ado_no_pat_az_available_logged_in_but_rejected(self):
        """Case 2: no PAT, az logged in, bearer acquired but ADO rejected it."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider = mock_provider_cls.return_value
                    mock_provider.is_available.return_value = True
                    mock_provider.get_bearer_token.return_value = "eyJfake"
                    mock_provider.get_current_tenant_id.return_value = "abc-123"
                    resolver = AuthResolver()
                    # Force cache clear so resolve uses the mocked bearer
                    resolver._cache.clear()
                    msg = resolver.build_error_context("dev.azure.com", "clone")
                    assert "tenant" in msg.lower()
                    assert "az account show" in msg

    def test_ado_pat_set_az_available_case4(self):
        """Case 4: PAT set + az available -> both rejected."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "expired-pat"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider = mock_provider_cls.return_value
                    mock_provider.is_available.return_value = True
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone")
                    assert "unset ADO_APM_PAT" in msg
                    assert "az login" in msg


# ---------------------------------------------------------------------------
# TestHostInfoPort -- port field + display_name property
# ---------------------------------------------------------------------------


class TestHostInfoPort:
    def test_port_defaults_to_none(self):
        hi = HostInfo(host="github.com", kind="github", has_public_repos=True, api_base="x")
        assert hi.port is None

    def test_display_name_without_port(self):
        hi = HostInfo(host="github.com", kind="github", has_public_repos=True, api_base="x")
        assert hi.display_name == "github.com"

    def test_display_name_with_port(self):
        hi = HostInfo(
            host="bitbucket.corp.com",
            kind="generic",
            has_public_repos=True,
            api_base="x",
            port=7999,
        )
        assert hi.display_name == "bitbucket.corp.com:7999"

    def test_classify_host_attaches_port(self):
        hi = AuthResolver.classify_host("bitbucket.corp.com", port=7999)
        assert hi.kind == "generic"
        assert hi.port == 7999
        assert hi.display_name == "bitbucket.corp.com:7999"

    def test_classify_host_port_is_transport_agnostic(self):
        """Port does not influence host-kind classification."""
        # github.com on a weird port is still 'github', not 'generic'.
        hi = AuthResolver.classify_host("github.com", port=8443)
        assert hi.kind == "github"
        assert hi.port == 8443


# ---------------------------------------------------------------------------
# TestResolvePortDiscrimination -- same host, different ports must not
# collapse into one cache entry and must return each port's credential.
# ---------------------------------------------------------------------------


class TestResolvePortDiscrimination:
    def test_same_host_different_ports_are_separate_cache_entries(self):
        """Widened cache key: (host, port, org) discriminates by port."""
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            calls: list = []

            def fake_cred(host, port=None):
                calls.append((host, port))
                return f"tok-{host}-{port}"

            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", side_effect=fake_cred
            ):
                ctx_a = resolver.resolve("bitbucket.corp.com", port=7990)
                ctx_b = resolver.resolve("bitbucket.corp.com", port=7991)

        assert ctx_a.token == "tok-bitbucket.corp.com-7990"
        assert ctx_b.token == "tok-bitbucket.corp.com-7991"
        assert ctx_a is not ctx_b
        assert calls == [
            ("bitbucket.corp.com", 7990),
            ("bitbucket.corp.com", 7991),
        ]

    def test_same_port_hits_cache(self):
        """Calling resolve() twice with the same (host, port, org) hits the cache."""
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()

            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="tok"
            ) as mock_cred:
                ctx_1 = resolver.resolve("bitbucket.corp.com", port=7990)
                ctx_2 = resolver.resolve("bitbucket.corp.com", port=7990)

        assert ctx_1 is ctx_2
        assert mock_cred.call_count == 1

    def test_port_none_vs_port_set_are_separate(self):
        """resolve(host) and resolve(host, port=443) produce distinct entries."""
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()

            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="tok"
            ) as mock_cred:
                resolver.resolve("bitbucket.corp.com")
                resolver.resolve("bitbucket.corp.com", port=443)

        assert mock_cred.call_count == 2

    def test_resolve_for_dep_threads_port(self):
        """resolve_for_dep propagates dep_ref.port into the resolver."""
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("ssh://git@bitbucket.corp.com:7999/team/repo.git")
        assert dep.port == 7999

        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="p"
            ) as mock_cred:
                ctx = resolver.resolve_for_dep(dep)

        assert ctx.host_info.port == 7999
        assert ctx.host_info.display_name == "bitbucket.corp.com:7999"
        mock_cred.assert_called_once_with("bitbucket.corp.com", port=7999)

    def test_resolve_for_dep_threads_port_from_https_url(self):
        """https://host:port/... also carries the port into the resolver."""
        from apm_cli.models.dependency.reference import DependencyReference

        dep = DependencyReference.parse("https://bitbucket.corp.com:7990/team/repo.git")
        assert dep.port == 7990

        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="p"
            ) as mock_cred:
                ctx = resolver.resolve_for_dep(dep)

        assert ctx.host_info.port == 7990
        mock_cred.assert_called_once_with("bitbucket.corp.com", port=7990)

    def test_host_info_carries_port(self):
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "t"}, clear=True):
            resolver = AuthResolver()
            ctx = resolver.resolve("gitlab.corp.com", port=8443)
            assert ctx.host_info.port == 8443
            assert ctx.host_info.display_name == "gitlab.corp.com:8443"


# ---------------------------------------------------------------------------
# TestBuildErrorContextWithPort
# ---------------------------------------------------------------------------


class TestBuildErrorContextWithPort:
    def test_error_message_uses_display_name(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("bitbucket.corp.com", "clone", port=7999)
        # Anchor with surrounding context tokens (" on " before, "." after)
        # so the assertion pins the rendered position rather than just the
        # substring's existence anywhere -- and so CodeQL's
        # py/incomplete-url-substring-sanitization heuristic does not
        # mistake a test assertion for unsafe URL sanitization.
        assert "Authentication failed for clone on bitbucket.corp.com:7999." in msg

    def test_port_hint_appears_when_port_set(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("bitbucket.corp.com", "clone", port=7999)
        assert "per-port" in msg, f"Expected per-port hint when port is set, got:\n{msg}"

    def test_no_port_hint_when_port_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("github.com", "clone")
        assert "per-port" not in msg


# ---------------------------------------------------------------------------
# TestTryWithFallbackWithPort
# ---------------------------------------------------------------------------


class TestTryWithFallbackWithPort:
    def test_port_threads_into_credential_fallback(self):
        """When env token fails on ghe_cloud, credential fill is called with port."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "bad"}, clear=True):
            captured: list = []

            def fake_cred(host, port=None):
                captured.append((host, port))
                return "good"

            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", side_effect=fake_cred
            ):
                resolver = AuthResolver()

                def op(token, env):
                    if token == "bad":
                        raise RuntimeError("rejected")
                    return "ok"

                result = resolver.try_with_fallback("contoso.ghe.com", op, port=8443)
        assert result == "ok"
        assert captured == [("contoso.ghe.com", 8443)]
