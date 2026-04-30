"""Tests for marketplace error hierarchy."""

import pytest  # noqa: F401

from apm_cli.marketplace.errors import (
    MarketplaceError,
    MarketplaceFetchError,
    MarketplaceNotFoundError,
    PluginNotFoundError,
)


class TestMarketplaceErrors:
    """Error messages are actionable and include next-step commands."""

    def test_hierarchy(self):
        assert issubclass(MarketplaceNotFoundError, MarketplaceError)
        assert issubclass(PluginNotFoundError, MarketplaceError)
        assert issubclass(MarketplaceFetchError, MarketplaceError)
        assert issubclass(MarketplaceError, Exception)

    def test_not_found_message(self):
        err = MarketplaceNotFoundError("acme")
        assert "acme" in str(err)
        assert "apm marketplace add" in str(err)
        assert err.name == "acme"

    def test_plugin_not_found_message(self):
        err = PluginNotFoundError("my-plugin", "acme")
        assert "my-plugin" in str(err)
        assert "acme" in str(err)
        assert "apm marketplace browse" in str(err)
        assert err.plugin_name == "my-plugin"
        assert err.marketplace_name == "acme"

    def test_fetch_error_message(self):
        err = MarketplaceFetchError("acme", "timeout")
        assert "acme" in str(err)
        assert "timeout" in str(err)
        assert "apm marketplace update" in str(err)
        assert err.name == "acme"
        assert err.reason == "timeout"

    def test_fetch_error_no_reason(self):
        err = MarketplaceFetchError("acme")
        assert "acme" in str(err)
        assert "apm marketplace update" in str(err)
