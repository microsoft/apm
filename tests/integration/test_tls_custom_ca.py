"""Integration tests for TLS verification against a custom CA.

Spins up a loopback HTTPS server whose leaf certificate is signed by a
freshly generated private CA (never present in any trust store), then
exercises the real ``requests`` -> ``urllib3`` -> ``ssl`` stack that APM uses
for the Contents API. This is the end-to-end counterpart to the unit tests in
``tests/unit/core/test_tls_trust.py`` and covers the additive behaviour from
#2034 as well as the original #2004 trust-store contract:

- an untrusted custom CA is genuinely rejected (verification is on),
- an explicit ``REQUESTS_CA_BUNDLE`` is honoured and makes the request pass
  (and ``configure_tls_trust`` correctly declines to override it),
- injecting the OS trust store via truststore does NOT weaken verification --
  a CA that is not in the OS store is still rejected,
- ``APM_EXTRA_CA_BUNDLE`` trusts a private CA without replacing an independent
  pre-existing root.

Requires the ``openssl`` CLI to mint the certificates; skipped where absent.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from apm_cli.core.tls_trust import _install_additive_ca_context, configure_tls_trust


def _resolve_openssl() -> str | None:
    """Resolve OpenSSL, including Git for Windows when PATH is sanitized."""
    executable = shutil.which("openssl")
    if executable:
        return executable
    if os.name != "nt":
        return None

    candidates: list[Path] = []
    git_executable = shutil.which("git")
    if git_executable:
        git_root = Path(git_executable).resolve().parent.parent
        candidates.append(git_root / "usr" / "bin" / "openssl.exe")
    for variable, suffix in (
        ("ProgramFiles", Path("Git/usr/bin/openssl.exe")),
        ("ProgramFiles(x86)", Path("Git/usr/bin/openssl.exe")),
        ("LOCALAPPDATA", Path("Programs/Git/usr/bin/openssl.exe")),
    ):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / suffix)
    # Managed Windows test processes may omit ProgramFiles from their
    # environment even though the standard Git for Windows install exists.
    candidates.append(Path("C:/Program Files/Git/usr/bin/openssl.exe"))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


_OPENSSL_EXECUTABLE = _resolve_openssl()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_OPENSSL_EXECUTABLE is None, reason="openssl CLI not available"),
]

_TRUST_ENV_VARS = (
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "APM_DISABLE_TRUSTSTORE",
    "APM_EXTRA_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "APM_NODE_EXTRA_CA_CERTS_IS_DERIVED_ADDITIVE",
    "APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE",
    "APM_SSL_CERT_FILE_IS_BUNDLED_DEFAULT",
)

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
    assert _OPENSSL_EXECUTABLE is not None
    subprocess.run([_OPENSSL_EXECUTABLE, *[str(a) for a in args]], check=True, capture_output=True)


def _mint_ca_and_leaf(dirpath, ca_common_name: str = "APM Test Root CA"):
    """Generate a private CA and a localhost leaf cert signed by it."""
    ca_key, ca_pem = dirpath / "ca.key", dirpath / "ca.pem"
    srv_key, srv_csr, srv_pem = (
        dirpath / "server.key",
        dirpath / "server.csr",
        dirpath / "server.pem",
    )
    ca_cnf, srv_cnf = dirpath / "ca.cnf", dirpath / "server.cnf"
    ca_cnf.write_text(_CA_CNF.replace("APM Test Root CA", ca_common_name))
    srv_cnf.write_text(_SERVER_CNF)

    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        ca_key,
        "-out",
        ca_pem,
        "-days",
        "2",
        "-config",
        ca_cnf,
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        srv_key,
        "-out",
        srv_csr,
        "-config",
        srv_cnf,
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        srv_csr,
        "-CA",
        ca_pem,
        "-CAkey",
        ca_key,
        "-CAcreateserial",
        "-out",
        srv_pem,
        "-days",
        "2",
        "-extfile",
        srv_cnf,
        "-extensions",
        "v3_req",
    )
    return ca_pem, srv_pem, srv_key


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # stdlib handler contract
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):  # silence per-request stderr logging
        pass


@contextlib.contextmanager
def _private_ca_server(dirpath: Path, ca_common_name: str = "APM Test Root CA"):
    """Run one private-CA loopback server and yield its trust material."""
    dirpath.mkdir(parents=True, exist_ok=True)
    try:
        import truststore

        truststore.extract_from_ssl()
    except Exception:
        pass
    ca_pem, srv_pem, srv_key = _mint_ca_and_leaf(dirpath, ca_common_name)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(srv_pem), keyfile=str(srv_key))

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _OkHandler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            url=f"https://localhost:{port}/",
            ca_path=str(ca_pem),
            ca_pem=ca_pem,
            port=port,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def custom_ca_server(tmp_path_factory):
    """A loopback HTTPS server presenting a leaf signed by a private CA."""
    dirpath = tmp_path_factory.mktemp("tls_custom_ca")
    with _private_ca_server(dirpath) as server:
        yield server


@pytest.fixture(autouse=True)
def _isolate_trust(monkeypatch):
    """Pristine trust env per test, and undo any global ssl/truststore mutation."""
    import urllib3.util.ssl_ as urllib3_ssl

    for var in _TRUST_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    original_ssl_context = ssl.SSLContext
    original_urllib3_context = urllib3_ssl.SSLContext
    original_preloaded_context = getattr(requests.adapters, "_preloaded_ssl_context", None)
    try:
        yield
    finally:
        try:
            import truststore

            truststore.extract_from_ssl()
        except Exception:
            pass
        ssl.SSLContext = original_ssl_context
        urllib3_ssl.SSLContext = original_urllib3_context
        if hasattr(requests.adapters, "_preloaded_ssl_context"):
            requests.adapters._preloaded_ssl_context = original_preloaded_context


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


def test_apm_extra_ca_bundle_trusts_private_ca(custom_ca_server, monkeypatch):
    """The real parent Requests stack accepts a selected private root."""
    monkeypatch.setenv("APM_EXTRA_CA_BUNDLE", custom_ca_server.ca_path)
    # Avoid permanently extending Requests' module-level preloaded context;
    # this test exercises the published ssl/urllib3 context class instead.
    monkeypatch.setattr(requests.adapters, "_preloaded_ssl_context", None, raising=False)

    assert configure_tls_trust() is True
    response = requests.get(custom_ca_server.url, timeout=5)

    assert response.status_code == 200
    assert response.text == "ok"


def test_apm_extra_ca_bundle_updates_requests_preloaded_context(custom_ca_server, monkeypatch):
    """Requests 2.32's successful preloaded-context path receives the extra CA."""
    original = ssl.create_default_context()
    monkeypatch.setattr(requests.adapters, "_preloaded_ssl_context", original, raising=False)
    monkeypatch.setenv("APM_EXTRA_CA_BUNDLE", custom_ca_server.ca_path)

    assert configure_tls_trust() is True
    published = requests.adapters._preloaded_ssl_context

    assert published is not original
    assert published.check_hostname is True
    assert published.verify_mode == ssl.CERT_REQUIRED
    _assert_tls_handshake(custom_ca_server.port, published)


