"""Tests for the PyInstaller SSL certificate runtime hook.

The hook lives at ``build/hooks/runtime_hook_ssl_certs.py`` and is executed
by PyInstaller before any application code.  These tests exercise the logic
in isolation by importing the private helper directly.
"""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# The runtime hook is not inside a regular Python package, so we import it
# manually from its file path.
def _find_repo_root() -> Path:
    """Walk up from this file until we find pyproject.toml (the repo root)."""
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):  # noqa: RUF005
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Cannot locate repository root (no pyproject.toml found)")


_HOOK_PATH = _find_repo_root() / "build" / "hooks" / "runtime_hook_ssl_certs.py"


def _load_hook_module():
    """Import the runtime hook as a module.

    Executes the module which defines ``_configure_ssl_certs`` *and* calls it
    at module scope.  Tests invoke the function again with controlled env vars
    to exercise each code path independently.
    """
    spec = importlib.util.spec_from_file_location("runtime_hook_ssl_certs", _HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get_configure_fn():
    """Return a fresh reference to ``_configure_ssl_certs`` from the hook."""
    mod = _load_hook_module()
    return mod._configure_ssl_certs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSSLCertRuntimeHook:
    """Tests for _configure_ssl_certs behaviour."""

    def test_hook_file_exists(self):
        """The runtime hook must exist at the expected path."""
        assert _HOOK_PATH.is_file(), f"Missing runtime hook: {_HOOK_PATH}"

    # -- Frozen-mode gating --------------------------------------------------

    def test_noop_when_not_frozen(self, monkeypatch):
        """When ``sys.frozen`` is absent, the hook must not set any env vars."""
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        fn = _get_configure_fn()
        fn()

        assert "SSL_CERT_FILE" not in os.environ

    # -- User-override respect -----------------------------------------------

    def test_respects_existing_ssl_cert_file(self, monkeypatch):
        """If the user already set SSL_CERT_FILE, do not overwrite it."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setenv("SSL_CERT_FILE", "/custom/ca.pem")
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        fn = _get_configure_fn()
        fn()

        assert os.environ["SSL_CERT_FILE"] == "/custom/ca.pem"

    def test_respects_existing_requests_ca_bundle(self, monkeypatch):
        """If the user already set REQUESTS_CA_BUNDLE, do not set SSL_CERT_FILE."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/custom/bundle.pem")

        fn = _get_configure_fn()
        fn()

        assert "SSL_CERT_FILE" not in os.environ

    # -- Happy path: frozen + certifi available ------------------------------

    def test_sets_ssl_cert_file_when_frozen(self, monkeypatch, tmp_path):
        """In a frozen binary with certifi, SSL_CERT_FILE is set automatically."""
        ca_file = tmp_path / "cacert.pem"
        ca_file.write_text("--- dummy CA bundle ---")

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        mock_certifi = MagicMock()
        mock_certifi.where.return_value = str(ca_file)

        with patch.dict("sys.modules", {"certifi": mock_certifi}):
            fn = _get_configure_fn()
            fn()

        assert os.environ.get("SSL_CERT_FILE") == str(ca_file)

    # -- Fallback: certifi missing -------------------------------------------

    def test_graceful_when_certifi_missing(self, monkeypatch):
        """If certifi is not importable, the hook silently continues."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        with patch.dict("sys.modules", {"certifi": None}):
            fn = _get_configure_fn()
            fn()  # must not raise

        assert "SSL_CERT_FILE" not in os.environ

    # -- Edge case: certifi points at missing file ---------------------------

    def test_skips_when_ca_file_missing(self, monkeypatch, tmp_path):
        """If certifi.where() returns a non-existent path, skip silently."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

        mock_certifi = MagicMock()
        mock_certifi.where.return_value = str(tmp_path / "does_not_exist.pem")

        with patch.dict("sys.modules", {"certifi": mock_certifi}):
            fn = _get_configure_fn()
            fn()

        assert "SSL_CERT_FILE" not in os.environ
