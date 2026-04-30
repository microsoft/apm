"""
Integration tests for AuthResolver.

These tests exercise the resolver end-to-end — classify_host, token resolution,
caching, try_with_fallback, and build_error_context — using real env-var
manipulation rather than deep mocking.

No network access is required; all tests control the environment via
``unittest.mock.patch.dict(os.environ, ...)``.
"""

import os
from unittest.mock import patch

import pytest

from apm_cli.core.auth import AuthResolver, HostInfo  # noqa: F401
from apm_cli.core.token_manager import GitHubTokenManager

# ---------------------------------------------------------------------------
# Gate: only run when APM_E2E_TESTS=1
# ---------------------------------------------------------------------------

E2E_MODE = os.environ.get("APM_E2E_TESTS", "").lower() in ("1", "true", "yes")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not E2E_MODE, reason="Integration tests require APM_E2E_TESTS=1"),
]

# Shared helper: suppress git-credential-fill so env vars are the only source.
_NO_GIT_CRED = patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None)


# ---------------------------------------------------------------------------
# 1. Clean env → no token
# ---------------------------------------------------------------------------


class TestAuthResolverNoEnv:
    def test_auth_resolver_no_env_resolves_none(self):
        """With a completely clean environment the resolver returns no token."""
        with patch.dict(os.environ, {}, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com")

            assert ctx.token is None
            assert ctx.source == "none"
            assert ctx.token_type == "unknown"
            assert ctx.host_info.kind == "github"


# ---------------------------------------------------------------------------
# 2. GITHUB_APM_PAT is picked up
# ---------------------------------------------------------------------------


class TestGlobalPat:
    def test_auth_resolver_respects_github_apm_pat(self):
        """GITHUB_APM_PAT is the primary env var for module access."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_global123"}, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com")

            assert ctx.token == "ghp_global123"
            assert ctx.source == "GITHUB_APM_PAT"
            assert ctx.token_type == "classic"


# ---------------------------------------------------------------------------
# 3. Per-org override takes precedence
# ---------------------------------------------------------------------------


class TestPerOrgOverride:
    def test_auth_resolver_per_org_override(self):
        """GITHUB_APM_PAT_{ORG} beats GITHUB_APM_PAT."""
        env = {
            "GITHUB_APM_PAT": "ghp_global",
            "GITHUB_APM_PAT_CONTOSO": "github_pat_contoso_specific",
        }
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com", org="contoso")

            assert ctx.token == "github_pat_contoso_specific"
            assert ctx.source == "GITHUB_APM_PAT_CONTOSO"
            assert ctx.token_type == "fine-grained"

    def test_per_org_hyphen_normalisation(self):
        """Org names with hyphens are converted to underscores in the env var."""
        env = {"GITHUB_APM_PAT_MY_ORG": "ghp_hyphens"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com", org="my-org")

            assert ctx.token == "ghp_hyphens"
            assert ctx.source == "GITHUB_APM_PAT_MY_ORG"


# ---------------------------------------------------------------------------
# 4. GHE Cloud uses global env vars
# ---------------------------------------------------------------------------


class TestGheCloudGlobalVars:
    def test_auth_resolver_ghe_cloud_uses_global(self):
        """*.ghe.com hosts pick up GITHUB_APM_PAT (global vars apply to all hosts)."""
        env = {"GITHUB_APM_PAT": "ghp_should_not_leak"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            ctx = resolver.resolve("contoso.ghe.com")

            assert ctx.token == "ghp_should_not_leak", (
                "Global GITHUB_APM_PAT should be returned for GHE Cloud hosts"
            )
            assert ctx.source == "GITHUB_APM_PAT"
            assert ctx.host_info.kind == "ghe_cloud"
            assert ctx.host_info.has_public_repos is False

    def test_ghe_cloud_per_org_still_works(self):
        """Per-org tokens work even on GHE Cloud hosts."""
        env = {
            "GITHUB_APM_PAT": "ghp_should_not_leak",
            "GITHUB_APM_PAT_ENTERPRISE_TEAM": "ghp_enterprise",
        }
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            ctx = resolver.resolve("contoso.ghe.com", org="enterprise-team")

            assert ctx.token == "ghp_enterprise"
            assert ctx.source == "GITHUB_APM_PAT_ENTERPRISE_TEAM"

    def test_ghe_cloud_global_var_with_credential_fallback_in_try_with_fallback(self):
        """When a global env-var token fails on GHE Cloud, try_with_fallback
        retries via git credential fill before giving up."""
        env = {"GITHUB_APM_PAT": "wrong-global-token"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                GitHubTokenManager,
                "resolve_credential_from_git",
                return_value="correct-ghe-cred",
            ),
        ):
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                if token == "wrong-global-token":
                    raise RuntimeError("auth failed")
                return "ok"

            result = resolver.try_with_fallback("contoso.ghe.com", op, org="contoso")

            assert result == "ok"
            assert calls == ["wrong-global-token", "correct-ghe-cred"], (
                "Should try global token first, then fall back to git credential fill"
            )


# ---------------------------------------------------------------------------
# 5. Cache consistency
# ---------------------------------------------------------------------------


class TestCacheConsistency:
    def test_auth_resolver_cache_consistency(self):
        """Same (host, org) always returns the same object (identity check)."""
        env = {"GITHUB_APM_PAT": "ghp_cached"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            ctx1 = resolver.resolve("github.com", org="microsoft")
            ctx2 = resolver.resolve("github.com", org="microsoft")

            assert ctx1 is ctx2, "Cached result must be the same object"

    def test_different_keys_are_independent(self):
        """Different (host, org) pairs produce independent cache entries."""
        env = {
            "GITHUB_APM_PAT_ALPHA": "ghp_alpha",
            "GITHUB_APM_PAT_BETA": "ghp_beta",
        }
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            ctx_a = resolver.resolve("github.com", org="alpha")
            ctx_b = resolver.resolve("github.com", org="beta")

            assert ctx_a is not ctx_b
            assert ctx_a.token == "ghp_alpha"
            assert ctx_b.token == "ghp_beta"


# ---------------------------------------------------------------------------
# 6. try_with_fallback: unauth-first for public repos
# ---------------------------------------------------------------------------


class TestTryWithFallbackUnauthFirst:
    def test_try_with_fallback_unauth_first_public(self):
        """unauth_first=True on github.com: unauthenticated call succeeds,
        token is never used."""
        env = {"GITHUB_APM_PAT": "ghp_not_needed"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                return "ok"

            result = resolver.try_with_fallback(
                "github.com", op, org="microsoft", unauth_first=True
            )

            assert result == "ok"
            assert calls == [None], "unauth_first should try None first"

    def test_unauth_first_falls_back_on_failure(self):
        """If unauth fails and a token exists, retry with token."""
        env = {"GITHUB_APM_PAT": "ghp_fallback"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                if token is None:
                    raise RuntimeError("rate-limited")
                return "ok"

            result = resolver.try_with_fallback(
                "github.com", op, org="microsoft", unauth_first=True
            )

            assert result == "ok"
            assert calls == [None, "ghp_fallback"]

    def test_ghe_cloud_never_tries_unauth(self):
        """GHE Cloud hosts skip the unauth attempt entirely."""
        env = {"GITHUB_APM_PAT_CORP": "ghp_corp"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                return "ok"

            result = resolver.try_with_fallback("corp.ghe.com", op, org="corp", unauth_first=True)

            assert result == "ok"
            assert calls == ["ghp_corp"], "GHE Cloud must use auth-only path"


# ---------------------------------------------------------------------------
# 7. classify_host variants
# ---------------------------------------------------------------------------


class TestClassifyHostVariants:
    """End-to-end classification of various host strings."""

    @pytest.mark.parametrize(
        "host, expected_kind, expected_public",
        [
            ("github.com", "github", True),
            ("GitHub.COM", "github", True),
            ("GITHUB.com", "github", True),
            ("contoso.ghe.com", "ghe_cloud", False),
            ("ACME.GHE.COM", "ghe_cloud", False),
            ("dev.azure.com", "ado", True),
            ("myorg.visualstudio.com", "ado", True),
            ("gitlab.com", "generic", True),
            ("bitbucket.org", "generic", True),
            ("git.internal.corp", "generic", True),
        ],
    )
    def test_classify_host_variants(self, host, expected_kind, expected_public):
        # Clear GITHUB_HOST so GHES detection doesn't interfere
        with patch.dict(os.environ, {}, clear=True):
            hi = AuthResolver.classify_host(host)
            assert hi.kind == expected_kind, f"{host} → expected {expected_kind}, got {hi.kind}"
            assert hi.has_public_repos is expected_public

    def test_ghes_via_github_host_env(self):
        """GITHUB_HOST pointing at a custom FQDN triggers GHES classification."""
        with patch.dict(os.environ, {"GITHUB_HOST": "github.mycompany.com"}, clear=True):
            hi = AuthResolver.classify_host("github.mycompany.com")
            assert hi.kind == "ghes"
            assert hi.has_public_repos is True
            assert "api/v3" in hi.api_base

    def test_api_base_values(self):
        """Verify API base URLs for each host kind."""
        with patch.dict(os.environ, {}, clear=True):
            assert AuthResolver.classify_host("github.com").api_base == "https://api.github.com"
            assert (
                AuthResolver.classify_host("acme.ghe.com").api_base == "https://acme.ghe.com/api/v3"
            )
            assert AuthResolver.classify_host("dev.azure.com").api_base == "https://dev.azure.com"


# ---------------------------------------------------------------------------
# 8. build_error_context integration
# ---------------------------------------------------------------------------


class TestBuildErrorContextIntegration:
    """Verify error messages are actionable under realistic conditions."""

    def test_no_token_suggests_env_vars(self):
        with patch.dict(os.environ, {}, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            msg = resolver.build_error_context("github.com", "install")

            assert "GITHUB_APM_PAT" in msg
            assert "--verbose" in msg

    def test_github_com_error_mentions_emu_sso(self):
        """github.com errors should mention EMU/SSO as possible causes."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_some_token"}, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            msg = resolver.build_error_context("github.com", "clone")

            assert "EMU" in msg or "SAML" in msg

    def test_org_hint_included(self):
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_tok"}, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            msg = resolver.build_error_context("github.com", "clone", org="contoso")

            assert "GITHUB_APM_PAT_CONTOSO" in msg
