"""Tests for registry auth resolution.

Covers the env-var key derivation (``APM_REGISTRY_TOKEN_{NAME}``), URL-prefix
matching for lockfile re-installs, and the §6.2 remediation message.
"""

from __future__ import annotations

import pytest

from apm_cli.deps.registry.auth import (
    RegistryAuthContext,
    _env_key,
    _normalize_url_prefix,
    lookup_name_for_url,
    remediation_message,
    resolve_for_url,
    resolve_registry_token,
)


class TestEnvKey:
    """Env-var key derivation per design §7.1."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("corp", "APM_REGISTRY_TOKEN_CORP"),
            ("corp-main", "APM_REGISTRY_TOKEN_CORP_MAIN"),
            ("corp.main", "APM_REGISTRY_TOKEN_CORP_MAIN"),
            ("Corp-Main", "APM_REGISTRY_TOKEN_CORP_MAIN"),
            ("a-b.c", "APM_REGISTRY_TOKEN_A_B_C"),
        ],
    )
    def test_key_form(self, name, expected):
        assert _env_key(name) == expected


class TestResolveRegistryToken:
    def test_returns_env_value(self, monkeypatch):
        monkeypatch.setenv("APM_REGISTRY_TOKEN_CORP_MAIN", "tok-123")
        assert resolve_registry_token("corp-main") == "tok-123"

    def test_missing_env_returns_none(self, monkeypatch):
        monkeypatch.delenv("APM_REGISTRY_TOKEN_NOPE", raising=False)
        assert resolve_registry_token("nope") is None


class TestRegistryAuthContext:
    def test_anonymous_has_no_header(self):
        ctx = RegistryAuthContext(registry_name="corp", token=None)
        assert ctx.auth_header() is None

    def test_token_produces_bearer_header(self):
        ctx = RegistryAuthContext(registry_name="corp", token="abc")
        assert ctx.auth_header() == "Bearer abc"

    def test_basic_auth_when_user_password_set(self):
        ctx = RegistryAuthContext(
            registry_name="corp",
            token=None,
            username="admin",
            password="hunter2",
        )
        # Authorization: Basic base64("admin:hunter2") == "YWRtaW46aHVudGVyMg=="
        assert ctx.auth_header() == "Basic YWRtaW46aHVudGVyMg=="

    def test_bearer_wins_over_basic(self):
        # When both are populated, Bearer is authoritative.
        ctx = RegistryAuthContext(
            registry_name="corp",
            token="bearer-tok",
            username="u",
            password="p",
        )
        assert ctx.auth_header() == "Bearer bearer-tok"

    def test_partial_basic_returns_anonymous(self):
        # Only username set — no Basic header (need both).
        ctx = RegistryAuthContext(
            registry_name="corp",
            token=None,
            username="admin",
            password=None,
        )
        assert ctx.auth_header() is None


class TestBasicAuthEnvVars:
    def test_user_pass_env_keys(self, monkeypatch):
        from apm_cli.deps.registry.auth import _env_key_user, _env_key_pass

        assert _env_key_user("corp-main") == "APM_REGISTRY_USER_CORP_MAIN"
        assert _env_key_pass("corp-main") == "APM_REGISTRY_PASS_CORP_MAIN"
        assert _env_key_user("corp.main") == "APM_REGISTRY_USER_CORP_MAIN"

    def test_resolve_registry_basic(self, monkeypatch):
        from apm_cli.deps.registry.auth import resolve_registry_basic

        monkeypatch.setenv("APM_REGISTRY_USER_CORP", "admin")
        monkeypatch.setenv("APM_REGISTRY_PASS_CORP", "hunter2")
        assert resolve_registry_basic("corp") == ("admin", "hunter2")

    def test_resolve_registry_basic_partial_returns_none(self, monkeypatch):
        from apm_cli.deps.registry.auth import resolve_registry_basic

        monkeypatch.setenv("APM_REGISTRY_USER_CORP", "admin")
        monkeypatch.delenv("APM_REGISTRY_PASS_CORP", raising=False)
        # Either missing -> (None, None)
        assert resolve_registry_basic("corp") == (None, None)


class TestMakeAuthContext:
    def test_bearer_only(self, monkeypatch):
        from apm_cli.deps.registry.auth import make_auth_context

        monkeypatch.setenv("APM_REGISTRY_TOKEN_CORP", "bearer-tok")
        monkeypatch.delenv("APM_REGISTRY_USER_CORP", raising=False)
        monkeypatch.delenv("APM_REGISTRY_PASS_CORP", raising=False)
        ctx = make_auth_context("corp")
        assert ctx.token == "bearer-tok"
        assert ctx.username is None
        assert ctx.password is None
        assert ctx.auth_header().startswith("Bearer ")

    def test_basic_only(self, monkeypatch):
        from apm_cli.deps.registry.auth import make_auth_context

        monkeypatch.delenv("APM_REGISTRY_TOKEN_CORP", raising=False)
        monkeypatch.setenv("APM_REGISTRY_USER_CORP", "admin")
        monkeypatch.setenv("APM_REGISTRY_PASS_CORP", "hunter2")
        ctx = make_auth_context("corp")
        assert ctx.token is None
        assert ctx.username == "admin"
        assert ctx.password == "hunter2"
        assert ctx.auth_header().startswith("Basic ")

    def test_both_bearer_wins(self, monkeypatch):
        from apm_cli.deps.registry.auth import make_auth_context

        monkeypatch.setenv("APM_REGISTRY_TOKEN_CORP", "tok")
        monkeypatch.setenv("APM_REGISTRY_USER_CORP", "admin")
        monkeypatch.setenv("APM_REGISTRY_PASS_CORP", "hunter2")
        ctx = make_auth_context("corp")
        # Both populated; auth_header renders Bearer.
        assert ctx.auth_header() == "Bearer tok"


class TestUrlNormalize:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://Corp.example.com/apm", "https://corp.example.com/apm"),
            ("HTTPS://corp.example.com/apm/", "https://corp.example.com/apm"),
            ("https://corp:8443/apm/", "https://corp:8443/apm"),
            ("https://corp/apm/foo/", "https://corp/apm/foo"),
        ],
    )
    def test_normalize(self, url, expected):
        assert _normalize_url_prefix(url) == expected


class TestLookupNameForUrl:
    """URL prefix matching for lockfile re-install auth (§6.2 rule 1)."""

    def test_exact_match(self):
        regs = {"corp": "https://corp.example.com/apm"}
        assert lookup_name_for_url("https://corp.example.com/apm", regs) == "corp"

    def test_path_prefix_match(self):
        regs = {"corp": "https://corp.example.com/apm"}
        url = "https://corp.example.com/apm/v1/packages/acme/web/versions/1.0.0/tarball"
        assert lookup_name_for_url(url, regs) == "corp"

    def test_longest_prefix_wins(self):
        regs = {
            "corp": "https://corp.example.com/apm",
            "corp-team-a": "https://corp.example.com/apm/team-a",
        }
        team_url = "https://corp.example.com/apm/team-a/foo"
        assert lookup_name_for_url(team_url, regs) == "corp-team-a"
        other_url = "https://corp.example.com/apm/some-other"
        assert lookup_name_for_url(other_url, regs) == "corp"

    def test_no_match_returns_none(self):
        regs = {"corp": "https://corp.example.com/apm"}
        assert lookup_name_for_url("https://other.com/apm", regs) is None

    def test_case_insensitive_host(self):
        regs = {"corp": "https://CORP.example.com/apm"}
        assert lookup_name_for_url("https://corp.example.com/apm", regs) == "corp"

    def test_partial_path_does_not_match(self):
        # The configured URL is /apm but the target is /apm-team — must not match.
        regs = {"corp": "https://corp.example.com/apm"}
        assert lookup_name_for_url("https://corp.example.com/apm-team", regs) is None

    def test_empty_inputs(self):
        assert lookup_name_for_url("", {"corp": "https://corp.example.com"}) is None
        assert lookup_name_for_url("https://corp.example.com", {}) is None


class TestResolveForUrl:
    """End-to-end auth resolution: URL -> name -> token."""

    def test_returns_token_when_url_matches(self, monkeypatch):
        monkeypatch.setenv("APM_REGISTRY_TOKEN_CORP", "tok-abc")
        regs = {"corp": "https://corp.example.com/apm"}
        ctx = resolve_for_url(
            "https://corp.example.com/apm/v1/packages/x/y/versions/1.0.0/tarball", regs
        )
        assert ctx.registry_name == "corp"
        assert ctx.token == "tok-abc"

    def test_url_match_without_env_returns_anonymous(self, monkeypatch):
        monkeypatch.delenv("APM_REGISTRY_TOKEN_CORP", raising=False)
        regs = {"corp": "https://corp.example.com/apm"}
        ctx = resolve_for_url("https://corp.example.com/apm/foo", regs)
        assert ctx.registry_name == "corp"
        assert ctx.token is None

    def test_no_url_match_returns_anonymous(self):
        ctx = resolve_for_url("https://other.example.com/foo", {"corp": "https://corp/apm"})
        assert ctx.registry_name is None
        assert ctx.token is None


class TestRemediationMessage:
    def test_includes_url_and_env_var_hint(self):
        msg = remediation_message("https://corp.example.com/apm")
        assert "https://corp.example.com/apm" in msg
        assert "APM_REGISTRY_TOKEN_<NAME>" in msg
