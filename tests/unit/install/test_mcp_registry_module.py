"""Unit tests for ``apm_cli.install.mcp.registry``.

Covers:
- ``resolve_registry_url`` precedence chain and visibility of overrides.
- ``registry_env_override`` save/restore semantics, including the
  exception-safety path that protects against env-var leakage between
  sequential ``apm install`` invocations in the same shell.
- ``validate_registry_url`` allowlist / length / scheme behaviour.
"""

from unittest.mock import MagicMock

import pytest

from apm_cli.install.mcp.registry import (
    registry_env_override,
    resolve_registry_url,
    validate_registry_url,
)


class TestResolveRegistryUrl:
    """Precedence chain and diagnostic emission."""

    def test_returns_default_when_neither_flag_nor_env(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        url, source = resolve_registry_url(None)
        assert url is None
        assert source == "default"

    def test_returns_flag_when_only_flag(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        url, source = resolve_registry_url("https://flag.example.com")
        assert url == "https://flag.example.com"
        assert source == "flag"

    def test_returns_env_when_only_env(self, monkeypatch):
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://env.example.com")
        url, source = resolve_registry_url(None)
        assert url == "https://env.example.com"
        assert source == "env"

    def test_flag_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://env.example.com")
        url, source = resolve_registry_url("https://flag.example.com")
        assert url == "https://flag.example.com"
        assert source == "flag"

    def test_env_only_emits_visible_diagnostic(self, monkeypatch):
        """B3 regression: silent registry redirect when MCP_REGISTRY_URL is set."""
        from urllib.parse import urlparse
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://poisoned.example.com")
        logger = MagicMock()
        resolve_registry_url(None, logger=logger)
        assert logger.progress.called
        msg = logger.progress.call_args.args[0]
        # Extract the URL token and compare hostname exactly (urlparse, not substring).
        urls = [tok for tok in msg.split() if "://" in tok]
        assert len(urls) == 1
        assert urlparse(urls[0]).hostname == "poisoned.example.com"
        assert "MCP_REGISTRY_URL" in msg

    def test_flag_overrides_env_emits_diagnostic(self, monkeypatch):
        from urllib.parse import urlparse
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://env.example.com")
        logger = MagicMock()
        resolve_registry_url("https://flag.example.com", logger=logger)
        assert logger.progress.called
        msg = logger.progress.call_args.args[0]
        assert "overrides MCP_REGISTRY_URL" in msg
        urls = [urlparse(tok.strip("()")).hostname for tok in msg.split() if "://" in tok]
        # Hostname-set equality avoids substring matching that CodeQL flags.
        # Diagnostic mentions only the overridden env URL, not the flag value.
        assert set(urls) == {"env.example.com"}

    def test_default_path_silent(self, monkeypatch):
        """Defaults are quiet; no diagnostic when neither source is set."""
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        logger = MagicMock()
        resolve_registry_url(None, logger=logger)
        logger.progress.assert_not_called()

    def test_empty_env_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("MCP_REGISTRY_URL", "   ")
        url, source = resolve_registry_url(None)
        assert url is None
        assert source == "default"


class TestRegistryEnvOverride:
    """Exception-safety for the env-export context manager."""

    def test_sets_env_during_context(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        monkeypatch.delenv("MCP_REGISTRY_ALLOW_HTTP", raising=False)
        import os
        with registry_env_override("https://x.example.com"):
            assert os.environ.get("MCP_REGISTRY_URL") == "https://x.example.com"

    def test_clears_env_on_normal_exit(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        import os
        with registry_env_override("https://x.example.com"):
            pass
        assert "MCP_REGISTRY_URL" not in os.environ

    def test_restores_env_on_exception(self, monkeypatch):
        """Critical: env must be restored even when caller raises."""
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        import os
        with pytest.raises(RuntimeError):
            with registry_env_override("https://x.example.com"):
                raise RuntimeError("boom")
        assert "MCP_REGISTRY_URL" not in os.environ

    def test_restores_prior_env_value(self, monkeypatch):
        """If MCP_REGISTRY_URL was set before, restore the original value."""
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://prior.example.com")
        import os
        with registry_env_override("https://override.example.com"):
            assert os.environ.get("MCP_REGISTRY_URL") == "https://override.example.com"
        assert os.environ.get("MCP_REGISTRY_URL") == "https://prior.example.com"

    def test_restores_prior_env_on_exception(self, monkeypatch):
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://prior.example.com")
        import os
        with pytest.raises(ValueError):
            with registry_env_override("https://override.example.com"):
                raise ValueError("boom")
        assert os.environ.get("MCP_REGISTRY_URL") == "https://prior.example.com"

    def test_http_url_sets_allow_http(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_ALLOW_HTTP", raising=False)
        import os
        with registry_env_override("http://intranet.example.com"):
            assert os.environ.get("MCP_REGISTRY_ALLOW_HTTP") == "1"
        assert "MCP_REGISTRY_ALLOW_HTTP" not in os.environ

    def test_none_is_no_op(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        import os
        with registry_env_override(None):
            assert "MCP_REGISTRY_URL" not in os.environ
        assert "MCP_REGISTRY_URL" not in os.environ


class TestValidateRegistryUrl:
    """URL allowlist + length + scheme + host invariants."""

    def test_https_accepted(self):
        validate_registry_url("https://mcp.example.com")

    def test_http_accepted(self):
        validate_registry_url("http://intranet.example.com")

    def test_schemeless_rejected(self):
        with pytest.raises(Exception):
            validate_registry_url("example.com")

    def test_ws_scheme_rejected(self):
        with pytest.raises(Exception):
            validate_registry_url("ws://example.com")

    def test_file_scheme_rejected(self):
        with pytest.raises(Exception):
            validate_registry_url("file:///etc/passwd")

    def test_javascript_scheme_rejected(self):
        with pytest.raises(Exception):
            validate_registry_url("javascript:alert(1)")

    def test_overlong_url_rejected(self):
        url = "https://example.com/" + ("a" * 2050)
        with pytest.raises(Exception):
            validate_registry_url(url)

    def test_empty_rejected(self):
        with pytest.raises(Exception):
            validate_registry_url("")

    def test_credentials_redacted_in_invalid_url_message(self):
        """UsageError text for an unparseable URL must not echo credentials."""
        import click
        with pytest.raises(click.UsageError) as exc_info:
            validate_registry_url("nothttp://user:topsecret@example.com")
        msg = str(exc_info.value.message)
        assert "topsecret" not in msg
        assert "user:" not in msg

    def test_credentials_redacted_in_unsupported_scheme_message(self):
        """UsageError text for an unsupported scheme must not echo credentials."""
        import click
        with pytest.raises(click.UsageError) as exc_info:
            validate_registry_url("ws://user:topsecret@example.com")
        msg = str(exc_info.value.message)
        assert "topsecret" not in msg
        assert "user:" not in msg


class TestValidateMcpDryRunEntrySignature:
    """Public-API contract: explicit typed kwargs, no silent **kwargs."""

    def test_unknown_kwarg_raises_type_error(self):
        from apm_cli.install.mcp.registry import validate_mcp_dry_run_entry
        with pytest.raises(TypeError):
            validate_mcp_dry_run_entry("srv", bogus_kwarg="x")

    def test_accepts_documented_kwargs(self):
        from apm_cli.install.mcp.registry import validate_mcp_dry_run_entry
        # Should not raise -- bare-string registry shorthand is valid.
        validate_mcp_dry_run_entry("srv")


class TestRedactUrlCredentials:
    """U3 regression: never echo URL credentials in logger output."""

    def test_strips_user_password(self):
        from apm_cli.install.mcp.registry import _redact_url_credentials
        from urllib.parse import urlparse
        out = _redact_url_credentials("https://user:secret@registry.example.com/v0")
        assert "secret" not in out
        assert "user" not in out
        parsed = urlparse(out)
        assert parsed.hostname == "registry.example.com"

    def test_keeps_port(self):
        from apm_cli.install.mcp.registry import _redact_url_credentials
        from urllib.parse import urlparse
        out = _redact_url_credentials("https://u:p@registry.example.com:8443/x")
        parsed = urlparse(out)
        assert parsed.hostname == "registry.example.com"
        assert parsed.port == 8443
        assert "p" not in (parsed.password or "")

    def test_no_creds_passthrough(self):
        from apm_cli.install.mcp.registry import _redact_url_credentials
        url = "https://registry.example.com/v0"
        assert _redact_url_credentials(url) == url

    def test_diagnostic_does_not_leak_credentials(self, monkeypatch):
        """End-to-end: B3 env-source diagnostic must redact creds."""
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://u:topsecret@x.example.com/")
        logger = MagicMock()
        resolve_registry_url(None, logger=logger)
        msg = logger.progress.call_args.args[0]
        assert "topsecret" not in msg
        assert "u:" not in msg


class TestSsrfWarning:
    """U2 regression: warn (not block) on loopback / link-local / RFC1918 hosts."""

    @pytest.mark.parametrize("url,host", [
        ("http://localhost:22", "localhost"),
        ("http://127.0.0.1/x", "127.0.0.1"),
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
        ("http://10.0.0.5/", "10.0.0.5"),
        ("http://192.168.1.1/", "192.168.1.1"),
        ("http://172.16.0.1/", "172.16.0.1"),
    ])
    def test_warns_on_local_or_metadata_host(self, monkeypatch, url, host):
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        logger = MagicMock()
        resolve_registry_url(url, logger=logger)
        assert logger.warning.called, f"expected warning for {host}"
        msg = logger.warning.call_args.args[0]
        assert host in msg

    def test_no_warn_for_public_host(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        logger = MagicMock()
        resolve_registry_url("https://api.mcp.github.com", logger=logger)
        logger.warning.assert_not_called()

    def test_decimal_encoded_loopback_warns(self, monkeypatch):
        """2130706433 == 127.0.0.1 in decimal IP form -- still loopback."""
        monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
        logger = MagicMock()
        resolve_registry_url("http://2130706433/", logger=logger)
        # Note: urlparse keeps '2130706433' as the host string; we coerce
        # via ipaddress.ip_address which parses the integer form correctly.
        assert logger.warning.called


class TestRegistryClientTimeout:
    """C3 regression: registry HTTP calls must pass an explicit timeout
    so a typo'd or unreachable --registry never hangs CI."""

    def test_default_timeout_tuple(self, monkeypatch):
        monkeypatch.delenv("MCP_REGISTRY_CONNECT_TIMEOUT", raising=False)
        monkeypatch.delenv("MCP_REGISTRY_READ_TIMEOUT", raising=False)
        from apm_cli.registry.client import _resolve_timeout
        connect, read = _resolve_timeout()
        assert connect > 0 and connect <= 30
        assert read > 0 and read <= 120

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MCP_REGISTRY_CONNECT_TIMEOUT", "5.5")
        monkeypatch.setenv("MCP_REGISTRY_READ_TIMEOUT", "60")
        from apm_cli.registry.client import _resolve_timeout
        assert _resolve_timeout() == (5.5, 60.0)

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MCP_REGISTRY_CONNECT_TIMEOUT", "not-a-number")
        monkeypatch.setenv("MCP_REGISTRY_READ_TIMEOUT", "-1")
        from apm_cli.registry.client import (
            _DEFAULT_CONNECT_TIMEOUT, _DEFAULT_READ_TIMEOUT, _resolve_timeout,
        )
        assert _resolve_timeout() == (_DEFAULT_CONNECT_TIMEOUT, _DEFAULT_READ_TIMEOUT)

    def test_session_get_called_with_timeout(self, monkeypatch):
        """Every registry HTTP call must pass timeout= to session.get."""
        from apm_cli.registry.client import SimpleRegistryClient
        client = SimpleRegistryClient("https://api.mcp.github.com")
        captured = {}

        def fake_get(url, **kw):
            captured.update(kw)
            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {"servers": [], "metadata": {}}
            return R()

        monkeypatch.setattr(client.session, "get", fake_get)
        client.list_servers()
        assert "timeout" in captured, "session.get must receive timeout kwarg"
        assert captured["timeout"] == client._timeout
