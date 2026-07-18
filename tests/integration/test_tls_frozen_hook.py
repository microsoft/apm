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
import shutil
import ssl
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import certifi
import pytest

from apm_cli.core.tls_trust import configure_tls_trust

pytestmark = pytest.mark.integration

_TRUST_ENV_VARS = (
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "APM_DISABLE_TRUSTSTORE",
    "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT",
)
_HOOK_PATH = Path(__file__).resolve().parents[2] / "build" / "hooks" / "runtime_hook_ssl_certs.py"

_BUNDLED_CERT_MARKER = "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT"

_truststore_missing = importlib.util.find_spec("truststore") is None
_requires_truststore = pytest.mark.skipif(
    _truststore_missing, reason="truststore not importable in this environment"
)
_requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl CLI not available"
)

# Child that bootstraps trust in its own process then does one real HTTPS GET.
# Reports the return value, whether SSL_CERT_FILE survived the bootstrap, and a
# single RESULT token -- so the parent can assert both mechanism and outcome
# without any global ssl mutation bleeding across tests.
_B2_CHILD = (
    "import os\n"
    "import sys\n"
    "import ssl\n"
    "import requests\n"
    "from apm_cli.core.tls_trust import configure_tls_trust\n"
    "ret = configure_tls_trust()\n"
    "print('RET:' + str(ret))\n"
    "print('SSL_CERT_FILE_PRESENT:' + str('SSL_CERT_FILE' in os.environ))\n"
    "try:\n"
    "    r = requests.get(sys.argv[1], timeout=5)\n"
    "    print('RESULT:OK' if r.status_code == 200 else 'RESULT:BAD')\n"
    "except (ssl.SSLError, requests.exceptions.SSLError):\n"
    "    print('RESULT:SSLERROR')\n"
)


def _install_fake_truststore(monkeypatch, inject=None):
    """Put a no-op (or raising) fake ``truststore`` in sys.modules for mechanism tests."""
    module = types.ModuleType("truststore")
    module.inject_into_ssl = inject or (lambda: None)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "truststore", module)


def _clean_child_env() -> dict[str, str]:
    """os.environ copy with every trust-related var stripped."""
    return {k: v for k, v in os.environ.items() if k not in _TRUST_ENV_VARS}


def _parse_tokens(stdout: str) -> dict[str, str]:
    """Split the B2 child's ``KEY:VALUE`` lines into a dict."""
    tokens: dict[str, str] = {}
    for line in stdout.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            tokens[key.strip()] = value.strip()
    return tokens


def _bundle_with_private_ca(tmp_path, ca_pem: Path) -> Path:
    """A CA bundle that is certifi + the private CA (a superset 'bundled default')."""
    bundle = tmp_path / "bundle_with_private_ca.pem"
    bundle.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + ca_pem.read_bytes())
    return bundle


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


@_requires_truststore
def test_user_override_still_wins_under_frozen_hook(monkeypatch):
    # A user-pinned REQUESTS_CA_BUNDLE makes the hook leave SSL_CERT_FILE unset,
    # and we must honour that bundle rather than inject the OS store.
    import truststore

    truststore.inject_into_ssl()
    assert "truststore" in ssl.SSLContext.__module__
    inject = Mock()
    monkeypatch.setattr(truststore, "inject_into_ssl", inject)

    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    sys.frozen = True
    _run_runtime_hook()

    assert os.environ.get("SSL_CERT_FILE") is None
    assert configure_tls_trust() is False
    inject.assert_not_called()


# ---------------------------------------------------------------------------
# B2 -- frozen binary uses the OS store; certifi only as a genuine fallback.
# These SIMULATE the frozen state (no binary is built). The bundled-default
# SSL_CERT_FILE (marked by the runtime hook) must be neutralized before
# injection so the true system store is consulted; a genuine user override
# must be left intact; and an injection failure must restore certifi so a
# minimal container never ends up with zero trust.
# ---------------------------------------------------------------------------


