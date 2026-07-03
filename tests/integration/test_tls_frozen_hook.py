"""Regression test: the frozen-binary SSL runtime hook must not disable OS-trust injection.

The PyInstaller build wires ``build/hooks/runtime_hook_ssl_certs.py``, which runs
before application code in the frozen binary and sets ``SSL_CERT_FILE`` to the
bundled certifi bundle (to fix the compiled-in OpenSSL cert path, see #428).

``configure_tls_trust()`` must still inject ``truststore`` in that state.
Otherwise, because the hook sets ``SSL_CERT_FILE``, treating that var as an
explicit override would make OS-trust verification a silent no-op in exactly the
shipped artifact this feature exists for. This test drives the *real* hook under
a simulated frozen process so the two cannot drift apart.
"""

from __future__ import annotations

import importlib.util
import os
import ssl
import sys
from pathlib import Path

import certifi
import pytest

from apm_cli.core.tls_trust import configure_tls_trust

pytestmark = pytest.mark.integration

_TRUST_ENV_VARS = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "APM_DISABLE_TRUSTSTORE")
_HOOK_PATH = Path(__file__).resolve().parents[2] / "build" / "hooks" / "runtime_hook_ssl_certs.py"


@pytest.fixture(autouse=True)
def _isolate_trust():
    """Snapshot/restore trust env, sys.frozen, and the global ssl.SSLContext.

    Manual snapshot (not monkeypatch) because the runtime hook mutates
    ``os.environ`` directly, which monkeypatch would not track or undo.
    """
    saved_env = {var: os.environ.get(var) for var in _TRUST_ENV_VARS}
    for var in _TRUST_ENV_VARS:
        os.environ.pop(var, None)
    saved_ctx = ssl.SSLContext
    had_frozen = hasattr(sys, "frozen")
    saved_frozen = getattr(sys, "frozen", None)
    try:
        yield
    finally:
        try:
            import truststore

            truststore.extract_from_ssl()
        except Exception:
            pass
        ssl.SSLContext = saved_ctx
        if had_frozen:
            sys.frozen = saved_frozen
        elif hasattr(sys, "frozen"):
            del sys.frozen
        for var, value in saved_env.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def _run_runtime_hook():
    """Execute the real PyInstaller SSL runtime hook (runs _configure_ssl_certs)."""
    spec = importlib.util.spec_from_file_location("runtime_hook_ssl_certs", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_hook_ssl_cert_file_does_not_disable_injection():
    sys.frozen = True
    _run_runtime_hook()

    # The hook pinned SSL_CERT_FILE to the bundled certifi...
    assert os.environ.get("SSL_CERT_FILE") == certifi.where()
    # ...and that must NOT suppress OS-trust injection in the frozen binary.
    assert configure_tls_trust() is True
    assert "truststore" in ssl.SSLContext.__module__


def test_user_override_still_wins_under_frozen_hook():
    # A user-pinned REQUESTS_CA_BUNDLE makes the hook leave SSL_CERT_FILE unset,
    # and we must honour that bundle rather than inject the OS store.
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    sys.frozen = True
    _run_runtime_hook()

    assert os.environ.get("SSL_CERT_FILE") is None
    assert configure_tls_trust() is False
    assert "truststore" not in ssl.SSLContext.__module__
