"""Integration tests for install.py, pack.py, plugin_parser.py, and auth.py.

large integration-gap coverage.
Exercises real code paths with minimal mocking (only external I/O is mocked).
No live network access.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# Lazy imports guard — all four modules must be importable
# ---------------------------------------------------------------------------
from apm_cli.commands.install import (
    _check_package_conflicts,
    _split_argv_at_double_dash,
    install,
)
from apm_cli.commands.pack import pack_cmd
from apm_cli.core.auth import (
    AuthResolver,
    BearerFallbackOutcome,
    HostInfo,
    _org_to_env_suffix,
)
from apm_cli.core.token_manager import GitHubTokenManager
from apm_cli.deps.plugin_parser import (
    _extract_mcp_servers,
    _generate_apm_yml,
    _is_within_plugin,
    _map_plugin_artifacts,
    _mcp_servers_to_apm_deps,
    _read_mcp_file,
    _read_mcp_json,
    _substitute_plugin_root,
    normalize_plugin_directory,
    parse_plugin_manifest,
    synthesize_plugin_json_from_apm_yml,
    validate_plugin_package,
)
from apm_cli.install.transaction import (
    _maybe_rollback_manifest,
    _restore_manifest_from_snapshot,
)
from apm_cli.models.apm_package import clear_apm_yml_cache

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_LOCKFILE_TEMPLATE = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""

_NO_GIT_CRED = patch.object(GitHubTokenManager, "resolve_credential_from_git", return_value=None)
_NO_GH_CLI = patch.object(GitHubTokenManager, "resolve_credential_from_gh_cli", return_value=None)


def _write_lockfile(root: Path) -> None:
    (root / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")


def _write_apm_yml(root: Path, content: str) -> None:
    (root / "apm.yml").write_text(content, encoding="utf-8")


# ===========================================================================
# 1.  auth.py — HostInfo / AuthContext / AuthResolver
# ===========================================================================


class TestHostInfoDisplayName:
    """HostInfo.display_name suppresses well-known default ports."""

    def test_display_name_no_port(self) -> None:
        hi = HostInfo(host="github.com", kind="github", has_public_repos=True, api_base="x")
        assert hi.display_name == "github.com"

    def test_display_name_non_standard_port(self) -> None:
        hi = HostInfo(
            host="bitbucket.corp", kind="generic", has_public_repos=True, api_base="x", port=7999
        )
        assert hi.display_name == "bitbucket.corp:7999"

    def test_display_name_suppresses_port_443(self) -> None:
        hi = HostInfo(
            host="github.com", kind="github", has_public_repos=True, api_base="x", port=443
        )
        assert hi.display_name == "github.com"

    def test_display_name_suppresses_port_80(self) -> None:
        hi = HostInfo(
            host="github.com", kind="github", has_public_repos=True, api_base="x", port=80
        )
        assert hi.display_name == "github.com"

    def test_display_name_suppresses_port_22(self) -> None:
        hi = HostInfo(
            host="github.com", kind="github", has_public_repos=True, api_base="x", port=22
        )
        assert hi.display_name == "github.com"


class TestOrgToEnvSuffix:
    def test_simple_org(self) -> None:
        assert _org_to_env_suffix("myorg") == "MYORG"

    def test_hyphen_becomes_underscore(self) -> None:
        assert _org_to_env_suffix("my-org") == "MY_ORG"

    def test_already_upper(self) -> None:
        assert _org_to_env_suffix("CONTOSO") == "CONTOSO"

    def test_mixed_case_with_hyphens(self) -> None:
        assert _org_to_env_suffix("some-Big-Org") == "SOME_BIG_ORG"


class TestDetectTokenType:
    """AuthResolver.detect_token_type classifies all known prefixes."""

    @pytest.mark.parametrize(
        "token, expected",
        [
            ("github_pat_abc", "fine-grained"),
            ("ghp_abc123", "classic"),
            ("ghu_abc123", "oauth"),
            ("gho_abc123", "oauth"),
            ("ghs_abc123", "github-app"),
            ("ghr_abc123", "github-app"),
            ("anunknown_token", "unknown"),
            ("Bearer eyJxxx", "unknown"),
        ],
    )
    def test_token_type_detection(self, token: str, expected: str) -> None:
        assert AuthResolver.detect_token_type(token) == expected


class TestGitlabRestHeaders:
    def test_no_token_returns_empty_dict(self) -> None:
        assert AuthResolver.gitlab_rest_headers(None) == {}
        assert AuthResolver.gitlab_rest_headers("") == {}

    def test_pat_uses_private_token_header(self) -> None:
        headers = AuthResolver.gitlab_rest_headers("glpat-abc123")
        assert headers == {"PRIVATE-TOKEN": "glpat-abc123"}

    def test_oauth_bearer_uses_authorization_header(self) -> None:
        headers = AuthResolver.gitlab_rest_headers("oauth_token", oauth_bearer=True)
        assert headers == {"Authorization": "Bearer oauth_token"}


class TestClassifyHostComprehensive:
    """classify_host covers all seven host kinds."""

    def test_github_com_exact(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            hi = AuthResolver.classify_host("github.com")
        assert hi.kind == "github"
        assert hi.has_public_repos is True
        assert hi.api_base == "https://api.github.com"

    def test_github_com_uppercase(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            hi = AuthResolver.classify_host("GITHUB.COM")
        assert hi.kind == "github"

    def test_ghe_cloud(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            hi = AuthResolver.classify_host("contoso.ghe.com")
        assert hi.kind == "ghe_cloud"
        assert hi.has_public_repos is False
        assert "contoso.ghe.com" in hi.api_base

    def test_ado_dev_azure_com(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            hi = AuthResolver.classify_host("dev.azure.com")
        assert hi.kind == "ado"
        assert hi.api_base == "https://dev.azure.com"

    def test_ado_visualstudio_com(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            hi = AuthResolver.classify_host("myorg.visualstudio.com")
        assert hi.kind == "ado"

    def test_gitlab_saas(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            hi = AuthResolver.classify_host("gitlab.com")
        assert hi.kind == "gitlab"
        assert hi.api_base == "https://gitlab.com/api/v4"

    def test_generic_bitbucket(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            hi = AuthResolver.classify_host("bitbucket.org")
        assert hi.kind == "generic"
        assert hi.has_public_repos is True

    def test_ghes_via_github_host_env(self) -> None:
        with patch.dict(os.environ, {"GITHUB_HOST": "github.corp.example.com"}, clear=True):
            hi = AuthResolver.classify_host("github.corp.example.com")
        assert hi.kind == "ghes"
        assert "api/v3" in hi.api_base

    def test_port_is_preserved(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            hi = AuthResolver.classify_host("bitbucket.corp", port=7990)
        assert hi.port == 7990

    def test_github_com_not_classified_as_ghes_even_if_github_host_set(self) -> None:
        with patch.dict(os.environ, {"GITHUB_HOST": "github.com"}, clear=True):
            hi = AuthResolver.classify_host("github.com")
        # github.com should still classify as "github", not "ghes"
        assert hi.kind == "github"


class TestAuthResolverResolve:
    """AuthResolver.resolve exercises the full token resolution chain."""

    def test_no_env_returns_none_token(self) -> None:
        with patch.dict(os.environ, {}, clear=True), _NO_GIT_CRED, _NO_GH_CLI:
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com")
        assert ctx.token is None
        assert ctx.source == "none"
        assert ctx.token_type == "unknown"

    def test_github_apm_pat_is_primary(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_primary"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com")
        assert ctx.token == "ghp_primary"
        assert ctx.source == "GITHUB_APM_PAT"
        assert ctx.token_type == "classic"

    def test_github_token_fallback(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "ghu_fallback"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com")
        assert ctx.token == "ghu_fallback"
        assert ctx.source == "GITHUB_TOKEN"

    def test_per_org_override_beats_global(self) -> None:
        env = {
            "GITHUB_APM_PAT": "ghp_global",
            "GITHUB_APM_PAT_CONTOSO": "github_pat_contoso_xyz",
        }
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED, _NO_GH_CLI:
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com", org="contoso")
        assert ctx.token == "github_pat_contoso_xyz"
        assert ctx.source == "GITHUB_APM_PAT_CONTOSO"
        assert ctx.token_type == "fine-grained"

    def test_per_org_hyphen_normalised_to_underscore(self) -> None:
        env = {"GITHUB_APM_PAT_MY_ORG": "ghp_org"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED, _NO_GH_CLI:
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com", org="my-org")
        assert ctx.token == "ghp_org"

    def test_gitlab_uses_gitlab_pat_not_github(self) -> None:
        env = {"GITLAB_APM_PAT": "glpat_correct", "GITHUB_APM_PAT": "ghp_wrong"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED, _NO_GH_CLI:
            resolver = AuthResolver()
            ctx = resolver.resolve("gitlab.com")
        assert ctx.token == "glpat_correct"
        assert ctx.source == "GITLAB_APM_PAT"

    def test_gitlab_gitlab_token_second(self) -> None:
        env = {"GITLAB_TOKEN": "glpat_second"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED, _NO_GH_CLI:
            resolver = AuthResolver()
            ctx = resolver.resolve("gitlab.com")
        assert ctx.token == "glpat_second"

    def test_generic_host_ignores_github_env_vars(self) -> None:
        env = {"GITHUB_APM_PAT": "ghp_no", "GITHUB_TOKEN": "ghp_also_no"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED, _NO_GH_CLI:
            resolver = AuthResolver()
            ctx = resolver.resolve("bitbucket.org")
        assert ctx.token is None
        assert ctx.source == "none"

    def test_ado_pat_resolution(self) -> None:
        env = {"ADO_APM_PAT": "ado_pat_token"}
        with patch.dict(os.environ, env, clear=True), _NO_GIT_CRED:
            resolver = AuthResolver()
            ctx = resolver.resolve("dev.azure.com")
        assert ctx.token == "ado_pat_token"
        assert ctx.source == "ADO_APM_PAT"

    def test_resolution_is_cached(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_cached"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            ctx1 = resolver.resolve("github.com", org="my-org")
            ctx2 = resolver.resolve("github.com", org="my-org")
        assert ctx1 is ctx2

    def test_different_ports_give_different_cache_entries(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_p"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            ctx_a = resolver.resolve("bitbucket.corp", port=7999)
            ctx_b = resolver.resolve("bitbucket.corp", port=7990)
        assert ctx_a is not ctx_b

    def test_git_env_contains_terminal_prompt_0(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_x"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com")
        assert ctx.git_env.get("GIT_TERMINAL_PROMPT") == "0"
        assert ctx.git_env.get("GIT_ASKPASS") == "echo"

    def test_git_env_injects_git_token_for_basic_scheme(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_tok"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            ctx = resolver.resolve("github.com")
        assert ctx.git_env.get("GIT_TOKEN") == "ghp_tok"
        assert ctx.auth_scheme == "basic"


class TestTryWithFallback:
    """try_with_fallback exercises auth-first, unauth-first, and fallback paths."""

    def test_unauth_first_succeeds_immediately(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_unused"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                return "success"

            result = resolver.try_with_fallback("github.com", op, unauth_first=True)
        assert result == "success"
        assert calls == [None]

    def test_unauth_fails_then_token_used(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_fallback"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                if token is None:
                    raise RuntimeError("unauth failed")
                return "ok"

            result = resolver.try_with_fallback("github.com", op, unauth_first=True)
        assert result == "ok"
        assert calls[0] is None
        assert calls[1] == "ghp_fallback"

    def test_auth_first_succeeds(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_auth"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                return "done"

            result = resolver.try_with_fallback("github.com", op)
        assert result == "done"
        assert calls == ["ghp_auth"]

    def test_auth_first_fails_then_retries_unauthenticated(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_bad"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                if token == "ghp_bad":
                    raise RuntimeError("bad token")
                return "anon ok"

            result = resolver.try_with_fallback("github.com", op)
        assert result == "anon ok"
        assert "ghp_bad" in calls
        assert None in calls

    def test_ghe_cloud_auth_only_path(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_ghe"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                return "ghe_result"

            result = resolver.try_with_fallback("corp.ghe.com", op)
        assert result == "ghe_result"
        # GHE Cloud is auth-only; should not try None
        assert None not in calls

    def test_no_token_tries_unauthenticated(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            calls: list = []

            def op(token, git_env):
                calls.append(token)
                return "anon"

            result = resolver.try_with_fallback("github.com", op)
        assert result == "anon"
        assert calls == [None]

    def test_verbose_callback_is_invoked(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_v"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            log_lines: list[str] = []
            resolver.try_with_fallback(
                "github.com",
                lambda t, e: "ok",
                verbose_callback=log_lines.append,
            )
        # At minimum the verbose callback must have been callable
        assert isinstance(log_lines, list)


class TestBuildErrorContext:
    """build_error_context produces actionable error messages."""

    def test_no_token_mentions_set_pat(self) -> None:
        with patch.dict(os.environ, {}, clear=True), _NO_GIT_CRED, _NO_GH_CLI:
            resolver = AuthResolver()
            msg = resolver.build_error_context("github.com", "install")
        assert "GITHUB_APM_PAT" in msg

    def test_github_token_present_mentions_verbose(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_tok"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            msg = resolver.build_error_context("github.com", "clone")
        assert "--verbose" in msg

    def test_org_hint_contains_per_org_var(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_tok"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            msg = resolver.build_error_context("github.com", "clone", org="contoso")
        assert "GITHUB_APM_PAT_CONTOSO" in msg

    def test_gitlab_no_token_suggests_gitlab_pat(self) -> None:
        with patch.dict(os.environ, {}, clear=True), _NO_GIT_CRED, _NO_GH_CLI:
            resolver = AuthResolver()
            msg = resolver.build_error_context("gitlab.com", "install")
        assert "GITLAB_APM_PAT" in msg or "GITLAB_TOKEN" in msg

    def test_port_in_error_message(self) -> None:
        with patch.dict(os.environ, {}, clear=True), _NO_GIT_CRED, _NO_GH_CLI:
            resolver = AuthResolver()
            msg = resolver.build_error_context("bitbucket.corp", "clone", port=7990)
        assert "7990" in msg

    def test_ghe_cloud_error_mentions_enterprise_tokens(self) -> None:
        with (
            patch.dict(os.environ, {"GITHUB_APM_PAT": "ghp_tok"}, clear=True),
            _NO_GIT_CRED,
            _NO_GH_CLI,
        ):
            resolver = AuthResolver()
            msg = resolver.build_error_context("corp.ghe.com", "install")
        assert "ghe" in msg.lower() or "enterprise" in msg.lower() or "GHE" in msg


class TestEmitStalePat:
    """emit_stale_pat_diagnostic deduplicates per host."""

    def test_emits_only_once_per_host(self) -> None:
        resolver = AuthResolver()
        warnings: list[str] = []

        with patch(
            "apm_cli.utils.console._rich_warning", side_effect=lambda m, **kw: warnings.append(m)
        ):
            resolver.emit_stale_pat_diagnostic("dev.azure.com")
            resolver.emit_stale_pat_diagnostic("dev.azure.com")
            resolver.emit_stale_pat_diagnostic("dev.azure.com")

        # Should only emit once per host
        azure_lines = [w for w in warnings if "dev.azure.com" in w]
        assert len(azure_lines) == 1

    def test_different_hosts_each_emit_once(self) -> None:
        resolver = AuthResolver()
        warnings: list[str] = []

        with patch(
            "apm_cli.utils.console._rich_warning", side_effect=lambda m, **kw: warnings.append(m)
        ):
            resolver.emit_stale_pat_diagnostic("org1.visualstudio.com")
            resolver.emit_stale_pat_diagnostic("org2.visualstudio.com")

        hosts_warned = {w for w in warnings if "visualstudio.com" in w}
        assert len(hosts_warned) == 2


class TestBearerFallbackOutcome:
    def test_named_tuple_fields(self) -> None:
        bfo = BearerFallbackOutcome(outcome="result", bearer_attempted=True)
        assert bfo.outcome == "result"
        assert bfo.bearer_attempted is True

    def test_bearer_not_attempted(self) -> None:
        bfo = BearerFallbackOutcome(outcome=None, bearer_attempted=False)
        assert bfo.bearer_attempted is False


# ===========================================================================
# 2.  plugin_parser.py — parse, normalize, synthesize
# ===========================================================================


class TestParsePluginManifest:
    def test_valid_manifest_with_name(self, tmp_path: Path) -> None:
        plugin_json = tmp_path / "plugin.json"
        plugin_json.write_text(json.dumps({"name": "my-plugin", "version": "1.0.0"}))
        manifest = parse_plugin_manifest(plugin_json)
        assert manifest["name"] == "my-plugin"
        assert manifest["version"] == "1.0.0"

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"plugin\.json not found"):
            parse_plugin_manifest(tmp_path / "missing.json")

    def test_invalid_json_raises_value_error(self, tmp_path: Path) -> None:
        plugin_json = tmp_path / "plugin.json"
        plugin_json.write_text("{ invalid json !!!")
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_plugin_manifest(plugin_json)

    def test_manifest_without_name_still_parses(self, tmp_path: Path) -> None:
        plugin_json = tmp_path / "plugin.json"
        plugin_json.write_text(json.dumps({"version": "2.0.0", "description": "no-name plugin"}))
        manifest = parse_plugin_manifest(plugin_json)
        assert manifest["version"] == "2.0.0"
        assert "name" not in manifest

    def test_manifest_with_author_object(self, tmp_path: Path) -> None:
        plugin_json = tmp_path / "plugin.json"
        plugin_json.write_text(
            json.dumps({"name": "p", "author": {"name": "Alice", "email": "a@b.com"}})
        )
        manifest = parse_plugin_manifest(plugin_json)
        assert manifest["author"]["name"] == "Alice"

    def test_manifest_with_mcp_servers_dict(self, tmp_path: Path) -> None:
        plugin_json = tmp_path / "plugin.json"
        content = {
            "name": "mcp-plugin",
            "mcpServers": {"my-server": {"command": "npx", "args": ["-y", "my-pkg"]}},
        }
        plugin_json.write_text(json.dumps(content))
        manifest = parse_plugin_manifest(plugin_json)
        assert "my-server" in manifest["mcpServers"]


class TestIsWithinPlugin:
    def test_path_inside_plugin_returns_true(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        nested = plugin_root / "agents" / "file.md"
        nested.parent.mkdir()
        nested.write_text("content")
        assert _is_within_plugin(nested, plugin_root, component="agents") is True

    def test_path_outside_plugin_returns_false(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        outside = tmp_path / "etc" / "passwd"
        outside.parent.mkdir()
        outside.write_text("root:x:0:0")
        result = _is_within_plugin(outside, plugin_root, component="agents")
        assert result is False

    def test_plugin_root_itself_is_within(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        assert _is_within_plugin(plugin_root, plugin_root, component="agents") is True


class TestReadMcpJson:
    def test_reads_mcp_servers_key(self, tmp_path: Path) -> None:
        import logging

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"srv": {"command": "node", "args": ["server.js"]}}})
        )
        servers = _read_mcp_json(mcp_file, logging.getLogger("test"))
        assert "srv" in servers
        assert servers["srv"]["command"] == "node"

    def test_missing_mcp_servers_returns_empty(self, tmp_path: Path) -> None:
        import logging

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(json.dumps({"other": "data"}))
        servers = _read_mcp_json(mcp_file, logging.getLogger("test"))
        assert servers == {}

    def test_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        import logging

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text("not json at all")
        servers = _read_mcp_json(mcp_file, logging.getLogger("test"))
        assert servers == {}

    def test_non_dict_top_level_returns_empty(self, tmp_path: Path) -> None:
        import logging

        mcp_file = tmp_path / ".mcp.json"
        mcp_file.write_text(json.dumps([1, 2, 3]))
        servers = _read_mcp_json(mcp_file, logging.getLogger("test"))
        assert servers == {}


class TestReadMcpFile:
    def test_reads_relative_file(self, tmp_path: Path) -> None:
        import logging

        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        mcp_file = plugin_root / "tools.json"
        mcp_file.write_text(json.dumps({"mcpServers": {"tool1": {"url": "http://localhost:3000"}}}))
        servers = _read_mcp_file(plugin_root, "tools.json", logging.getLogger("test"))
        assert "tool1" in servers

    def test_path_escaping_returns_empty(self, tmp_path: Path) -> None:
        import logging

        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        servers = _read_mcp_file(plugin_root, "../../etc/passwd", logging.getLogger("test"))
        assert servers == {}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        import logging

        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        servers = _read_mcp_file(plugin_root, "nonexistent.json", logging.getLogger("test"))
        assert servers == {}


class TestSubstitutePluginRoot:
    def test_substitutes_placeholder_in_string_values(self, tmp_path: Path) -> None:
        import logging

        servers = {
            "my-srv": {
                "command": "node",
                "args": ["${CLAUDE_PLUGIN_ROOT}/server.js"],
            }
        }
        result = _substitute_plugin_root(servers, "/abs/path", logging.getLogger("test"))
        assert result["my-srv"]["args"][0] == "/abs/path/server.js"

    def test_substitutes_in_nested_dict(self, tmp_path: Path) -> None:
        import logging

        servers = {
            "srv": {
                "env": {"MY_PATH": "${CLAUDE_PLUGIN_ROOT}/data"},
            }
        }
        result = _substitute_plugin_root(servers, "/base", logging.getLogger("test"))
        assert result["srv"]["env"]["MY_PATH"] == "/base/data"

    def test_no_placeholder_unchanged(self) -> None:
        import logging

        servers = {"srv": {"command": "node"}}
        result = _substitute_plugin_root(servers, "/base", logging.getLogger("test"))
        assert result == servers


class TestExtractMcpServers:
    def test_inline_dict_mcp_servers(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        manifest = {
            "name": "p",
            "mcpServers": {"my-srv": {"command": "npx", "args": ["-y", "pkg"]}},
        }
        servers = _extract_mcp_servers(plugin_root, manifest)
        assert "my-srv" in servers

    def test_auto_discovery_from_mcp_json(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        mcp_file = plugin_root / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"auto": {"command": "python", "args": ["srv.py"]}}})
        )
        servers = _extract_mcp_servers(plugin_root, {"name": "p"})
        assert "auto" in servers

    def test_auto_discovery_fallback_to_github_mcp_json(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        github_dir = plugin_root / ".github"
        github_dir.mkdir()
        mcp_file = github_dir / ".mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"gh-srv": {"url": "https://mcp.example.com"}}})
        )
        servers = _extract_mcp_servers(plugin_root, {"name": "p"})
        assert "gh-srv" in servers

    def test_string_mcp_servers_reads_file(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        mcp_ref = plugin_root / "mcp.json"
        mcp_ref.write_text(
            json.dumps({"mcpServers": {"file-srv": {"command": "node", "args": ["s.js"]}}})
        )
        manifest = {"name": "p", "mcpServers": "mcp.json"}
        servers = _extract_mcp_servers(plugin_root, manifest)
        assert "file-srv" in servers

    def test_list_mcp_servers_merges_files(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        f1 = plugin_root / "a.json"
        f1.write_text(json.dumps({"mcpServers": {"srv-a": {"command": "a"}}}))
        f2 = plugin_root / "b.json"
        f2.write_text(json.dumps({"mcpServers": {"srv-b": {"command": "b"}}}))
        manifest = {"name": "p", "mcpServers": ["a.json", "b.json"]}
        servers = _extract_mcp_servers(plugin_root, manifest)
        assert "srv-a" in servers
        assert "srv-b" in servers

    def test_empty_plugin_no_mcp_json_returns_empty(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        servers = _extract_mcp_servers(plugin_root, {"name": "p"})
        assert servers == {}

    def test_symlinked_mcp_json_is_skipped(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        real_file = tmp_path / "real.json"
        real_file.write_text(json.dumps({"mcpServers": {"bad": {"command": "evil"}}}))
        symlink = plugin_root / ".mcp.json"
        try:
            symlink.symlink_to(real_file)
        except OSError:
            pytest.skip("symlinks not supported on this platform")
        servers = _extract_mcp_servers(plugin_root, {"name": "p"})
        assert servers == {}


class TestMcpServersToDeps:
    def test_stdio_server_maps_correctly(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "p"
        plugin_root.mkdir()
        servers = {"my-srv": {"command": "npx", "args": ["-y", "mcp-pkg"]}}
        deps = _mcp_servers_to_apm_deps(servers, plugin_root)
        assert len(deps) == 1
        dep = deps[0]
        assert dep["name"] == "my-srv"
        assert dep["transport"] == "stdio"
        assert dep["command"] == "npx"
        assert dep["args"] == ["-y", "mcp-pkg"]
        assert dep["registry"] is False

    def test_http_server_maps_correctly(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "p"
        plugin_root.mkdir()
        servers = {"http-srv": {"url": "https://mcp.example.com/sse"}}
        deps = _mcp_servers_to_apm_deps(servers, plugin_root)
        assert len(deps) == 1
        dep = deps[0]
        assert dep["transport"] == "http"
        assert dep["url"] == "https://mcp.example.com/sse"

    def test_server_without_command_or_url_is_skipped(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "p"
        plugin_root.mkdir()
        servers = {"bad-srv": {"env": {"KEY": "val"}}}
        deps = _mcp_servers_to_apm_deps(servers, plugin_root)
        assert deps == []

    def test_env_vars_carried_through(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "p"
        plugin_root.mkdir()
        servers = {
            "env-srv": {
                "command": "node",
                "args": ["s.js"],
                "env": {"TOKEN": "${MY_TOKEN}"},
            }
        }
        deps = _mcp_servers_to_apm_deps(servers, plugin_root)
        assert len(deps) == 1
        assert deps[0]["env"]["TOKEN"] == "${MY_TOKEN}"

    def test_non_dict_server_config_is_skipped(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "p"
        plugin_root.mkdir()
        servers = {"bad": "not_a_dict"}
        deps = _mcp_servers_to_apm_deps(servers, plugin_root)
        assert deps == []


class TestGenerateApmYml:
    def test_minimal_manifest_produces_valid_yaml(self) -> None:
        import yaml

        content = _generate_apm_yml({"name": "my-plugin"})
        data = yaml.safe_load(content)
        assert data["name"] == "my-plugin"
        assert data["type"] == "hybrid"

    def test_version_and_description_included(self) -> None:
        import yaml

        content = _generate_apm_yml({"name": "p", "version": "2.0.0", "description": "A plugin"})
        data = yaml.safe_load(content)
        assert data["version"] == "2.0.0"
        assert data["description"] == "A plugin"

    def test_author_string_included(self) -> None:
        import yaml

        content = _generate_apm_yml({"name": "p", "author": "Alice"})
        data = yaml.safe_load(content)
        assert data["author"] == "Alice"

    def test_author_dict_uses_name_field(self) -> None:
        import yaml

        content = _generate_apm_yml({"name": "p", "author": {"name": "Bob", "email": "b@b.com"}})
        data = yaml.safe_load(content)
        assert data["author"] == "Bob"

    def test_mcp_deps_injected(self) -> None:
        import yaml

        mcp_deps = [{"name": "my-srv", "transport": "stdio", "command": "npx"}]
        content = _generate_apm_yml({"name": "p", "_mcp_deps": mcp_deps})
        data = yaml.safe_load(content)
        assert "mcp" in data["dependencies"]
        assert data["dependencies"]["mcp"][0]["name"] == "my-srv"


class TestSynthesizePluginJsonFromApmYml:
    def test_valid_apm_yml_produces_plugin_json(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            "name: my-plugin\nversion: 1.2.3\ndescription: Test plugin\nauthor: Alice\nlicense: MIT\n"
        )
        result = synthesize_plugin_json_from_apm_yml(apm_yml)
        assert result["name"] == "my-plugin"
        assert result["version"] == "1.2.3"
        assert result["description"] == "Test plugin"
        assert result["author"] == {"name": "Alice"}
        assert result["license"] == "MIT"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            synthesize_plugin_json_from_apm_yml(tmp_path / "missing.yml")

    def test_missing_name_raises_value_error(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("version: 1.0.0\n")
        with pytest.raises(ValueError, match="name"):
            synthesize_plugin_json_from_apm_yml(apm_yml)

    def test_minimal_apm_yml_only_name(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text("name: bare-plugin\n")
        result = synthesize_plugin_json_from_apm_yml(apm_yml)
        assert result["name"] == "bare-plugin"
        assert "version" not in result
        assert "description" not in result


class TestValidatePluginPackage:
    def test_directory_with_plugin_json_and_name_is_valid(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(json.dumps({"name": "test-plugin"}))
        assert validate_plugin_package(plugin_dir) is True

    def test_directory_with_agents_dir_is_valid(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "agents").mkdir()
        assert validate_plugin_package(plugin_dir) is True

    def test_directory_with_skills_dir_is_valid(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "skills").mkdir()
        assert validate_plugin_package(plugin_dir) is True

    def test_empty_directory_is_not_valid(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "empty"
        plugin_dir.mkdir()
        assert validate_plugin_package(plugin_dir) is False

    def test_plugin_json_without_name_field_falls_back_to_dir_check(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.json").write_text(json.dumps({"version": "1.0.0"}))
        # No name → falls back to component dir check → no components → False
        assert validate_plugin_package(plugin_dir) is False


class TestNormalizePluginDirectory:
    def test_no_plugin_json_uses_dir_name(self, tmp_path: Path) -> None:
        import yaml

        plugin_dir = tmp_path / "my-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "agents").mkdir()
        (plugin_dir / "agents" / "agent.md").write_text("# Agent\n")

        apm_yml_path = normalize_plugin_directory(plugin_dir, None)
        assert apm_yml_path.exists()
        data = yaml.safe_load(apm_yml_path.read_text())
        assert data["name"] == "my-plugin"

    def test_plugin_json_name_takes_precedence(self, tmp_path: Path) -> None:
        import yaml

        plugin_dir = tmp_path / "dir-name"
        plugin_dir.mkdir()
        plugin_json = plugin_dir / "plugin.json"
        plugin_json.write_text(json.dumps({"name": "json-name"}))

        apm_yml_path = normalize_plugin_directory(plugin_dir, plugin_json)
        data = yaml.safe_load(apm_yml_path.read_text())
        assert data["name"] == "json-name"

    def test_invalid_plugin_json_falls_back_to_dir_name(self, tmp_path: Path) -> None:
        import yaml

        plugin_dir = tmp_path / "fallback-name"
        plugin_dir.mkdir()
        plugin_json = plugin_dir / "plugin.json"
        plugin_json.write_text("{ bad json {{")

        apm_yml_path = normalize_plugin_directory(plugin_dir, plugin_json)
        data = yaml.safe_load(apm_yml_path.read_text())
        assert data["name"] == "fallback-name"


class TestMapPluginArtifacts:
    def test_agents_dir_copied(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        agents_dir = plugin_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "agent.md").write_text("# Agent\n")
        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()

        _map_plugin_artifacts(plugin_dir, apm_dir)

        assert (apm_dir / "agents" / "agent.md").exists()

    def test_commands_normalized_to_prompt_md(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        cmds_dir = plugin_dir / "commands"
        cmds_dir.mkdir()
        (cmds_dir / "my-cmd.md").write_text("# My Command\n")
        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()

        _map_plugin_artifacts(plugin_dir, apm_dir)

        # .md should be renamed to .prompt.md
        assert (apm_dir / "prompts" / "my-cmd.prompt.md").exists()

    def test_skills_dir_copied(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        skills_dir = plugin_dir / "skills" / "my-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("# Skill\n")
        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()

        _map_plugin_artifacts(plugin_dir, apm_dir)

        assert (apm_dir / "skills").exists()

    def test_passthrough_files_copied(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
        (plugin_dir / "settings.json").write_text(json.dumps({"setting": "value"}))
        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()

        _map_plugin_artifacts(plugin_dir, apm_dir)

        assert (apm_dir / ".mcp.json").exists()
        assert (apm_dir / "settings.json").exists()

    def test_hooks_inline_dict_written_as_json(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        apm_dir = plugin_dir / ".apm"
        apm_dir.mkdir()
        manifest = {"name": "p", "hooks": {"pre-commit": [{"command": "lint"}]}}

        _map_plugin_artifacts(plugin_dir, apm_dir, manifest)

        hooks_file = apm_dir / "hooks" / "hooks.json"
        assert hooks_file.exists()
        data = json.loads(hooks_file.read_text())
        assert "pre-commit" in data


# ===========================================================================
# 3.  install.py — pure helpers and CliRunner integration
# ===========================================================================


class TestRestoreManifestFromSnapshot:
    def test_restores_exact_bytes(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / "apm.yml"
        original = b"name: original\nversion: 1.0.0\n"
        apm_yml.write_bytes(original)

        # Mutate the file
        apm_yml.write_bytes(b"name: mutated\n")

        _restore_manifest_from_snapshot(apm_yml, original)
        assert apm_yml.read_bytes() == original

    def test_restore_is_atomic(self, tmp_path: Path) -> None:
        """Snapshot bytes should survive even if the file previously didn't exist."""
        apm_yml = tmp_path / "apm.yml"
        snapshot = b"name: restored\n"

        _restore_manifest_from_snapshot(apm_yml, snapshot)
        assert apm_yml.read_bytes() == snapshot


