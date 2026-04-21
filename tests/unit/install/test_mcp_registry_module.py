"""Unit tests for ``apm_cli.install.mcp_registry``.

Covers:
- ``resolve_registry_url`` precedence chain and visibility of overrides.
- ``registry_env_override`` save/restore semantics, including the
  exception-safety path that protects against env-var leakage between
  sequential ``apm install`` invocations in the same shell.
- ``validate_registry_url`` allowlist / length / scheme behaviour.
"""

from unittest.mock import MagicMock

import pytest

from apm_cli.install.mcp_registry import (
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
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://poisoned.example.com")
        logger = MagicMock()
        resolve_registry_url(None, logger=logger)
        assert logger.progress.called
        msg = logger.progress.call_args.args[0]
        assert "https://poisoned.example.com" in msg
        assert "MCP_REGISTRY_URL" in msg

    def test_flag_overrides_env_emits_diagnostic(self, monkeypatch):
        monkeypatch.setenv("MCP_REGISTRY_URL", "https://env.example.com")
        logger = MagicMock()
        resolve_registry_url("https://flag.example.com", logger=logger)
        assert logger.progress.called
        msg = logger.progress.call_args.args[0]
        assert "overrides MCP_REGISTRY_URL" in msg
        assert "https://env.example.com" in msg

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
