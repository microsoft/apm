"""Loopback HTTPS proxy that records the request headers each host receives.

The installed CLI reaches arbitrary ``https://`` hosts through ``HTTPS_PROXY``.
This proxy terminates every CONNECT tunnel with a leaf certificate minted for
the configured host names by a private CA, answers each request locally (404
by default, or a configured redirect), and records the headers so tests can
assert which origin received which credential. It never contacts real hosts.
"""

from __future__ import annotations

import base64
import contextlib
import socket
import ssl
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.utils.local_git_http_server import _stdlib_server_tls
from tests.utils.local_github_https_fixture import _mint_certificates


@dataclass(frozen=True)
class HeaderObservation:
    """One request answered by the proxy."""

    host: str
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]

    def header_values(self) -> tuple[str, ...]:
        """Return every header value plus decoded Basic credentials."""
        values: list[str] = []
        for _name, value in self.headers:
            values.append(value)
            scheme, _, credential = value.partition(" ")
            if scheme.lower() == "basic" and credential:
                with contextlib.suppress(ValueError, UnicodeDecodeError):
                    values.append(base64.b64decode(credential).decode("utf-8"))
        return tuple(values)


class _CaptureServer(ThreadingHTTPServer):
    daemon_threads = True
    tls_context: ssl.SSLContext
    hosts: frozenset[str]
    redirects: Mapping[str, str]
    observations: list[HeaderObservation]
    observations_lock: threading.Lock


class _OriginHandler(BaseHTTPRequestHandler):
    """Answer one tunnelled origin and record its request headers."""

    server: _CaptureServer

    def __init__(self, request, client_address, server, *, target_host: str) -> None:
        self.target_host = target_host
        super().__init__(request, client_address, server)

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep hermetic test output silent."""

    def _answer(self) -> None:
        with self.server.observations_lock:
            self.server.observations.append(
                HeaderObservation(
                    host=self.target_host,
                    method=self.command,
                    path=self.path,
                    headers=tuple(self.headers.items()),
                )
            )
        location = self.server.redirects.get(self.target_host)
        if location is not None:
            self.send_response(302)
            self.send_header("Location", location)
        else:
            self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_HEAD = do_POST = _answer


class _ProxyHandler(BaseHTTPRequestHandler):
    """Tunnel CONNECT for configured hosts and terminate TLS locally."""

    server: _CaptureServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep hermetic test output silent."""

    def do_CONNECT(self) -> None:
        host = self.path.rsplit(":", 1)[0].lower()
        if host not in self.server.hosts:
            self.send_error(502, "Hermetic fixture does not proxy external hosts")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.wfile.flush()
        try:
            tls = self.server.tls_context.wrap_socket(self.connection, server_side=True)
        except (ssl.SSLError, OSError):
            self.close_connection = True
            return
        try:
            _OriginHandler(tls, self.client_address, self.server, target_host=host)
        finally:
            with contextlib.suppress(OSError):
                tls.shutdown(socket.SHUT_RDWR)
            tls.close()
            self.close_connection = True

    def do_GET(self) -> None:
        self.send_error(502, "Hermetic fixture only tunnels HTTPS")


class LocalHeaderCaptureProxy:
    """Running proxy plus the trust material a child process must use."""

    def __init__(self, server: _CaptureServer, thread: threading.Thread, ca_path: Path) -> None:
        self._server = server
        self._thread = thread
        self.ca_path = ca_path

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def observations(self) -> tuple[HeaderObservation, ...]:
        with self._server.observations_lock:
            return tuple(self._server.observations)

    def hosts_receiving(self, secret: str) -> set[str]:
        """Return every host that received *secret* in any header value."""
        return {
            observation.host
            for observation in self.observations
            if any(secret in value for value in observation.header_values())
        }

    def child_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        """Route a child's HTTPS through the proxy and trust only the fixture CA."""
        child = dict(environment)
        for name in ("ALL_PROXY", "HTTP_PROXY", "all_proxy", "http_proxy", "CURL_CA_BUNDLE"):
            child.pop(name, None)
        child["HTTPS_PROXY"] = self.proxy_url
        child["https_proxy"] = self.proxy_url
        child["NO_PROXY"] = "localhost"
        child["no_proxy"] = "localhost"
        child["REQUESTS_CA_BUNDLE"] = str(self.ca_path)
        child["GIT_SSL_CAINFO"] = str(self.ca_path)
        port = str(self._server.server_address[1])
        ports = [p for p in child.get("APM_TEST_LOOPBACK_PORTS", "").split(",") if p]
        child["APM_TEST_LOOPBACK_PORTS"] = ",".join([*ports, port])
        return child

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextlib.contextmanager
def local_header_capture_proxy(
    directory: Path,
    hosts: tuple[str, ...],
    *,
    redirects: Mapping[str, str] | None = None,
) -> Iterator[LocalHeaderCaptureProxy]:
    """Serve *hosts* over a recording HTTPS proxy; *redirects* maps host -> Location."""
    ca_pem, leaf_pem, leaf_key = _mint_certificates(directory, hosts)
    with _stdlib_server_tls():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(leaf_pem), keyfile=str(leaf_key))
    server = _CaptureServer(("127.0.0.1", 0), _ProxyHandler)
    server.tls_context = context
    server.hosts = frozenset(host.lower() for host in hosts)
    server.redirects = dict(redirects or {})
    server.observations = []
    server.observations_lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy = LocalHeaderCaptureProxy(server, thread, ca_pem)
    try:
        yield proxy
    finally:
        proxy.close()