def test_bundled_default_marker_gates_ssl_cert_file_pop(monkeypatch):
    """B2 mechanism: the bundled-default SSL_CERT_FILE is popped, a user value is kept.

    Platform-independent proof (fake truststore -> no global ssl mutation) that
    the pop is gated strictly on the runtime-hook marker: OUR bundled default is
    removed before injection, a real user SSL_CERT_FILE is preserved.
    """
    _install_fake_truststore(monkeypatch)

    marked = {"SSL_CERT_FILE": "/bundled/certifi.pem", _BUNDLED_CERT_MARKER: "1"}
    assert configure_tls_trust(env=marked) is True
    assert "SSL_CERT_FILE" not in marked, "bundled default must be popped before injection"
    assert _BUNDLED_CERT_MARKER not in marked, "marker must not leak to children"

    user = {"SSL_CERT_FILE": "/etc/ssl/user-ca.pem"}
    assert configure_tls_trust(env=user) is True
    assert user["SSL_CERT_FILE"] == "/etc/ssl/user-ca.pem", "user value must be preserved"


@_requires_truststore
@_requires_openssl
def test_bundled_default_neutralized_system_store_wins(tmp_path):
    """B2 Test A: with the bundled-default marker, the private CA is NOT trusted.

    SSL_CERT_FILE points at a bundle that CONTAINS the private CA and the marker
    is set. Because configure_tls_trust must pop that bundled default before
    injecting, verification falls to the real system trust (which lacks the
    private CA), so the request fails. If the B2 bug persisted and the bundled
    SSL_CERT_FILE were honored, the request would SUCCEED -- so SSLERROR proves
    the bundled certifi is no longer the trust source. Run in a child process to
    avoid global-state bleed.
    """
    from ._tls_ca_server import private_ca_https_server

    with private_ca_https_server(tmp_path) as server:
        bundle = _bundle_with_private_ca(tmp_path, server.ca_pem)
        env = _clean_child_env()
        env["SSL_CERT_FILE"] = str(bundle)
        env[_BUNDLED_CERT_MARKER] = "1"

        result = subprocess.run(
            [sys.executable, "-c", _B2_CHILD, server.url],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        tokens = _parse_tokens(result.stdout)
        assert tokens.get("RET") == "True", result.stdout
        assert tokens.get("SSL_CERT_FILE_PRESENT") == "False", (
            f"bundled default must be popped, got {result.stdout!r}"
        )
        assert tokens.get("RESULT") == "SSLERROR", (
            f"private CA must not be trusted after neutralization, got {result.stdout!r}"
        )


@_requires_truststore
@_requires_openssl
def test_genuine_user_override_still_honored(tmp_path):
    """B2 Test B: a genuine user CA override (no marker) is honored -> request succeeds.

    Same private-CA bundle, delivered as a real user value WITHOUT the
    bundled-default marker. Only OUR bundled default is ever neutralized; user
    intent must survive. Delivered via the channel the platform's truststore
    backend honors (REQUESTS_CA_BUNDLE on macOS, SSL_CERT_FILE on Linux). Run in
    a child process to avoid global-state bleed.
    """
    from ._tls_ca_server import private_ca_https_server

    with private_ca_https_server(tmp_path) as server:
        bundle = _bundle_with_private_ca(tmp_path, server.ca_pem)
        env = _clean_child_env()
        if sys.platform == "darwin":
            env["REQUESTS_CA_BUNDLE"] = str(bundle)
        else:
            env["SSL_CERT_FILE"] = str(bundle)

        result = subprocess.run(
            [sys.executable, "-c", _B2_CHILD, server.url],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        tokens = _parse_tokens(result.stdout)
        assert tokens.get("RESULT") == "OK", (
            f"genuine user override must be honored, got {result.stdout!r}"
        )


def test_injection_failure_restores_bundled_certifi(monkeypatch):
    """B2 Test C: an inject failure returns False and restores the bundled certifi path.

    Minimal-container safety: never end with zero trust. With the bundled-default
    marker set and inject_into_ssl raising, configure_tls_trust must restore
    SSL_CERT_FILE to the captured bundled value and clear the marker.
    """

    def _boom():
        raise RuntimeError("platform trust API unavailable")

    _install_fake_truststore(monkeypatch, inject=_boom)
    bundled = certifi.where()
    os.environ["SSL_CERT_FILE"] = bundled
    os.environ[_BUNDLED_CERT_MARKER] = "1"

    assert configure_tls_trust() is False
    assert os.environ.get("SSL_CERT_FILE") == bundled, "certifi fallback must be restored"
    assert _BUNDLED_CERT_MARKER not in os.environ, "marker must be cleared"