def test_apm_run_propagates_extra_ca_to_real_child(custom_ca_server, tmp_path):
    """The real CLI-to-runner boundary gives a shell child additive trust."""
    project = tmp_path / "apm-run-project"
    project.mkdir()
    interpreter = Path(sys.executable).as_posix()
    (project / "tls_probe.py").write_text(
        "import sys, requests\n"
        "response = requests.get(sys.argv[1], timeout=5)\n"
        "print(response.text)\n",
        encoding="ascii",
    )
    (project / "apm.yml").write_text(
        "name: tls-probe\n"
        'version: "0.1.0"\n'
        "scripts:\n"
        "  tls-probe: >-\n"
        f'    "{interpreter}" tls_probe.py "{custom_ca_server.url}"\n',
        encoding="ascii",
    )
    env = {key: value for key, value in os.environ.items() if key not in _TRUST_ENV_VARS}
    env.update(
        {
            "APM_E2E_TESTS": "1",
            "APM_EXTRA_CA_BUNDLE": custom_ca_server.ca_path,
            "NO_PROXY": "localhost,127.0.0.1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "apm_cli.cli", "run", "tls-probe"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout.splitlines()


def test_invalid_extra_ca_fails_before_real_cli_command(tmp_path):
    """A fresh CLI reports one ASCII-safe error before executing the script."""
    project = tmp_path / "invalid-ca-project"
    project.mkdir()
    sentinel = project / "command-ran.txt"
    (project / "should_not_run.py").write_text(
        "from pathlib import Path\nPath('command-ran.txt').write_text('ran')\n",
        encoding="ascii",
    )
    (project / "apm.yml").write_text(
        'name: invalid-ca-probe\nversion: "0.1.0"\nscripts:\n  blocked: python should_not_run.py\n',
        encoding="ascii",
    )
    invalid = project / f"missing-{chr(9731)}.pem"
    env = {key: value for key, value in os.environ.items() if key not in _TRUST_ENV_VARS}
    env.update(
        {
            "APM_E2E_TESTS": "1",
            "APM_EXTRA_CA_BUNDLE": str(invalid),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "apm_cli.cli", "run", "blocked"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.count("APM_EXTRA_CA_BUNDLE") == 1
    assert "path does not exist" in result.stderr
    result.stderr.encode("ascii")
    assert not sentinel.exists()


def test_additive_ca_still_rejects_wrong_server_identity(custom_ca_server, monkeypatch):
    """Trusting the issuer never weakens hostname verification."""
    monkeypatch.setenv("APM_EXTRA_CA_BUNDLE", custom_ca_server.ca_path)
    monkeypatch.setattr(requests.adapters, "_preloaded_ssl_context", None, raising=False)

    assert configure_tls_trust() is True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with socket.create_connection(("127.0.0.1", custom_ca_server.port), timeout=5) as raw_socket:
        with pytest.raises(ssl.SSLCertVerificationError):
            context.wrap_socket(raw_socket, server_hostname="wrong.example")


def test_additive_ca_survives_real_requests_fallback(tmp_path):
    """A fresh process keeps stdlib SSL usable and reaches the CA on fallback."""
    with _private_ca_server(tmp_path / "fallback", "APM Fallback Extra Root") as server:
        probe = """
import json
import os
import ssl
import sys

sys.modules["truststore"] = None
from apm_cli.core.tls_trust import configure_tls_trust

configured = configure_tls_trust()
context = ssl.create_default_context()
import requests

response = requests.get(sys.argv[1], timeout=5)
derived = os.environ["REQUESTS_CA_BUNDLE"]
print(json.dumps({
    "configured": configured,
    "ssl_module": type(context).__module__,
    "check_hostname": context.check_hostname,
    "verify_mode": int(context.verify_mode),
    "response_status": response.status_code,
    "response_text": response.text,
    "derived_absolute": os.path.isabs(derived),
    "marker_matches": (
        os.environ["APM_REQUESTS_CA_BUNDLE_IS_DERIVED_ADDITIVE"] == derived
    ),
}))
"""
        env = {key: value for key, value in os.environ.items() if key not in _TRUST_ENV_VARS}
        env["APM_EXTRA_CA_BUNDLE"] = server.ca_path
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        result = subprocess.run(
            [sys.executable, "-c", probe, server.url],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
        )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence == {
        "configured": False,
        "ssl_module": "ssl",
        "check_hostname": True,
        "verify_mode": int(ssl.CERT_REQUIRED),
        "response_status": 200,
        "response_text": "ok",
        "derived_absolute": True,
        "marker_matches": True,
    }


def _assert_tls_handshake(port: int, context: ssl.SSLContext) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname="localhost"):
            pass


def test_additive_context_retains_independent_existing_root(tmp_path, monkeypatch):
    """Two independent synthetic roots prove the extra CA does not replace trust."""
    import urllib3.util.ssl_ as urllib3_ssl

    with (
        _private_ca_server(tmp_path / "baseline", "APM Synthetic Baseline Root") as baseline,
        _private_ca_server(tmp_path / "extra", "APM Synthetic Extra Root") as extra,
    ):
        stdlib_context = ssl.SSLContext

        class SyntheticDefaultContext(stdlib_context):
            def __init__(self, protocol=None):
                # SSLContext configures PROTOCOL_TLS_CLIENT in __new__; loading
                # this root here models the trust that existed before #2034.
                self.load_verify_locations(cafile=baseline.ca_path)

        # Register restoration before the helper publishes its context class.
        monkeypatch.setattr(ssl, "SSLContext", stdlib_context)
        monkeypatch.setattr(urllib3_ssl, "SSLContext", urllib3_ssl.SSLContext)
        monkeypatch.setattr(requests.adapters, "_preloaded_ssl_context", None, raising=False)

        _install_additive_ca_context(
            SyntheticDefaultContext, Path(extra.ca_path).read_text(encoding="ascii")
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        _assert_tls_handshake(baseline.port, context)
        _assert_tls_handshake(extra.port, context)
