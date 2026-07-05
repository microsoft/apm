"""Shared private-CA HTTPS server harness for the child-runtime/frozen TLS tests.

Reuses the certificate factory (``_mint_ca_and_leaf``) and request handler
(``_OkHandler``) already proven in ``test_tls_custom_ca`` -- single source of
truth for minting a private CA that is present in neither ``certifi`` nor the OS
trust store. Exposes a context manager that boots a loopback HTTPS server whose
leaf is signed by that private CA, so the child-runtime (B1) and frozen-binary
(B2) verifiers can drive real ``requests`` traffic across a genuine trust
boundary.
"""

from __future__ import annotations

import contextlib
import http.server
import ssl
import threading
from pathlib import Path
from types import SimpleNamespace

from .test_tls_custom_ca import _mint_ca_and_leaf, _OkHandler


@contextlib.contextmanager
def private_ca_https_server(dirpath: Path):
    """Yield a running loopback HTTPS server backed by a fresh private CA.

    Yields a namespace with ``url`` (https://localhost:<port>/), ``ca_path``
    (PEM of the private CA), and ``ca_pem``/``srv_pem``/``srv_key`` paths.
    """
    ca_pem, srv_pem, srv_key = _mint_ca_and_leaf(dirpath)

    # A prior test may have left truststore globally injected; a truststore-backed
    # server-side SSLContext raises on wrap_socket. Extract first (best-effort) so
    # the server always presents its chain via the stdlib ssl backend.
    try:
        import truststore

        truststore.extract_from_ssl()
    except Exception:
        pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
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
            srv_pem=srv_pem,
            srv_key=srv_key,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
