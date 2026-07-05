"""B1 verifier: OS-store trust must propagate into a REAL child process.

The parent cannot monkeypatch a child runtime's ``ssl`` module across ``exec``;
``build_child_tls_env`` must carry trust across the process boundary by
prepending the ``sitecustomize`` shim dir to the child ``PYTHONPATH`` so the
child re-runs :func:`configure_tls_trust` at its own interpreter startup.

These are independent end-to-end tests written from the acceptance spec for
PR #2005 -- they spawn genuine child interpreters and prove:

* the shim actually executes in the child (``ssl.SSLContext`` becomes
  truststore-backed) and does so only via the env, not inheritance;
* a real ``requests.get`` against a private-CA HTTPS server succeeds only when
  trust is delivered through the ``build_child_tls_env``-produced env, and fails
  with the identical child under a plain env (the asymmetry is the proof);
* ``APM_DISABLE_TRUSTSTORE`` suppresses the shim entirely.

Platform note: on macOS ``truststore`` verifies against the system keychain and
does not consult ``SSL_CERT_FILE``, so the file-based system default cannot be
honored through truststore there. On macOS the env-delivered trust is therefore
proven via ``REQUESTS_CA_BUNDLE`` carried through the same
``build_child_tls_env``-produced env; on Linux ``SSL_CERT_FILE`` is honored by
truststore's ``set_default_verify_paths()``. Either way the trust reaches the
child solely through the env this feature builds.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

from apm_cli.core.tls_trust import build_child_tls_env

pytestmark = pytest.mark.integration

_TRUST_ENV_VARS = (
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "APM_DISABLE_TRUSTSTORE",
    "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT",
)

_truststore_missing = importlib.util.find_spec("truststore") is None
_requires_truststore = pytest.mark.skipif(
    _truststore_missing, reason="truststore not importable in this environment"
)
_requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl CLI not available"
)

# Child that reports which module owns ssl.SSLContext -- truststore-backed after
# the shim runs, plain "ssl" otherwise.
_SSL_MODULE_PROBE = "import ssl; print(ssl.SSLContext.__module__)"

# Child that performs a real HTTPS GET and prints exactly one RESULT token.
_REQUEST_PROBE = (
    "import sys\n"
    "import ssl\n"
    "import requests\n"
    "try:\n"
    "    r = requests.get(sys.argv[1], timeout=5)\n"
    "    print('RESULT:OK' if r.status_code == 200 else 'RESULT:BAD')\n"
    "except (ssl.SSLError, requests.exceptions.SSLError):\n"
    "    print('RESULT:SSLERROR')\n"
)


@pytest.fixture(autouse=True)
def _isolate_trust():
    """Undo any global truststore/ssl injection so parent state cannot bleed in or out.

    These tests spawn isolated child interpreters, but the in-parent private-CA
    server must be built with the stdlib ssl backend -- a prior test that left
    truststore injected would otherwise break the server's wrap_socket.
    """
    import ssl as _ssl

    saved_ctx = _ssl.SSLContext
    try:
        import truststore

        truststore.extract_from_ssl()
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            import truststore

            truststore.extract_from_ssl()
        except Exception:
            pass
        _ssl.SSLContext = saved_ctx


def _clean_base_env() -> dict[str, str]:
    """os.environ copy with every trust-related var stripped (pristine start)."""
    return {k: v for k, v in os.environ.items() if k not in _TRUST_ENV_VARS}


def _ca_delivery_env(base: dict[str, str], ca_path: str) -> dict[str, str]:
    """Return *base* augmented so an honest child would trust *ca_path*.

    Uses the delivery channel the platform's truststore backend actually
    honors: ``REQUESTS_CA_BUNDLE`` on macOS (keychain backend ignores
    ``SSL_CERT_FILE``), ``SSL_CERT_FILE`` on Linux/other.
    """
    env = dict(base)
    if sys.platform == "darwin":
        env["REQUESTS_CA_BUNDLE"] = ca_path
    else:
        env["SSL_CERT_FILE"] = ca_path
    return env


@_requires_truststore
def test_shim_runs_in_child_via_env_not_inheritance():
    """Test 1: build_child_tls_env makes ssl.SSLContext truststore-backed in the child."""
    base = _clean_base_env()

    with_shim = subprocess.run(
        [sys.executable, "-c", _SSL_MODULE_PROBE],
        env=build_child_tls_env(base),
        capture_output=True,
        text=True,
    )
    assert with_shim.returncode == 0, with_shim.stderr
    assert with_shim.stdout.strip().startswith("truststore"), (
        f"child ssl module should be truststore-backed, got {with_shim.stdout!r}"
    )

    # Control: a plain env (no shim dir on PYTHONPATH) must stay on stdlib ssl,
    # proving the trust crosses the boundary via the env, not process inheritance.
    control = subprocess.run(
        [sys.executable, "-c", _SSL_MODULE_PROBE],
        env=base,
        capture_output=True,
        text=True,
    )
    assert control.returncode == 0, control.stderr
    assert control.stdout.strip() == "ssl", (
        f"control child should use stdlib ssl, got {control.stdout!r}"
    )


@_requires_truststore
@_requires_openssl
def test_real_child_gains_trust_only_through_env(tmp_path):
    """Test 2: a real requests.get in a child succeeds only via the env-delivered trust."""
    from ._tls_ca_server import private_ca_https_server

    with private_ca_https_server(tmp_path) as server:
        base = _clean_base_env()

        # Trust delivered ONLY through the build_child_tls_env-produced env.
        trusted_env = build_child_tls_env(_ca_delivery_env(base, server.ca_path))
        trusted = subprocess.run(
            [sys.executable, "-c", _REQUEST_PROBE, server.url],
            env=trusted_env,
            capture_output=True,
            text=True,
        )
        assert trusted.returncode == 0, trusted.stderr
        # No shim contamination: stdout is exactly the RESULT token, stderr empty.
        assert trusted.stdout.strip() == "RESULT:OK", (
            f"trusted child stdout should be only RESULT:OK, got {trusted.stdout!r}"
        )
        assert trusted.stderr == "", f"shim must not write to child stderr, got {trusted.stderr!r}"

        # Control: identical child, plain env (private CA absent) -> SSL failure.
        control = subprocess.run(
            [sys.executable, "-c", _REQUEST_PROBE, server.url],
            env=base,
            capture_output=True,
            text=True,
        )
        assert control.returncode == 0, control.stderr
        assert control.stdout.strip() == "RESULT:SSLERROR", (
            f"control child should fail verification, got {control.stdout!r}"
        )


@_requires_truststore
def test_disable_flag_suppresses_child_shim():
    """Test 3: APM_DISABLE_TRUSTSTORE keeps the shim out and the child on stdlib ssl."""
    base = _clean_base_env()
    base["APM_DISABLE_TRUSTSTORE"] = "1"

    child_env = build_child_tls_env(base)
    shim_dir = os.path.dirname(importlib.util.find_spec("apm_cli.core.tls_trust").origin)
    # The shim dir must NOT have been prepended to PYTHONPATH.
    assert os.path.join(shim_dir, "_child_tls") not in child_env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-c", _SSL_MODULE_PROBE],
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ssl", (
        f"disabled child should use stdlib ssl, got {result.stdout!r}"
    )
