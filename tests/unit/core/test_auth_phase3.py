"""Phase-3 unit tests for apm_cli.core.auth — targeting uncovered branches.

Focuses on:
- HostInfo.display_name with various ports
- AuthContext construction
- detect_token_type / gitlab_rest_headers
- resolve() caching and port discrimination
- resolve_for_dep()
- try_with_fallback() all branches (unauth_first, no-token, ghe_cloud, ado bearer, credential fallback)
- build_error_context() non-ADO paths (gitlab, generic, ghe_cloud, github, ghes, port hint)
- _resolve_token() all host-class branches
- _purpose_for_host / _identify_env_source
- _build_git_env no-token path
- emit_stale_pat_diagnostic with logger wired
- _diagnostics_or_none
- notify_auth_source various paths
- execute_with_bearer_fallback all branches
- BearerFallbackOutcome
- _org_to_env_suffix
- set_logger
"""

from __future__ import annotations

import os
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core import azure_cli as _azure_cli_mod
from apm_cli.core.auth import (
    AuthContext,
    AuthResolver,
    BearerFallbackOutcome,
    HostInfo,
    _org_to_env_suffix,
)
from apm_cli.core.token_manager import GitHubTokenManager

# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_bearer_singleton():
    _azure_cli_mod._provider_singleton = None
    yield
    _azure_cli_mod._provider_singleton = None


@pytest.fixture(autouse=True)
def _disable_gh_cli():
    with patch.object(GitHubTokenManager, "resolve_credential_from_gh_cli", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _disable_git_credential():
    with patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None):
        yield


# ---------------------------------------------------------------------------
# HostInfo.display_name
# ---------------------------------------------------------------------------


class TestHostInfoDisplayName:
    def test_no_port_returns_bare_host(self) -> None:
        hi = HostInfo(host="example.com", kind="generic", has_public_repos=True, api_base="x")
        assert hi.display_name == "example.com"

    def test_non_standard_port_appends_port(self) -> None:
        hi = HostInfo(
            host="bb.corp.com", kind="generic", has_public_repos=True, api_base="x", port=7990
        )
        assert hi.display_name == "bb.corp.com:7990"

    def test_well_known_port_443_suppressed(self) -> None:
        hi = HostInfo(
            host="github.com", kind="github", has_public_repos=True, api_base="x", port=443
        )
        assert hi.display_name == "github.com"

    def test_well_known_port_80_suppressed(self) -> None:
        hi = HostInfo(
            host="github.com", kind="github", has_public_repos=True, api_base="x", port=80
        )
        assert hi.display_name == "github.com"

    def test_well_known_port_22_suppressed(self) -> None:
        hi = HostInfo(
            host="github.com", kind="github", has_public_repos=True, api_base="x", port=22
        )
        assert hi.display_name == "github.com"

    def test_port_7999_included(self) -> None:
        hi = HostInfo(
            host="git.corp.com", kind="generic", has_public_repos=True, api_base="x", port=7999
        )
        assert hi.display_name == "git.corp.com:7999"


# ---------------------------------------------------------------------------
# AuthContext construction
# ---------------------------------------------------------------------------


class TestAuthContextConstruction:
    def test_fields_accessible(self) -> None:
        hi = HostInfo(
            host="github.com",
            kind="github",
            has_public_repos=True,
            api_base="https://api.github.com",
        )
        ctx = AuthContext(
            token="ghp_test",
            source="GITHUB_TOKEN",
            token_type="classic",
            host_info=hi,
            git_env={"GIT_TOKEN": "ghp_test"},
        )
        assert ctx.token == "ghp_test"
        assert ctx.source == "GITHUB_TOKEN"
        assert ctx.token_type == "classic"
        assert ctx.host_info is hi
        assert ctx.auth_scheme == "basic"

    def test_repr_hides_token(self) -> None:
        hi = HostInfo(host="github.com", kind="github", has_public_repos=True, api_base="x")
        ctx = AuthContext(
            token="super_secret_token", source="env", token_type="classic", host_info=hi, git_env={}
        )
        assert "super_secret_token" not in repr(ctx)


# ---------------------------------------------------------------------------
# detect_token_type
# ---------------------------------------------------------------------------


