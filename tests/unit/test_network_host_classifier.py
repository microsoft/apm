"""Behavioral regression tests for utils/network.py canonical host classifier.

Dual-guardrail behavioral test (AC33): these tests encode the exact symptom
that prompted centralizing is_local_or_metadata_host in utils/network.py --
namely that both the MCP registry URL validator and the Codex adapter URL
guard must share one definition and agree on every host classification.

These tests MUST fail if:
- The canonical owner (utils/network.py) is removed or renamed.
- A second definition is introduced in another module.
- The classification logic drifts between call sites.
"""

from __future__ import annotations

import importlib

import pytest

# ---------------------------------------------------------------------------
# Unit: canonical function behaviour
# ---------------------------------------------------------------------------


class TestIsLocalOrMetadataHostCanonical:
    """is_local_or_metadata_host must classify every host consistently."""

    @pytest.fixture(autouse=True)
    def _fn(self) -> None:
        from apm_cli.utils.network import is_local_or_metadata_host

        self.fn = is_local_or_metadata_host

    def test_none_returns_false(self) -> None:
        assert self.fn(None) is False

    def test_empty_string_returns_false(self) -> None:
        assert self.fn("") is False

    def test_localhost_name(self) -> None:
        assert self.fn("localhost") is True

    def test_ip6_localhost_name(self) -> None:
        assert self.fn("ip6-localhost") is True

    def test_ipv4_loopback(self) -> None:
        assert self.fn("127.0.0.1") is True

    def test_ipv6_loopback(self) -> None:
        assert self.fn("::1") is True

    def test_rfc1918_10_block(self) -> None:
        assert self.fn("10.0.0.1") is True

    def test_rfc1918_172_block(self) -> None:
        assert self.fn("172.16.0.1") is True

    def test_rfc1918_192_168_block(self) -> None:
        assert self.fn("192.168.1.1") is True

    def test_link_local(self) -> None:
        assert self.fn("169.254.1.1") is True

    def test_cloud_metadata_ip(self) -> None:
        assert self.fn("169.254.169.254") is True

    def test_public_hostname_false(self) -> None:
        assert self.fn("registry.npmjs.org") is False

    def test_public_ip_false(self) -> None:
        assert self.fn("8.8.8.8") is False

    def test_decimal_encoded_loopback(self) -> None:
        # 2130706433 == 127.0.0.1 in decimal form
        assert self.fn("2130706433") is True


# ---------------------------------------------------------------------------
# Structural regression: single canonical import path
# ---------------------------------------------------------------------------


class TestCanonicalImportPath:
    """Both consumers must import from utils.network, not from each other."""

    def test_codex_imports_from_utils_network(self) -> None:
        """codex.py must import is_local_or_metadata_host from utils.network."""
        import inspect

        from apm_cli.adapters.client import codex as codex_mod

        # The function used inside CodexClientAdapter must be the canonical one
        source = inspect.getsource(codex_mod)
        assert "from ...utils.network import is_local_or_metadata_host" in source
        assert "from ...install.mcp.registry import" not in source

    def test_registry_does_not_define_local_host_fn(self) -> None:
        """install/mcp/registry.py must not define is_local_or_metadata_host."""
        import inspect

        from apm_cli.install.mcp import registry as registry_mod

        source = inspect.getsource(registry_mod)
        assert "def is_local_or_metadata_host(" not in source
        assert "def _is_local_or_metadata_host(" not in source

    def test_canonical_module_is_importable(self) -> None:
        """utils.network must be importable as a standalone module."""
        mod = importlib.import_module("apm_cli.utils.network")
        assert hasattr(mod, "is_local_or_metadata_host")
        assert callable(mod.is_local_or_metadata_host)