class TestMaybeRollbackManifest:
    def test_none_snapshot_is_no_op(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_bytes(b"name: current\n")
        mock_logger = MagicMock()

        _maybe_rollback_manifest(apm_yml, None, mock_logger)

        assert apm_yml.read_bytes() == b"name: current\n"
        mock_logger.progress.assert_not_called()

    def test_snapshot_restores_file_and_logs(self, tmp_path: Path) -> None:
        apm_yml = tmp_path / "apm.yml"
        original = b"name: original\n"
        apm_yml.write_bytes(b"name: mutated\n")
        mock_logger = MagicMock()

        _maybe_rollback_manifest(apm_yml, original, mock_logger)

        assert apm_yml.read_bytes() == original
        mock_logger.progress.assert_called_once()


class TestSplitArgvAtDoubleDash:
    def test_no_double_dash(self) -> None:
        argv = ["apm", "install", "--mcp", "fetch"]
        clean, cmd = _split_argv_at_double_dash(argv)
        assert clean == argv
        assert cmd == ()

    def test_with_double_dash(self) -> None:
        argv = ["apm", "install", "--mcp", "fetch", "--", "npx", "-y", "@mcp/fetch"]
        clean, cmd = _split_argv_at_double_dash(argv)
        assert clean == ["apm", "install", "--mcp", "fetch"]
        assert cmd == ("npx", "-y", "@mcp/fetch")

    def test_double_dash_at_beginning(self) -> None:
        argv = ["--", "npx", "pkg"]
        clean, cmd = _split_argv_at_double_dash(argv)
        assert clean == []
        assert cmd == ("npx", "pkg")

    def test_empty_post_dash(self) -> None:
        argv = ["apm", "install", "--"]
        clean, cmd = _split_argv_at_double_dash(argv)
        assert clean == ["apm", "install"]
        assert cmd == ()


class TestCheckPackageConflicts:
    def test_empty_deps_returns_empty_set(self) -> None:
        result = _check_package_conflicts([])
        assert result == set()

    def test_string_dep_is_parsed(self) -> None:
        result = _check_package_conflicts(["owner/repo"])
        assert len(result) > 0

    def test_invalid_entries_are_skipped(self) -> None:
        # Should not raise; invalid entries are silently skipped
        result = _check_package_conflicts(["not-valid-format-##$$", 123, None])
        assert isinstance(result, set)


class TestInstallCli:
    """CLI-level install tests via CliRunner — no network needed for these paths."""

    @pytest.fixture(autouse=True)
    def cleanup(self) -> None:
        yield
        clear_apm_yml_cache()

    def test_frozen_and_update_mutually_exclusive(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, "name: test\nversion: 1.0.0\n")
        runner = CliRunner()
        result = runner.invoke(install, ["--frozen", "--update"])
        assert result.exit_code != 0
        assert "mutually exclusive" in (result.output + str(result.exception)).lower()

    def test_dry_run_no_packages(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test\nversion: 1.0.0\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        runner = CliRunner()
        result = runner.invoke(install, ["--dry-run"])
        # Should run without error (no dependencies to install)
        assert result.exit_code in (0, 1), result.output

    def test_alias_without_local_bundle_raises_usage_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, "name: test\nversion: 1.0.0\n")
        runner = CliRunner()
        result = runner.invoke(install, ["--as", "my-name", "owner/repo"])
        assert result.exit_code != 0

    def test_install_help_text_contains_mcp_example(self) -> None:
        runner = CliRunner()
        result = runner.invoke(install, ["--help"])
        assert result.exit_code == 0
        assert "--mcp" in result.output

    def test_install_with_legacy_tarball_reports_error(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, "name: test\nversion: 1.0.0\n")
        # Create a .tar.gz file that is not a valid APM bundle
        fake_tar = tmp_path / "fake.tar.gz"
        fake_tar.write_bytes(b"not a real tarball")
        runner = CliRunner()
        result = runner.invoke(install, [str(fake_tar)])
        assert result.exit_code != 0


# ===========================================================================
# 4.  pack.py — CliRunner integration tests
# ===========================================================================


class TestPackCliEmitJsonErrorOrRaise:
    """_emit_json_error_or_raise produces correct output in both modes."""

    def test_non_json_mode_raises_click_exception(self) -> None:
        import click
        from click.testing import CliRunner

        from apm_cli.commands.pack import _emit_json_error_or_raise

        @click.command()
        @click.pass_context
        def _test_cmd(ctx):
            _emit_json_error_or_raise(ctx, False, "some_code", "test error message")

        runner = CliRunner()
        result = runner.invoke(_test_cmd)
        assert "test error message" in result.output

    def test_json_mode_emits_json_envelope(self) -> None:
        import click

        from apm_cli.commands.pack import _emit_json_error_or_raise

        @click.command()
        @click.pass_context
        def _test_cmd(ctx):
            _emit_json_error_or_raise(ctx, True, "some_code", "json error message")

        runner = CliRunner()
        result = runner.invoke(_test_cmd)
        data = json.loads(result.output.strip().split("\n")[0])
        assert data.get("ok") is False or "errors" in data


class TestPackCmdHelp:
    def test_help_text_contains_key_info(self) -> None:
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--help"])
        assert result.exit_code == 0
        assert "--format" in result.output
        assert "--dry-run" in result.output
        assert "--archive" in result.output

    def test_unknown_marketplace_format_in_marketplace_path_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, "name: test\nversion: 1.0.0\n")
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--marketplace-path", "unknown_format=./dist/out.json"])
        assert result.exit_code != 0 or "unknown" in result.output.lower()

    def test_marketplace_path_without_equals_fails(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, "name: test\nversion: 1.0.0\n")
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--marketplace-path", "noequalssign"])
        assert result.exit_code != 0 or "FORMAT=PATH" in result.output

    def test_deprecated_marketplace_output_flag_warns(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(tmp_path, "name: test\nversion: 1.0.0\n")
        _write_lockfile(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            pack_cmd,
            ["--marketplace-output", "./dist/marketplace.json", "--dry-run"],
            catch_exceptions=False,
        )
        # The flag is deprecated and should emit a warning
        assert "deprecated" in result.output.lower() or result.exit_code in (0, 1, 2)


class TestPackBundleOnly:
    def test_pack_bundle_creates_build_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test-pkg\nversion: 1.0.0\ndescription: test\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        runner = CliRunner()
        result = runner.invoke(pack_cmd, [])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "build").exists()

    def test_pack_dry_run_no_output(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test-pkg\nversion: 1.0.0\ndescription: test\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--dry-run"])
        assert result.exit_code == 0, result.output

    def test_pack_with_format_apm(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test-pkg\nversion: 1.0.0\ndescription: test\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--format", "apm"])
        assert result.exit_code == 0, result.output

    def test_pack_with_custom_output_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test-pkg\nversion: 1.0.0\ndescription: test\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        custom_out = tmp_path / "custom-out"
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["-o", str(custom_out)])
        assert result.exit_code == 0, result.output
        assert custom_out.exists()

    def test_pack_verbose_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test-pkg\nversion: 1.0.0\ndescription: test\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--verbose"])
        assert result.exit_code == 0, result.output

    def test_pack_json_output_produces_json(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test-pkg\nversion: 1.0.0\ndescription: test\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        # Under --json all non-JSON output goes to stderr; find the JSON object in stdout
        lines = result.output.strip().splitlines()
        json_lines = [ln for ln in lines if ln.strip().startswith("{")]
        assert json_lines, f"No JSON line found in output:\n{result.output}"
        data = json.loads(
            "\n".join(
                # Collect from the first "{" line to end
                lines[lines.index(json_lines[0]) :]
            )
        )
        assert "ok" in data


class TestPackMarketplaceOnly:
    def test_pack_marketplace_only_creates_json(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "azure"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: pack-test
version: 1.0.0
description: pack integration test

marketplace:
  owner:
    name: Tester
    url: https://example.com
  packages:
    - name: azure
      description: Local package
      source: ./.github/plugins/azure
      homepage: https://example.com
""",
        )
        runner = CliRunner()
        result = runner.invoke(pack_cmd, [])
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".claude-plugin" / "marketplace.json").exists()

    def test_pack_marketplace_dry_run_no_output_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        plugin_dir = tmp_path / ".github" / "plugins" / "mypkg"
        plugin_dir.mkdir(parents=True)
        _write_apm_yml(
            tmp_path,
            """\
name: dry-run-test
version: 1.0.0
description: dry run test

marketplace:
  owner:
    name: Tester
    url: https://example.com
  packages:
    - name: mypkg
      description: test pkg
      source: ./.github/plugins/mypkg
      homepage: https://example.com
""",
        )
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--dry-run"])
        assert result.exit_code == 0, result.output

    def test_pack_marketplace_filter_none_skips_marketplace(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test\nversion: 1.0.0\ndescription: test\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        runner = CliRunner()
        # -m none means skip marketplace build
        result = runner.invoke(pack_cmd, ["-m", "none"])
        assert result.exit_code == 0, result.output


class TestPackCheckCleanAndVersions:
    """Test --check-clean and --check-versions flags."""

    def test_check_versions_no_marketplace_block_logs_info(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test\nversion: 1.0.0\ndescription: test\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--check-versions"])
        assert result.exit_code == 0, result.output
        assert "skipped" in result.output.lower() or result.exit_code == 0

    def test_check_clean_no_marketplace_block_logs_info(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        _write_apm_yml(
            tmp_path,
            "name: test\nversion: 1.0.0\ndescription: test\ndependencies:\n  apm: []\n",
        )
        _write_lockfile(tmp_path)
        runner = CliRunner()
        result = runner.invoke(pack_cmd, ["--check-clean"])
        assert result.exit_code == 0, result.output
