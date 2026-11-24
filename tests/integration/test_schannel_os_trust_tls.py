"""git schannel's trust in process refuses what git would refuse (W3d SC4, SC9).

Windows only: the OS-trust context verifies through the Windows certificate
chain engine. A peer the store does not trust sees no application bytes. A
leaf whose revocation cannot be checked (its CRL distribution point is
unreachable) is refused because the context checks revocation for the chain,
and the same chain is accepted once that check is off (killed control). A
distribution point that answers slowly is waited for, not treated as offline.
The private root is added to the context's own engine for the test only;
the product context adds no anchor.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from apm_cli.deps.anonymous_ref_listing import os_trust_context
from tests.utils.local_github_https_fixture import openssl_executable

pytestmark = [
    pytest.mark.component,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]

if os.name != "nt":
    pytest.skip("the Windows certificate chain engine is Windows only", allow_module_level=True)
if openssl_executable() is None:
    pytest.skip("openssl CLI is required to mint certificates", allow_module_level=True)

_CA = """[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = APM schannel cell root
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
"""

_LEAF = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = {host}
[v3_leaf]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:{host}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
{cdp}
"""

# A CRL distribution point nothing answers: revocation status is unknowable.
_OFFLINE_CDP = "crlDistributionPoints = URI:http://127.0.0.1:9/root.crl"

_CRL_CA = """[ca]
default_ca = ca_default
[ca_default]
database = index.txt
crlnumber = crlnumber
default_md = sha256
default_crl_days = 2
crl_extensions = crl_ext
[crl_ext]
authorityKeyIdentifier = keyid:always
"""

# Slower than a normal CRL fetch, well inside Windows' URL retrieval timeout.
_SLOW_CRL_SECONDS = 3.0


def _openssl(*args: object, cwd: Path | None = None) -> None:
    subprocess.run(
        [openssl_executable(), *map(str, args)],
        check=True,
        capture_output=True,
        timeout=60,
        cwd=cwd,
    )


def _mint(directory: Path, host: str, cdp: str = "") -> tuple[Path, Path, Path]:
    """``(root, leaf, key)`` for *host*, optionally naming a CRL distribution point."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ca.cnf").write_text(_CA, encoding="ascii")
    (directory / "leaf.cnf").write_text(_LEAF.format(host=host, cdp=cdp), encoding="ascii")
    run = _openssl

    root, root_key = directory / "ca.pem", directory / "ca.key"
    leaf, key, csr = directory / "leaf.pem", directory / "leaf.key", directory / "leaf.csr"
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", root_key, "-out", root,
        "-days", "2", "-config", directory / "ca.cnf")  # fmt: skip
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", csr,
        "-config", directory / "leaf.cnf")  # fmt: skip
    run("x509", "-req", "-in", csr, "-CA", root, "-CAkey", root_key, "-CAcreateserial",
        "-out", leaf, "-days", "2", "-extfile", directory / "leaf.cnf",
        "-extensions", "v3_leaf")  # fmt: skip
    return root, leaf, key


def _empty_crl(directory: Path) -> bytes:
    """A DER CRL from the root minted in *directory*, revoking nothing."""
    (directory / "crl.cnf").write_text(_CRL_CA, encoding="ascii")
    (directory / "index.txt").write_text("", encoding="ascii")
    (directory / "crlnumber").write_text("01\n", encoding="ascii")
    pem, der = directory / "root.crl.pem", directory / "root.crl"
    _openssl("ca", "-gencrl", "-config", directory / "crl.cnf", "-keyfile",
             directory / "ca.key", "-cert", directory / "ca.pem", "-out", pem,
             cwd=directory)  # fmt: skip
    _openssl("crl", "-in", pem, "-outform", "DER", "-out", der)
    return der.read_bytes()


@contextlib.contextmanager
def _slow_crl_point(delay: float) -> Iterator[tuple[str, list[bytes], list[float]]]:
    """``(url, crl slot, fetch times)`` of a loopback CRL distribution point."""
    crl: list[bytes] = []
    fetched: list[float] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            time.sleep(delay)
            body = crl[0]
            self.send_response(200)
            self.send_header("Content-Type", "application/pkix-crl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            fetched.append(time.monotonic())

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/root.crl", crl, fetched
    finally:
        server.shutdown()
        server.server_close()


@contextlib.contextmanager
def _tls_peer(leaf: Path, key: Path) -> Iterator[tuple[int, list[bytes]]]:
    """``(port, application bytes received)`` of a local TLS server presenting *leaf*."""
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certfile=str(leaf), keyfile=str(key))
    listener = socket.create_server(("127.0.0.1", 0))
    received: list[bytes] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with contextlib.suppress(OSError), conn:
                conn.settimeout(30)
                with server_context.wrap_socket(conn, server_side=True) as tls:
                    if chunk := tls.recv(4096):
                        received.append(chunk)
                        tls.sendall(b"HTTP/1.1 204 No Content\r\n\r\n")

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield listener.getsockname()[1], received
    finally:
        listener.close()


def _handshake(context: ssl.SSLContext, port: int, host: str = "github.com") -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(b"GET / HTTP/1.1\r\nAuthorization: Basic secret\r\n\r\n")
            # Read the answer so the close is orderly: an abortive close can
            # drop the request from the peer's buffer before it reads it.
            tls.recv(64)


def _assert_peer_saw(received: list[bytes], *, request: bool) -> None:
    """What the peer received: nothing before a refusal, the request after an accept."""
    deadline = time.monotonic() + 5
    while request and not received and time.monotonic() < deadline:
        time.sleep(0.05)
    data = b"".join(received)
    if request:
        assert data.startswith(b"GET / HTTP/1.1"), "the accepted handshake reached the peer"
    else:
        assert data == b"", "a refused handshake sends no application byte"


def _anchored(root: Path, *, revocation: bool = True) -> ssl.SSLContext:
    """The product context plus a test-only anchor in its own chain engine."""
    context = os_trust_context()
    context._ctx.load_verify_locations(cafile=str(root))  # bypasses the product's no-op
    if not revocation:
        context.verify_flags &= ~ssl.VERIFY_CRL_CHECK_CHAIN
    return context


def test_an_untrusted_peer_sees_no_application_bytes(tmp_path: Path) -> None:
    """SC4: a root the Windows store does not hold is refused before any request byte."""
    _root, leaf, key = _mint(tmp_path / "rogue", "github.com")

    with _tls_peer(leaf, key) as (port, received):
        with pytest.raises(ssl.SSLError):
            _handshake(os_trust_context(), port)
        _assert_peer_saw(received, request=False)


def test_a_certificate_for_another_host_is_refused(tmp_path: Path) -> None:
    root, leaf, key = _mint(tmp_path / "wrong-host", "wrong.example.invalid")

    with _tls_peer(leaf, key) as (port, received):
        with pytest.raises(ssl.SSLError):
            _handshake(_anchored(root, revocation=False), port)
        _assert_peer_saw(received, request=False)


def test_unknowable_revocation_is_refused_and_accepted_without_the_check(tmp_path: Path) -> None:
    """SC9: an unreachable CRL distribution point refuses; the killed control accepts."""
    root, leaf, key = _mint(tmp_path / "offline-crl", "github.com", _OFFLINE_CDP)

    with _tls_peer(leaf, key) as (port, received):
        with pytest.raises(ssl.SSLError):
            _handshake(_anchored(root), port)
        _assert_peer_saw(received, request=False)
        _handshake(_anchored(root, revocation=False), port)
        _assert_peer_saw(received, request=True)


def test_a_slow_crl_distribution_point_is_waited_for(tmp_path: Path) -> None:
    """A reachable but slow CRL fetch passes; only an unknowable status refuses."""
    with _slow_crl_point(_SLOW_CRL_SECONDS) as (url, crl, fetched):
        directory = tmp_path / "slow-crl"
        root, leaf, key = _mint(directory, "github.com", f"crlDistributionPoints = URI:{url}")
        crl.append(_empty_crl(directory))

        with _tls_peer(leaf, key) as (port, received):
            started = time.monotonic()
            _handshake(_anchored(root), port)
            _assert_peer_saw(received, request=True)

    assert fetched, "the revocation check fetched the CRL"
    assert fetched[0] - started >= _SLOW_CRL_SECONDS, "the slow answer was waited for"
