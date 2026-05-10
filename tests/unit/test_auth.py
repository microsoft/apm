"""Unit tests for AuthResolver, HostInfo, and AuthContext."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from apm_cli.core import azure_cli as _azure_cli_mod
from apm_cli.core.auth import AuthResolver, HostInfo
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.models.dependency.reference import DependencyReference


@pytest.fixture(autouse=True)
def _reset_bearer_singleton():
    """Reset AzureCliBearerProvider singleton between tests so per-test
    mocks of the class take effect (B3 #852)."""
    _azure_cli_mod._provider_singleton = None
    yield
    _azure_cli_mod._provider_singleton = None


@pytest.fixture(autouse=True)
def _disable_gh_cli_fallback():
    """Keep auth tests deterministic regardless of local gh login state."""
    with patch.object(GitHubTokenManager, "resolve_credential_from_gh_cli", return_value=None):
        yield


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

    def test_gitlab_com(self):
        hi = AuthResolver.classify_host("gitlab.com")
        assert hi.kind == "gitlab"
        assert hi.api_base == "https://gitlab.com/api/v4"
        assert hi.has_public_repos is True

    def test_gitlab_com_not_ghes_even_if_github_host_env_set(self):
        """gitlab.com is well-known SaaS; do not treat as GHES when GITHUB_HOST matches."""
        with patch.dict(os.environ, {"GITHUB_HOST": "gitlab.com"}, clear=False):
            hi = AuthResolver.classify_host("gitlab.com")
            assert hi.kind == "gitlab"
            assert hi.api_base == "https://gitlab.com/api/v4"

    def test_gitlab_self_managed_gitlab_host_env(self):
        with patch.dict(os.environ, {"GITLAB_HOST": "git.corp.example.com"}, clear=False):
            hi = AuthResolver.classify_host("git.corp.example.com")
            assert hi.kind == "gitlab"
            assert hi.api_base == "https://git.corp.example.com/api/v4"

    def test_gitlab_self_managed_apm_gitlab_hosts_env(self):
        with patch.dict(
            os.environ,
            {"APM_GITLAB_HOSTS": "git.epam.com, gitlab.corp.io"},
            clear=False,
        ):
            hi = AuthResolver.classify_host("gitlab.corp.io")
            assert hi.kind == "gitlab"
            assert hi.api_base == "https://gitlab.corp.io/api/v4"

    def test_ghes_wins_over_gitlab_when_same_host_in_both_envs(self):
        """GITHUB_HOST match must not be reclassified as GitLab (spec Critical Rules)."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_HOST": "git.company.com",
                "APM_GITLAB_HOSTS": "git.company.com",
            },
            clear=False,
        ):
            hi = AuthResolver.classify_host("git.company.com")
            assert hi.kind == "ghes"
            assert "api/v3" in hi.api_base

    def test_generic_fqdn_not_in_gitlab_allowlist(self):
        hi = AuthResolver.classify_host("bitbucket.org")
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
# TestGitlabRestHeaders
# ---------------------------------------------------------------------------


class TestGitlabRestHeaders:
    def test_no_token_returns_empty_dict(self):
        assert AuthResolver.gitlab_rest_headers(None) == {}
        assert AuthResolver.gitlab_rest_headers("") == {}

    def test_pat_uses_private_token_header(self):
        headers = AuthResolver.gitlab_rest_headers("glpat-secret")
        assert headers == {"PRIVATE-TOKEN": "glpat-secret"}

    def test_oauth_bearer_style(self):
        headers = AuthResolver.gitlab_rest_headers("oauth-access-token", oauth_bearer=True)
        assert headers == {"Authorization": "Bearer oauth-access-token"}


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

        with patch.object(
            AuthResolver, "_resolve_token", side_effect=_slow_resolve_token
        ) as mock_resolve:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [
                    pool.submit(resolver.resolve, "github.com", "microsoft") for _ in range(8)
                ]
                contexts = [f.result() for f in futures]

        assert mock_resolve.call_count == 1
        assert all(ctx is contexts[0] for ctx in contexts)

    def test_different_orgs_different_cache(self):
        """Different orgs get different cache entries."""
        with patch.dict(
            os.environ,
            {
                "GITHUB_APM_PAT_ORG_A": "token-a",
                "GITHUB_APM_PAT_ORG_B": "token-b",
            },
            clear=True,
        ):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
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
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="cred-token"
            ):
                resolver = AuthResolver()
                ctx = resolver.resolve("github.com")
                assert ctx.token == "cred-token"
                assert ctx.source == "git-credential-fill"

    def test_gh_cli_source_label(self):
        """When gh CLI supplies the token, ctx.source == 'gh-auth-token'."""
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                GitHubTokenManager,
                "resolve_credential_from_gh_cli",
                return_value="gho_cli_token",
            ),
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com")
            assert ctx.token == "gho_cli_token"
            assert ctx.source == "gh-auth-token"

    def test_try_with_fallback_uses_gh_cli(self):
        """try_with_fallback retries via gh CLI before git credential fill."""
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "stale-token"}, clear=True),
            patch.object(
                GitHubTokenManager,
                "resolve_credential_from_gh_cli",
                return_value="gho_fresh",
            ),
            patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value=None
            ) as mock_cred,
        ):
            resolver = AuthResolver()
            attempts = []

            def op(token, env):
                attempts.append(token)
                if token == "gho_fresh":
                    return token
                raise RuntimeError("401 Unauthorized")

            result = resolver.try_with_fallback("github.com", op)
            assert result == "gho_fresh"
            assert attempts == ["stale-token", None, "gho_fresh"]
            # git credential fill must not be reached when gh CLI succeeds.
            mock_cred.assert_not_called()

    def test_resolve_for_dep_uses_standard_credential_fallback(self):
        """Dependency-aware resolution still uses the standard host-based fallback chain."""
        dep_ref = DependencyReference.parse("Devolutions/RDM/.claude/skills/add-culture-rdm")
        with patch.dict(os.environ, {}, clear=True):
            with (
                patch.object(
                    GitHubTokenManager,
                    "resolve_credential_from_gh_cli",
                    return_value=None,
                ) as mock_gh,
                patch.object(
                    GitHubTokenManager,
                    "resolve_credential_from_git",
                    return_value="cred-token",
                ) as mock_cred,
            ):
                resolver = AuthResolver()
                ctx = resolver.resolve_for_dep(dep_ref)
                assert ctx.token == "cred-token"
                assert ctx.source == "git-credential-fill"
                mock_gh.assert_called_once_with("github.com")
                mock_cred.assert_called_once_with("github.com", port=None)

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

    def test_gitlab_prefers_gitlab_apm_pat_over_github_token(self):
        env = {
            "GITLAB_APM_PAT": "glpat_primary",
            "GITHUB_TOKEN": "gh_actions_token",
            "GITHUB_APM_PAT": "ghp_should_not_pick",
            "GH_TOKEN": "gh_cli_token",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                ctx = resolver.resolve("gitlab.com")
        assert ctx.token == "glpat_primary"
        assert ctx.source == "GITLAB_APM_PAT"
        assert ctx.host_info.kind == "gitlab"

    def test_gitlab_uses_gitlab_token_when_gitlab_apm_pat_absent(self):
        env = {"GITLAB_TOKEN": "glpat_from_gitlab_token", "GITHUB_TOKEN": "gh_should_ignore"}
        with patch.dict(os.environ, env, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                ctx = resolver.resolve("gitlab.com")
        assert ctx.token == "glpat_from_gitlab_token"
        assert ctx.source == "GITLAB_TOKEN"

    def test_gitlab_returns_none_when_only_github_env_vars(self):
        env = {
            "GITHUB_TOKEN": "gh_only",
            "GH_TOKEN": "gh_cli_only",
            "GITHUB_APM_PAT": "github_apm_pat_only",
            "GITHUB_APM_PAT_MYGROUP": "per_org_github",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                ctx = resolver.resolve("gitlab.com", org="mygroup")
        assert ctx.token is None
        assert ctx.source == "none"

    def test_gitlab_uses_github_per_org_var_is_not_selected(self):
        """Namespace segment must not activate GITHUB_APM_PAT_<ORG> on GitLab."""
        env = {
            "GITHUB_APM_PAT_ACME": "ghp_github_org_token",
            "GITLAB_TOKEN": "glpat_correct",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                ctx = resolver.resolve("gitlab.com", org="acme")
        assert ctx.token == "glpat_correct"
        assert ctx.source == "GITLAB_TOKEN"

    def test_gitlab_fallback_to_git_credential_when_no_gitlab_env(self):
        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "ignored", "GITHUB_APM_PAT": "ignored2"},
            clear=True,
        ):
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="from-helper"
            ):
                resolver = AuthResolver()
                ctx = resolver.resolve("gitlab.com")
        assert ctx.token == "from-helper"
        assert ctx.source == "git-credential-fill"

    def test_generic_host_does_not_use_github_or_gitlab_env_tokens(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "gh_bb",
                "GH_TOKEN": "gh_cli_bb",
                "GITHUB_APM_PAT": "apm_bb",
                "GITLAB_TOKEN": "glpat_bb",
                "GITLAB_APM_PAT": "glpat_apm_bb",
            },
            clear=True,
        ):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                ctx = resolver.resolve("bitbucket.org")
        assert ctx.token is None
        assert ctx.source == "none"
        assert ctx.host_info.kind == "generic"

    def test_generic_host_uses_credential_helper_when_configured(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ignored"}, clear=True):
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="bb-cred"
            ):
                resolver = AuthResolver()
                ctx = resolver.resolve("bitbucket.org")
        assert ctx.token == "bb-cred"
        assert ctx.source == "git-credential-fill"


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
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", return_value="cred-token"
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

    def test_gitlab_no_token_mentions_gitlab_env_not_github(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("gitlab.com", "clone")
        assert "GITLAB_APM_PAT" in msg
        assert "GITLAB_TOKEN" in msg
        assert "GITHUB_TOKEN" not in msg

    def test_gitlab_with_token_no_github_settings_link(self):
        with patch.dict(os.environ, {"GITLAB_TOKEN": "glpat"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("gitlab.com", "fetch")
        assert "GITLAB_TOKEN" in msg
        assert "github.com/settings/tokens" not in msg

    def test_generic_no_token_excludes_github_remediation(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("bitbucket.org", "clone")
        assert "GITHUB_APM_PAT" not in msg
        assert "GITHUB_TOKEN" not in msg

    def test_gitlab_org_does_not_suggest_github_per_org_var(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                resolver = AuthResolver()
                msg = resolver.build_error_context("gitlab.com", "clone", org="acme-group")
        assert "GITHUB_APM_PAT_" not in msg

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

    def test_ado_pat_set_az_available_case4_bearer_also_failed_prefix(self):
        """Case 4 + bearer_also_failed=True: dual-rejection prefix appears."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "expired-pat"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider = mock_provider_cls.return_value
                    mock_provider.is_available.return_value = True
                    resolver = AuthResolver()
                    msg = resolver.build_error_context(
                        "dev.azure.com",
                        "clone",
                        bearer_also_failed=True,
                    )
                    assert "ADO_APM_PAT was rejected" in msg
                    assert "az cli bearer was also rejected" in msg
                    assert "unset ADO_APM_PAT" in msg

    def test_ado_pat_set_az_available_case4_bearer_not_failed_no_prefix(self):
        """Case 4 default (bearer_also_failed=False): no dual-rejection prefix."""
        with patch.dict(os.environ, {"ADO_APM_PAT": "expired-pat"}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider = mock_provider_cls.return_value
                    mock_provider.is_available.return_value = True
                    resolver = AuthResolver()
                    msg = resolver.build_error_context("dev.azure.com", "clone")
                    assert "ADO_APM_PAT was rejected" not in msg
                    assert "az cli bearer was also rejected" not in msg

    def test_ado_no_pat_case2_ignores_bearer_also_failed_kwarg(self):
        """Case 2 (no PAT, bearer rejected) must NOT render PAT-rejected prefix
        even if bearer_also_failed=True is passed -- the prefix wording is
        contradictory when no PAT was tried. Defends against contradictory
        diagnostics if future callers misuse the kwarg."""
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
                with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_provider_cls:
                    mock_provider = mock_provider_cls.return_value
                    mock_provider.is_available.return_value = True
                    mock_provider.get_current_tenant_id.return_value = "tenant-abc"
                    resolver = AuthResolver()
                    msg = resolver.build_error_context(
                        "dev.azure.com",
                        "clone",
                        bearer_also_failed=True,
                    )
                    assert "ADO_APM_PAT was rejected" not in msg
                    assert "tenant" in msg.lower()


# ---------------------------------------------------------------------------
# TestStalePATDiagnosticDedup -- per-host dedup of warning emission
# ---------------------------------------------------------------------------


class TestStalePATDiagnosticDedup:
    def test_same_host_emits_once(self):
        """Two calls with same host -> warn called exactly once.

        The dedup uses _stale_pat_warned_hosts on the AuthResolver instance.
        Without dedup, users hit N warnings per dependency under the same
        host cluster; this regression-trap defends the per-host promise.
        """
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            with patch("apm_cli.utils.console._rich_warning") as mock_warn:
                resolver.emit_stale_pat_diagnostic("dev.azure.com")
                resolver.emit_stale_pat_diagnostic("dev.azure.com")
                # Each emit_stale_pat_diagnostic that fires calls _rich_warning
                # twice (msg + detail). One emission -> 2 calls; dedup'd second
                # call -> still 2 total.
                assert mock_warn.call_count == 2

    def test_different_hosts_each_emit_once(self):
        """Different hosts dedup independently."""
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            with patch("apm_cli.utils.console._rich_warning") as mock_warn:
                resolver.emit_stale_pat_diagnostic("dev.azure.com")
                resolver.emit_stale_pat_diagnostic("contoso.visualstudio.com")
                resolver.emit_stale_pat_diagnostic("dev.azure.com")
                # Two distinct hosts emit; each emission calls _rich_warning
                # twice (msg + detail). Third call (dup) -> no extra calls.
                assert mock_warn.call_count == 4

    def test_concurrent_same_host_emits_once(self):
        """Parallel install: N threads racing on the same ADO host -> ONE warning.

        #1214 follow-up: without locking the check-then-add of
        ``_stale_pat_warned_hosts``, two threads can both pass the
        ``host in set`` check before either calls ``add()``, defeating the
        per-host dedup the set is there to provide. The lock serialises
        check+add so only the first racer emits.
        """
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            with patch("apm_cli.utils.console._rich_warning") as mock_warn:
                with ThreadPoolExecutor(max_workers=16) as pool:
                    futures = [
                        pool.submit(resolver.emit_stale_pat_diagnostic, "dev.azure.com")
                        for _ in range(64)
                    ]
                    for fut in futures:
                        fut.result()
                # Single emission -> _rich_warning called twice (msg + detail).
                assert mock_warn.call_count == 2


# ---------------------------------------------------------------------------
# TestBuildGitEnvBearerIsolation -- _build_git_env(scheme="bearer") drops GIT_TOKEN
# ---------------------------------------------------------------------------


class TestBuildGitEnvBearerIsolation:
    def test_bearer_env_drops_pre_existing_git_token(self):
        """A stale GIT_TOKEN in the parent env must NOT survive into the bearer env.

        #1214 follow-up: ``_build_git_env`` starts from ``os.environ.copy()``;
        if a prior shell, CI step, or sibling tool already set GIT_TOKEN, the
        copy preserves it and silently defeats the bearer-isolation guarantee
        (the JWT is meant to flow ONLY via GIT_CONFIG_VALUE_0). Pop it
        explicitly so the bearer env is clean by construction.
        """
        with patch.dict(os.environ, {"GIT_TOKEN": "stale-pat-from-prior-shell"}, clear=False):
            env = AuthResolver._build_git_env(
                "fresh-jwt-from-az-cli", scheme="bearer", host_kind="ado"
            )
        assert "GIT_TOKEN" not in env, (
            "Stale GIT_TOKEN leaked into bearer env -- isolation guarantee broken"
        )
        # Sanity: bearer JWT IS present via GIT_CONFIG_* (the only legit channel).
        assert env.get("GIT_CONFIG_COUNT") is not None
        # Find the value slot that carries the JWT.
        value_slots = [v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_")]
        assert any("fresh-jwt-from-az-cli" in v for v in value_slots)

    def test_basic_scheme_still_sets_git_token(self):
        """Non-bearer path keeps the legacy GIT_TOKEN behaviour."""
        with patch.dict(os.environ, {}, clear=True):
            env = AuthResolver._build_git_env("a-pat", scheme="basic", host_kind="github")
        assert env.get("GIT_TOKEN") == "a-pat"


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

    def test_display_name_suppresses_default_port_443(self):
        """Defence-in-depth: display_name never renders well-known default ports."""
        hi = HostInfo(
            host="github.com",
            kind="github",
            has_public_repos=True,
            api_base="x",
            port=443,
        )
        assert hi.display_name == "github.com"

    def test_display_name_suppresses_default_port_22(self):
        hi = HostInfo(
            host="gitlab.com",
            kind="generic",
            has_public_repos=True,
            api_base="x",
            port=22,
        )
        assert hi.display_name == "gitlab.com"

    def test_display_name_suppresses_default_port_80(self):
        hi = HostInfo(
            host="internal.git",
            kind="generic",
            has_public_repos=True,
            api_base="x",
            port=80,
        )
        assert hi.display_name == "internal.git"


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

            def fake_cred(host, port=None, path=None):
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


class TestTryWithFallbackPathDisambiguation:
    """try_with_fallback must thread `path` to credential fill (per-URL GCM)."""

    def test_path_threaded_to_credential_fallback(self):
        """When env token fails, path is forwarded to resolve_credential_from_git."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "bad"}, clear=True):
            seen_kwargs: list = []

            def fake_cred(host, port=None, path=None):
                seen_kwargs.append({"host": host, "port": port, "path": path})
                return "good"

            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", side_effect=fake_cred
            ):
                resolver = AuthResolver()

                def op(token, env):
                    if token != "good":
                        raise RuntimeError("rejected")
                    return "ok"

                result = resolver.try_with_fallback("github.com", op, path="acme/widgets")
        assert result == "ok"
        assert seen_kwargs == [{"host": "github.com", "port": None, "path": "acme/widgets"}]

    def test_path_default_none_preserves_legacy_call(self):
        """Callers that omit path still invoke credential fill with path=None."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "bad"}, clear=True):
            seen_kwargs: list = []

            def fake_cred(host, port=None, path=None):
                seen_kwargs.append({"host": host, "port": port, "path": path})
                return "good"

            with patch.object(
                GitHubTokenManager, "resolve_credential_from_git", side_effect=fake_cred
            ):
                resolver = AuthResolver()

                def op(token, env):
                    if token != "good":
                        raise RuntimeError("rejected")
                    return "ok"

                resolver.try_with_fallback("github.com", op)
        assert seen_kwargs == [{"host": "github.com", "port": None, "path": None}]


class TestGhCliShortCircuitsCredentialFill:
    """Regression trap: when gh CLI returns a token, credential fill must NOT run.

    PR #630 added gh-CLI as the second resolver in the fallback chain. Without
    this trap, a refactor that re-orders the chain (or accidentally calls
    resolve_credential_from_git unconditionally) would silently re-introduce
    the GCM account-picker prompt for users who configured gh.
    """

    def test_gh_cli_success_skips_credential_fill(self):
        """resolve_credential_from_git must not be invoked when gh CLI returns a token."""
        with patch.dict(os.environ, {"GITHUB_APM_PAT": "bad"}, clear=True):
            with patch.object(
                GitHubTokenManager,
                "resolve_credential_from_gh_cli",
                return_value="gho_from_gh_cli",
            ):
                with patch.object(
                    GitHubTokenManager, "resolve_credential_from_git"
                ) as mock_cred_fill:
                    resolver = AuthResolver()

                    def op(token, env):
                        if token != "gho_from_gh_cli":
                            raise RuntimeError("rejected")
                        return f"ok:{token}"

                    result = resolver.try_with_fallback("github.com", op, path="acme/widgets")

            assert result == "ok:gho_from_gh_cli"
            mock_cred_fill.assert_not_called()
