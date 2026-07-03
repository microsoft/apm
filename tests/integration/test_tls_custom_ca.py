"""Integration tests for TLS verification against a custom CA.

Spins up a loopback HTTPS server whose leaf certificate is signed by a
freshly generated private CA (never present in any trust store), then
exercises the real ``requests`` -> ``urllib3`` -> ``ssl`` stack that APM uses
for the Contents API. This is the end-to-end counterpart to the unit tests in
``tests/unit/core/test_tls_trust.py`` and covers the behaviour the triage
panel asked for on #2004:

- an untrusted custom CA is genuinely rejected (verification is on),
- an explicit ``REQUESTS_CA_BUNDLE`` is honoured and makes the request pass
  (and ``configure_tls_trust`` correctly declines to override it),
- injecting the OS trust store via truststore does NOT weaken verification --
  a CA that is not in the OS store is still rejected.

Requires the ``openssl`` CLI to mint the certificates; skipped where absent.
"""

from __future__ import annotations

import http.server
import shutil
import ssl
import subprocess
import threading
from types import SimpleNamespace

import pytest
import requests

from apm_cli.core.tls_trust import configure_tls_trust

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not available"),
]

_TRUST_ENV_VARS = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "APM_DISABLE_TRUSTSTORE")

_CA_CNF = """\
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = APM Test Root CA
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
"""

_SERVER_CNF = """\
[req]
distinguished_name = dn
req_extensions = v3_req
prompt = no
[dn]
CN = localhost
[v3_req]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost, IP:127.0.0.1
"""


def _openssl(*args) -> None:
    subprocess.run(["openssl", *[str(a) for a in args]], check=True, capture_output=True)


def _mint_ca_and_leaf(dirpath):
    """Generate a private CA and a localhost leaf cert signed by it."""
    ca_key, ca_pem = dirpath / "ca.key", dirpath / "ca.pem"
    srv_key, srv_csr, srv_pem = dirpath / "server.key", dirpath / "server.csr", dirpath / "server.pem"
    ca_cnf, srv_cnf = dirpath / "ca.cnf", dirpath / "server.cnf"
    ca_cnf.write_text(_CA_CNF)
    srv_cnf.write_text(_SERVER_CNF)

    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", ca_key, "-out", ca_pem, "-days", "2", "-config", ca_cnf)
    _openssl("req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", srv_key, "-out", srv_csr, "-config", srv_cnf)
    _openssl("x509", "-req", "-in", srv_csr, "-CA", ca_pem, "-CAkey", ca_key,
             "-CAcreateserial", "-out", srv_pem, "-days", "2",
             "-extfile", srv_cnf, "-extensions", "v3_req")
    return ca_pem, srv_pem, srv_key


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # stdlib handler contract
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):  # silence per-request stderr logging
        pass


@pytest.fixture(scope="module")
def custom_ca_server(tmp_path_factory):
    """A loopback HTTPS server presenting a leaf signed by a private CA."""
    dirpath = tmp_path_factory.mktemp("tls_custom_ca")
    ca_pem, srv_pem, srv_key = _mint_ca_and_leaf(dirpath)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(srv_pem), keyfile=str(srv_key))

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _OkHandler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"https://localhost:{port}/", ca_path=str(ca_pem))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _isolate_trust(monkeypatch):
    """Pristine trust env per test, and undo any global ssl/truststore mutation."""
    for var in _TRUST_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    original_ssl_context = ssl.SSLContext
    try:
        yield
    finally:
        try:
            import truststore

            truststore.extract_from_ssl()
        except Exception:
            pass
        ssl.SSLContext = original_ssl_context


def test_untrusted_custom_ca_is_rejected(custom_ca_server):
    # Default trust (certifi) must reject a cert signed by an unknown CA.
    with pytest.raises(requests.exceptions.SSLError):
        requests.get(custom_ca_server.url, timeout=5)


def test_verify_with_ca_path_succeeds(custom_ca_server):
    # Sanity: the minted chain is valid when the CA is explicitly trusted.
    resp = requests.get(custom_ca_server.url, verify=custom_ca_server.ca_path, timeout=5)
    assert resp.status_code == 200
    assert resp.text == "ok"


@pytest.mark.parametrize("env_var", ["REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
def test_explicit_ca_bundle_env_is_honored(custom_ca_server, monkeypatch, env_var):
    # Both env vars requests consults must win end-to-end.
    monkeypatch.setenv(env_var, custom_ca_server.ca_path)

    # An explicit bundle must win: we skip truststore injection...
    assert configure_tls_trust() is False
    # ...and requests verifies against it, so the request succeeds.
    resp = requests.get(custom_ca_server.url, timeout=5)
    assert resp.status_code == 200


def test_truststore_injection_keeps_verification_on(custom_ca_server):
    # With no explicit bundle, we inject the OS trust store.
    assert configure_tls_trust() is True
    # The private CA is in no OS store, so verification must still fail --
    # injection routes trust to the OS store, it does not disable it.
    with pytest.raises(requests.exceptions.SSLError):
        requests.get(custom_ca_server.url, timeout=5)
