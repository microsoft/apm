"""Unit tests for apm_cli.core.tls_trust.configure_tls_trust.

Covers every branch:
- opt-out via APM_DISABLE_TRUSTSTORE
- explicit CA bundle env vars win (REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE)
- SSL_CERT_FILE does NOT suppress injection (it is set by the frozen runtime hook)
- truststore missing -> graceful certifi fallback
- injection failure -> graceful certifi fallback
- happy path -> inject_into_ssl called exactly once
"""

from __future__ import annotations

import sys
import types

import pytest

from apm_cli.core.tls_trust import (
    _DISABLE_ENV_VAR,
    _EXPLICIT_CA_ENV_VARS,
    configure_tls_trust,
)

_ALL_TRUST_ENV = (_DISABLE_ENV_VAR, "SSL_CERT_FILE", *_EXPLICIT_CA_ENV_VARS)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start each test from a pristine env (no override / opt-out set)."""
    for var in _ALL_TRUST_ENV:
        monkeypatch.delenv(var, raising=False)


def _install_fake_truststore(monkeypatch, inject=None):
    """Put a fake ``truststore`` module in sys.modules and return its inject mock."""
    calls = {"n": 0}

    def _default_inject():
        calls["n"] += 1

    module = types.ModuleType("truststore")
    module.inject_into_ssl = inject or _default_inject  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", module)
    return calls


def test_opt_out_disables_injection(monkeypatch):
    calls = _install_fake_truststore(monkeypatch)
    monkeypatch.setenv(_DISABLE_ENV_VAR, "1")

    assert configure_tls_trust() is False
    assert calls["n"] == 0


@pytest.mark.parametrize("var", _EXPLICIT_CA_ENV_VARS)
def test_explicit_ca_bundle_wins(monkeypatch, var):
    calls = _install_fake_truststore(monkeypatch)
    monkeypatch.setenv(var, "/etc/ssl/certs/custom-ca.pem")

    assert configure_tls_trust() is False
    assert calls["n"] == 0


def test_ssl_cert_file_does_not_suppress_injection(monkeypatch):
    # SSL_CERT_FILE is not a requests CA override and IS set by the frozen-binary
    # runtime hook (to bundled certifi). It must NOT disable OS-trust injection,
    # or the feature becomes a no-op in the shipped artifact.
    calls = _install_fake_truststore(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")

    assert configure_tls_trust() is True
    assert calls["n"] == 1


def test_missing_truststore_falls_back(monkeypatch):
    # A None entry in sys.modules makes ``import truststore`` raise ImportError.
    monkeypatch.setitem(sys.modules, "truststore", None)

    assert configure_tls_trust() is False


def test_injection_failure_falls_back(monkeypatch):
    def _boom():
        raise RuntimeError("platform trust API unavailable")

    _install_fake_truststore(monkeypatch, inject=_boom)

    assert configure_tls_trust() is False


def test_happy_path_injects_once(monkeypatch):
    calls = _install_fake_truststore(monkeypatch)

    assert configure_tls_trust() is True
    assert calls["n"] == 1