class TestDetectTokenType:
    @pytest.mark.parametrize(
        "token,expected",
        [
            ("github_pat_abc", "fine-grained"),
            ("ghp_classicpat", "classic"),
            ("ghu_oauthuser", "oauth"),
            ("gho_oauthapp", "oauth"),
            ("ghs_appinstall", "github-app"),
            ("ghr_refreshtoken", "github-app"),
            ("some_random_token", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_token_prefixes(self, token: str, expected: str) -> None:
        assert AuthResolver.detect_token_type(token) == expected


# ---------------------------------------------------------------------------
# gitlab_rest_headers
# ---------------------------------------------------------------------------


class TestGitlabRestHeaders:
    def test_returns_empty_dict_when_no_token(self) -> None:
        assert AuthResolver.gitlab_rest_headers(None) == {}
        assert AuthResolver.gitlab_rest_headers("") == {}

    def test_returns_private_token_header_by_default(self) -> None:
        headers = AuthResolver.gitlab_rest_headers("mytoken")
        assert headers == {"PRIVATE-TOKEN": "mytoken"}

    def test_returns_bearer_header_when_oauth_bearer(self) -> None:
        headers = AuthResolver.gitlab_rest_headers("mytoken", oauth_bearer=True)
        assert headers == {"Authorization": "Bearer mytoken"}


# ---------------------------------------------------------------------------
# _org_to_env_suffix
# ---------------------------------------------------------------------------


class TestOrgToEnvSuffix:
    def test_simple_org(self) -> None:
        assert _org_to_env_suffix("myorg") == "MYORG"

    def test_hyphen_becomes_underscore(self) -> None:
        assert _org_to_env_suffix("my-org") == "MY_ORG"

    def test_mixed_case(self) -> None:
        assert _org_to_env_suffix("AcmeCorp") == "ACMECORP"

    def test_multiple_hyphens(self) -> None:
        assert _org_to_env_suffix("a-b-c") == "A_B_C"


# ---------------------------------------------------------------------------
# set_logger
# ---------------------------------------------------------------------------


class TestSetLogger:
    def test_set_logger_idempotent(self) -> None:
        resolver = AuthResolver()
        logger = MagicMock()
        resolver.set_logger(logger)
        assert resolver._logger is logger
        resolver.set_logger(logger)
        assert resolver._logger is logger

    def test_set_logger_overwrites(self) -> None:
        resolver = AuthResolver()
        l1 = MagicMock()
        l2 = MagicMock()
        resolver.set_logger(l1)
        resolver.set_logger(l2)
        assert resolver._logger is l2


# ---------------------------------------------------------------------------
# resolve() — caching and port discrimination
# ---------------------------------------------------------------------------


class TestResolveCache:
    def test_cache_hit_returns_same_object(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            ctx1 = resolver.resolve("github.com")
            ctx2 = resolver.resolve("github.com")
            assert ctx1 is ctx2

    def test_same_host_different_ports_are_different_keys(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            ctx_a = resolver.resolve("git.corp.com", port=7990)
            ctx_b = resolver.resolve("git.corp.com", port=7999)
            assert ctx_a is not ctx_b

    def test_same_host_different_orgs_are_different_keys(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            ctx_a = resolver.resolve("github.com", org="acme")
            ctx_b = resolver.resolve("github.com", org="betacorp")
            assert ctx_a is not ctx_b

    def test_case_insensitive_host(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            ctx_a = resolver.resolve("GitHub.com")
            ctx_b = resolver.resolve("github.com")
            assert ctx_a is ctx_b


# ---------------------------------------------------------------------------
# resolve_for_dep()
# ---------------------------------------------------------------------------


class TestResolveForDep:
    def _make_dep_ref(self, host: str, repo_url: str | None = None, port: int | None = None):
        dep = MagicMock()
        dep.host = host
        dep.repo_url = repo_url
        dep.port = port
        dep.host_type = None
        return dep

    def test_resolve_for_dep_uses_host(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            dep = self._make_dep_ref("github.com", "myorg/myrepo")
            ctx = resolver.resolve_for_dep(dep)
            assert ctx.host_info.kind == "github"

    def test_resolve_for_dep_extracts_org_from_repo_url(self) -> None:
        with patch.dict(os.environ, {"GITHUB_APM_PAT_MYORG": "ghp_perorg"}, clear=True):
            resolver = AuthResolver()
            dep = self._make_dep_ref("github.com", "myorg/myrepo")
            ctx = resolver.resolve_for_dep(dep)
            assert ctx.token == "ghp_perorg"
            assert ctx.source == "GITHUB_APM_PAT_MYORG"

    def test_resolve_for_dep_no_repo_url_no_org(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            dep = self._make_dep_ref("github.com", None)
            ctx = resolver.resolve_for_dep(dep)
            assert ctx.host_info.kind == "github"
            assert ctx.token is None

    def test_resolve_for_dep_threads_port(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            dep = self._make_dep_ref("git.corp.com", None, port=7990)
            ctx = resolver.resolve_for_dep(dep)
            assert ctx.host_info.port == 7990


# ---------------------------------------------------------------------------
# _purpose_for_host / _identify_env_source
# ---------------------------------------------------------------------------


class TestPurposeForHost:
    def test_ado_host(self) -> None:
        hi = HostInfo(host="dev.azure.com", kind="ado", has_public_repos=True, api_base="x")
        assert AuthResolver._purpose_for_host(hi) == "ado_modules"

    def test_gitlab_host(self) -> None:
        hi = HostInfo(host="gitlab.com", kind="gitlab", has_public_repos=True, api_base="x")
        assert AuthResolver._purpose_for_host(hi) == "gitlab_modules"

    def test_generic_host(self) -> None:
        hi = HostInfo(host="bb.corp.com", kind="generic", has_public_repos=True, api_base="x")
        assert AuthResolver._purpose_for_host(hi) == "generic_modules"

    def test_github_host(self) -> None:
        hi = HostInfo(host="github.com", kind="github", has_public_repos=True, api_base="x")
        assert AuthResolver._purpose_for_host(hi) == "modules"


class TestIdentifyEnvSource:
    def test_returns_var_name_when_set(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=True):
            resolver = AuthResolver()
            source = resolver._identify_env_source("modules")
            assert source == "GITHUB_TOKEN"

    def test_returns_env_when_none_set(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            source = resolver._identify_env_source("modules")
            assert source == "env"


# ---------------------------------------------------------------------------
# _build_git_env — no-token path
# ---------------------------------------------------------------------------


class TestBuildGitEnvNoToken:
    def test_no_token_still_sets_terminal_prompt_off(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            env = AuthResolver._build_git_env(None, scheme="basic", host_kind="github")
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_ASKPASS"] == "echo"
        assert "GIT_TOKEN" not in env

    def test_non_ado_bearer_with_token_falls_through_to_basic(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            env = AuthResolver._build_git_env("mytoken", scheme="bearer", host_kind="github")
        assert env.get("GIT_TOKEN") == "mytoken"


# ---------------------------------------------------------------------------
# _diagnostics_or_none
# ---------------------------------------------------------------------------


class TestDiagnosticsOrNone:
    def test_no_logger_returns_none(self) -> None:
        resolver = AuthResolver()
        assert resolver._diagnostics_or_none() is None

    def test_logger_without_diagnostics_attr_returns_none(self) -> None:
        resolver = AuthResolver()
        logger = MagicMock(spec=[])  # no 'diagnostics' attribute
        resolver.set_logger(logger)
        assert resolver._diagnostics_or_none() is None

    def test_logger_with_diagnostics_returns_it(self) -> None:
        resolver = AuthResolver()
        diag = MagicMock()
        logger = MagicMock()
        logger.diagnostics = diag
        resolver.set_logger(logger)
        assert resolver._diagnostics_or_none() is diag


# ---------------------------------------------------------------------------
# emit_stale_pat_diagnostic — with logger wired
# ---------------------------------------------------------------------------


class TestEmitStalePATDiagnosticWithLogger:
    def test_emits_via_diagnostics_when_logger_wired(self) -> None:
        resolver = AuthResolver()
        diag = MagicMock()
        logger = MagicMock()
        logger.diagnostics = diag
        resolver.set_logger(logger)

        resolver.emit_stale_pat_diagnostic("dev.azure.com")

        diag.warn.assert_called_once()
        args, _ = diag.warn.call_args
        assert "ADO_APM_PAT" in args[0]

    def test_emits_only_once_per_host_with_logger(self) -> None:
        resolver = AuthResolver()
        diag = MagicMock()
        logger = MagicMock()
        logger.diagnostics = diag
        resolver.set_logger(logger)

        resolver.emit_stale_pat_diagnostic("dev.azure.com")
        resolver.emit_stale_pat_diagnostic("dev.azure.com")

        diag.warn.assert_called_once()

    def test_private_alias_works(self) -> None:
        resolver = AuthResolver()
        diag = MagicMock()
        logger = MagicMock()
        logger.diagnostics = diag
        resolver.set_logger(logger)

        resolver._emit_stale_pat_diagnostic("contoso.visualstudio.com")

        diag.warn.assert_called_once()


# ---------------------------------------------------------------------------
# notify_auth_source
# ---------------------------------------------------------------------------


class TestNotifyAuthSource:
    def test_empty_host_is_noop(self) -> None:
        resolver = AuthResolver()
        resolver.notify_auth_source("", None)
        # No exception; host_key was empty so early return

    def test_source_none_is_noop(self) -> None:
        resolver = AuthResolver()
        ctx = MagicMock()
        ctx.source = "none"
        resolver.notify_auth_source("github.com", ctx)
        # Does not write to stderr or logger

    def test_dedup_same_host(self) -> None:
        resolver = AuthResolver()
        ctx = MagicMock()
        ctx.source = "GITHUB_TOKEN"
        ctx.auth_scheme = "basic"

        import sys
        from io import StringIO

        captured = StringIO()
        with patch.object(sys, "stderr", captured):
            resolver.notify_auth_source("github.com", ctx)
            resolver.notify_auth_source("github.com", ctx)

        output = captured.getvalue()
        # Should only appear once even if called twice
        assert output.count("github.com") == 1

    def test_bearer_scheme_emits_bearer_message(self) -> None:
        resolver = AuthResolver()
        ctx = MagicMock()
        ctx.source = "az-bearer"
        ctx.auth_scheme = "bearer"

        import sys
        from io import StringIO

        captured = StringIO()
        with patch.object(sys, "stderr", captured):
            resolver.notify_auth_source("dev.azure.com", ctx)

        assert "bearer" in captured.getvalue().lower()

    def test_with_verbose_logger_routes_via_rich_echo(self) -> None:
        resolver = AuthResolver()
        logger = MagicMock()
        logger.verbose = True
        resolver.set_logger(logger)

        ctx = MagicMock()
        ctx.source = "GITHUB_TOKEN"
        ctx.auth_scheme = "basic"

        with patch("apm_cli.utils.console._rich_echo") as mock_echo:
            resolver.notify_auth_source("github.com", ctx)

        mock_echo.assert_called_once()

    def test_ctx_none_is_noop(self) -> None:
        resolver = AuthResolver()
        # ctx=None → source check does getattr(None, "source", "none") == "none" → return
        resolver.notify_auth_source("github.com", None)
        # No exception expected


# ---------------------------------------------------------------------------
# try_with_fallback — unauth_first path
# ---------------------------------------------------------------------------


class TestTryWithFallbackUnauthFirst:
    def test_unauth_first_succeeds_on_first_try(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            op = MagicMock(return_value="unauth_result")
            result = resolver.try_with_fallback("github.com", op, unauth_first=True)
            assert result == "unauth_result"
            # called once with (None, env)
            op.assert_called_once()
            token_arg = op.call_args[0][0]
            assert token_arg is None

    def test_unauth_first_fallback_to_token_when_unauth_fails(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_tok"}, clear=True):
            resolver = AuthResolver()
            call_count = [0]

            def _op(token, env):
                call_count[0] += 1
                if token is None:
                    raise RuntimeError("unauth_fail")
                return "auth_result"

            result = resolver.try_with_fallback("github.com", _op, unauth_first=True)
            assert result == "auth_result"
            assert call_count[0] == 2

    def test_unauth_first_no_token_re_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()

            def _op(token, env):
                raise RuntimeError("always fail")

            with pytest.raises(RuntimeError, match="always fail"):
                resolver.try_with_fallback("github.com", _op, unauth_first=True)

    def test_no_token_calls_operation_unauthenticated(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            op = MagicMock(return_value="anon_result")
            result = resolver.try_with_fallback("github.com", op)
            assert result == "anon_result"


# ---------------------------------------------------------------------------
# try_with_fallback — auth_first with public repo fallback
# ---------------------------------------------------------------------------


class TestTryWithFallbackAuthFirst:
    def test_auth_fails_then_unauthenticated_succeeds(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_tok"}, clear=True):
            resolver = AuthResolver()
            call_count = [0]

            def _op(token, env):
                call_count[0] += 1
                if token:
                    raise RuntimeError("auth_fail")
                return "unauth_success"

            result = resolver.try_with_fallback("github.com", _op)
            assert result == "unauth_success"
            assert call_count[0] == 2

    def test_verbose_callback_is_invoked(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_tok"}, clear=True):
            resolver = AuthResolver()
            messages: list[str] = []
            result = resolver.try_with_fallback(
                "github.com",
                lambda token, env: "ok",
                verbose_callback=messages.append,
            )
            assert result == "ok"
            assert len(messages) >= 1  # at least one log message


# ---------------------------------------------------------------------------
# try_with_fallback — ghe_cloud (auth-only)
# ---------------------------------------------------------------------------


class TestTryWithFallbackGheCloud:
    def test_ghe_cloud_auth_only_success(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_ghe"}, clear=True):
            resolver = AuthResolver()
            op = MagicMock(return_value="ghe_result")
            result = resolver.try_with_fallback("contoso.ghe.com", op)
            assert result == "ghe_result"


# ---------------------------------------------------------------------------
# try_with_fallback — credential fallback (non-ADO, non-secondary source)
# ---------------------------------------------------------------------------


class TestTryWithFallbackCredentialChain:
    def test_credential_fill_fallback_skipped_for_secondary_source(self) -> None:
        """If the source is already 'gh-auth-token', credential fallback should re-raise."""
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            # Force gh-auth-token as source by pre-resolving via gh cli
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_gh_cli", return_value="ghp_gh_cli"
            ):
                resolver.resolve("github.com")

            resolver._cache.clear()

            # Now simulate the operation failing; the fallback should NOT retry because
            # auth source was gh-auth-token
            with patch.object(
                GitHubTokenManager, "resolve_credential_from_gh_cli", return_value="ghp_gh_cli"
            ):

                def _op(token, env):
                    raise RuntimeError("fail")

                with pytest.raises(RuntimeError, match="fail"):
                    resolver.try_with_fallback("github.com", _op)


# ---------------------------------------------------------------------------
# build_error_context — non-ADO paths
# ---------------------------------------------------------------------------


class TestBuildErrorContextNonADO:
    def test_github_com_with_token_mentions_saml(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_tok"}, clear=True):
            resolver = AuthResolver()
            msg = resolver.build_error_context("github.com", "clone", org="myorg")
            # Token is set, org passed -> should get per-org hint
            assert "GITHUB_APM_PAT_MYORG" in msg

    def test_github_com_without_token_suggests_github_token(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            msg = resolver.build_error_context("github.com", "clone")
            assert "GITHUB_APM_PAT" in msg or "GITHUB_TOKEN" in msg

    def test_gitlab_com_with_token_mentions_gitlab_guidance(self) -> None:
        with patch.dict(os.environ, {"GITLAB_TOKEN": "glpat_tok"}, clear=True):
            resolver = AuthResolver()
            msg = resolver.build_error_context("gitlab.com", "clone")
            assert "gitlab" in msg.lower()

    def test_gitlab_com_without_token_suggests_gitlab_pat(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            msg = resolver.build_error_context("gitlab.com", "clone")
            assert "GITLAB_APM_PAT" in msg or "GITLAB_TOKEN" in msg

    def test_generic_host_with_token_mentions_credential_helper(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            # Force generic auth context by patching resolve
            with patch.object(
                AuthResolver,
                "resolve",
                return_value=AuthContext(
                    token="sometoken",
                    source="git-credential-fill",
                    token_type="unknown",
                    host_info=HostInfo(
                        host="bb.corp.com",
                        kind="generic",
                        has_public_repos=True,
                        api_base="x",
                    ),
                    git_env={},
                ),
            ):
                msg = resolver.build_error_context("bb.corp.com", "clone")
            assert "credential" in msg.lower() or "helper" in msg.lower()

    def test_generic_host_without_token_mentions_git_credential_fill(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            msg = resolver.build_error_context("bb.corp.com", "clone")
            assert (
                "credential" in msg.lower()
                or "git credential" in msg.lower()
                or "GITHUB" not in msg
            )

    def test_ghe_cloud_with_token_mentions_enterprise_scoped(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_tok"}, clear=True):
            resolver = AuthResolver()
            msg = resolver.build_error_context("contoso.ghe.com", "clone")
            assert "enterprise" in msg.lower() or "ghe" in msg.lower() or "token" in msg.lower()

    def test_host_with_port_appends_port_hint(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            msg = resolver.build_error_context("git.corp.com", "clone", port=7990)
            assert "7990" in msg or "port" in msg.lower()

    def test_always_mentions_verbose_flag(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            msg = resolver.build_error_context("github.com", "clone")
            assert "--verbose" in msg


# ---------------------------------------------------------------------------
# execute_with_bearer_fallback
# ---------------------------------------------------------------------------


class TestExecuteWithBearerFallback:
    def _make_ado_dep_ref(self) -> MagicMock:
        dep = MagicMock()
        dep.is_azure_devops = lambda: True
        dep.host = "dev.azure.com"
        return dep

    def _make_non_ado_dep_ref(self) -> MagicMock:
        dep = MagicMock()
        dep.is_azure_devops = lambda: False
        dep.host = "github.com"
        return dep

    def test_non_ado_returns_primary_immediately(self) -> None:
        resolver = AuthResolver()
        dep = self._make_non_ado_dep_ref()
        outcome = resolver.execute_with_bearer_fallback(
            dep,
            primary_op=lambda: "primary_result",
            bearer_op=lambda b: "bearer_result",
            is_auth_failure=lambda r: False,
        )
        assert outcome == BearerFallbackOutcome("primary_result", False)

    def test_dep_ref_none_returns_primary_immediately(self) -> None:
        resolver = AuthResolver()
        outcome = resolver.execute_with_bearer_fallback(
            None,
            primary_op=lambda: "primary",
            bearer_op=lambda b: "bearer",
            is_auth_failure=lambda r: False,
        )
        assert outcome.outcome == "primary"
        assert outcome.bearer_attempted is False

    def test_ado_no_auth_failure_returns_primary(self) -> None:
        resolver = AuthResolver()
        dep = self._make_ado_dep_ref()
        outcome = resolver.execute_with_bearer_fallback(
            dep,
            primary_op=lambda: "primary_ok",
            bearer_op=lambda b: "bearer_result",
            is_auth_failure=lambda r: False,
        )
        assert outcome.outcome == "primary_ok"
        assert outcome.bearer_attempted is False

    def test_ado_auth_failure_provider_unavailable_returns_primary(self) -> None:
        resolver = AuthResolver()
        dep = self._make_ado_dep_ref()
        with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_cls:
            mock_cls.return_value.is_available.return_value = False
            outcome = resolver.execute_with_bearer_fallback(
                dep,
                primary_op=lambda: "primary_fail",
                bearer_op=lambda b: "bearer_result",
                is_auth_failure=lambda r: True,
            )
        assert outcome.outcome == "primary_fail"
        assert outcome.bearer_attempted is False

    def test_ado_auth_failure_bearer_error_returns_primary(self) -> None:
        from apm_cli.core.azure_cli import AzureCliBearerError

        resolver = AuthResolver()
        dep = self._make_ado_dep_ref()
        with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_cls:
            mock_cls.return_value.is_available.return_value = True
            mock_cls.return_value.get_bearer_token.side_effect = AzureCliBearerError(
                "fail", kind="error"
            )
            outcome = resolver.execute_with_bearer_fallback(
                dep,
                primary_op=lambda: "primary_fail",
                bearer_op=lambda b: "bearer_result",
                is_auth_failure=lambda r: True,
            )
        assert outcome.outcome == "primary_fail"
        assert outcome.bearer_attempted is False

    def test_ado_bearer_succeeds_emits_diagnostic_returns_fallback(self) -> None:
        resolver = AuthResolver()
        dep = self._make_ado_dep_ref()

        with (
            patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_cls,
            patch.object(resolver, "emit_stale_pat_diagnostic") as mock_diag,
        ):
            mock_cls.return_value.is_available.return_value = True
            mock_cls.return_value.get_bearer_token.return_value = "ey.bearer.jwt"
            outcome = resolver.execute_with_bearer_fallback(
                dep,
                primary_op=lambda: "primary_fail",
                bearer_op=lambda b: "bearer_success",
                is_auth_failure=lambda r: r == "primary_fail",
            )

        assert outcome.outcome == "bearer_success"
        assert outcome.bearer_attempted is True
        mock_diag.assert_called_once_with("dev.azure.com")

    def test_ado_bearer_fails_returns_primary_with_attempted_true(self) -> None:
        resolver = AuthResolver()
        dep = self._make_ado_dep_ref()

        with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_cls:
            mock_cls.return_value.is_available.return_value = True
            mock_cls.return_value.get_bearer_token.return_value = "ey.bearer.jwt"
            outcome = resolver.execute_with_bearer_fallback(
                dep,
                primary_op=lambda: "primary_fail",
                bearer_op=lambda b: "bearer_also_fail",
                is_auth_failure=lambda r: r in ("primary_fail", "bearer_also_fail"),
            )

        assert outcome.outcome == "primary_fail"
        assert outcome.bearer_attempted is True

    def test_ado_bearer_op_raises_swallowed_returns_primary(self) -> None:
        resolver = AuthResolver()
        dep = self._make_ado_dep_ref()

        with patch("apm_cli.core.azure_cli.AzureCliBearerProvider") as mock_cls:
            mock_cls.return_value.is_available.return_value = True
            mock_cls.return_value.get_bearer_token.return_value = "ey.jwt"

            def _bearer_op(b):
                raise RuntimeError("bearer op exploded")

            outcome = resolver.execute_with_bearer_fallback(
                dep,
                primary_op=lambda: "primary_fail",
                bearer_op=_bearer_op,
                is_auth_failure=lambda r: True,
            )

        assert outcome.outcome == "primary_fail"
        assert outcome.bearer_attempted is True


# ---------------------------------------------------------------------------
# BearerFallbackOutcome
# ---------------------------------------------------------------------------


class TestBearerFallbackOutcome:
    def test_is_namedtuple(self) -> None:
        o = BearerFallbackOutcome(outcome="x", bearer_attempted=True)
        assert o.outcome == "x"
        assert o.bearer_attempted is True

    def test_unpacking(self) -> None:
        outcome, attempted = BearerFallbackOutcome("y", False)
        assert outcome == "y"
        assert attempted is False


# ---------------------------------------------------------------------------
# classify_host — ghes requires valid FQDN
# ---------------------------------------------------------------------------


class TestClassifyHostGhes:
    def test_invalid_fqdn_falls_through_to_generic(self) -> None:
        with patch.dict(os.environ, {"GITHUB_HOST": "notafqdn"}, clear=True):
            # "notafqdn" is not a valid FQDN (no dot) so is_valid_fqdn returns False
            hi = AuthResolver.classify_host("notafqdn")
            assert hi.kind == "generic"

    def test_ghes_host_env_must_match_exactly(self) -> None:
        with patch.dict(os.environ, {"GITHUB_HOST": "github.mycompany.com"}, clear=True):
            hi_match = AuthResolver.classify_host("github.mycompany.com")
            hi_other = AuthResolver.classify_host("other.mycompany.com")
            assert hi_match.kind == "ghes"
            assert hi_other.kind == "generic"

    def test_ghes_host_github_com_excluded(self) -> None:
        with patch.dict(os.environ, {"GITHUB_HOST": "github.com"}, clear=True):
            hi = AuthResolver.classify_host("github.com")
            # github.com is excluded from GHES classification
            assert hi.kind == "github"


# ---------------------------------------------------------------------------
# Thread safety — resolve() under contention
# ---------------------------------------------------------------------------


class TestResolveThreadSafety:
    def test_concurrent_resolves_produce_same_context(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            resolver = AuthResolver()
            results: list[AuthContext] = []
            errors: list[Exception] = []

            def _resolve():
                try:
                    results.append(resolver.resolve("github.com"))
                except Exception as exc:
                    errors.append(exc)

            threads = [Thread(target=_resolve) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors
            assert len(results) == 20
            # All should be the same cached object
            assert all(r is results[0] for r in results)
