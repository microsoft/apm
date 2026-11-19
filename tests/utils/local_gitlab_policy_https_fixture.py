"""Loopback HTTPS proxy that serves GitLab policy projects to the installed CLI.

The installed CLI reaches ``gitlab.com`` through ``HTTPS_PROXY``. This proxy
terminates each CONNECT tunnel with a leaf certificate minted by a private CA
and answers only the Repository Files ``raw`` endpoint for ``apm-policy.yml``
from an in-memory project table. Every request is recorded so tests can
assert which namespaces were probed. Any other CONNECT target is refused, so
the fixture never contacts real hosts.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from tests.utils.local_git_http_server import _stdlib_server_tls
from tests.utils.local_github_https_fixture import _mint_certificates

GITLAB_HOST = "gitlab.com"
_FILES_PREFIX = "/api/v4/projects/"
_FILES_SUFFIX = "/repository/files/apm-policy.yml/raw"


@dataclass(frozen=True)
class GitLabPolicyRequest:
    """One policy-file request answered (or refused) by the fixture."""

    host: str
    project: str
    status: int


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    tls_context: ssl.SSLContext
    projects: dict[str, str | int]
    requests: list[GitLabPolicyRequest]
    lock: threading.Lock


class _GitLabHandler(BaseHTTPRequestHandler):
    """Answer one TLS tunnel's GitLab Repository Files requests."""

    protocol_version = "HTTP/1.1"
    server: _ProxyServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep hermetic test output silent."""

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        project = ""
        status, body = 404, b'{"message": "404 Not Found"}'
        if path.startswith(_FILES_PREFIX) and path.endswith(_FILES_SUFFIX):
            project = unquote(path[len(_FILES_PREFIX) : -len(_FILES_SUFFIX)])
            with self.server.lock:
                value = self.server.projects.get(project, 404)
            if isinstance(value, str):
                status, body = 200, value.encode("utf-8")
            else:
                status = value
        with self.server.lock:
            self.server.requests.append(GitLabPolicyRequest(GITLAB_HOST, project, status))
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ProxyHandler(BaseHTTPRequestHandler):
    """Accept CONNECT only for gitlab.com and terminate TLS locally."""

    server: _ProxyServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep hermetic test output silent."""

    def do_CONNECT(self) -> None:
        host = self.path.rsplit(":", 1)[0].lower()
        if host != GITLAB_HOST:
            with self.server.lock:
                self.server.requests.append(GitLabPolicyRequest(host, "", 502))
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
            _GitLabHandler(tls, self.client_address, self.server)
        finally:
            with contextlib.suppress(OSError):
                tls.shutdown(socket.SHUT_RDWR)
            tls.close()
            self.close_connection = True

    def do_GET(self) -> None:
        self.send_error(502, "Hermetic fixture only tunnels HTTPS")


class LocalGitLabPolicyFixture:
    """Running proxy, its mutable project table, and the child trust material."""

    def __init__(self, server: _ProxyServer, thread: threading.Thread, ca_path: Path) -> None:
        self._server = server
        self._thread = thread
        self.ca_path = ca_path

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def requests(self) -> tuple[GitLabPolicyRequest, ...]:
        with self._server.lock:
            return tuple(self._server.requests)

    def publish(self, projects: Mapping[str, str | int]) -> None:
        """Replace the served projects (policy YAML text or an HTTP status)."""
        with self._server.lock:
            self._server.projects = dict(projects)
            self._server.requests.clear()

    def child_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        """Route a child's HTTPS through the proxy and trust only the fixture CA."""
        child = dict(environment)
        for name in ("ALL_PROXY", "HTTP_PROXY", "all_proxy", "http_proxy", "CURL_CA_BUNDLE"):
            child.pop(name, None)
        proxy = f"http://127.0.0.1:{self.port}"
        child.update(
            {
                "HTTPS_PROXY": proxy,
                "https_proxy": proxy,
                "NO_PROXY": "localhost",
                "no_proxy": "localhost",
                "REQUESTS_CA_BUNDLE": str(self.ca_path),
            }
        )
        ports = [p for p in child.get("APM_TEST_LOOPBACK_PORTS", "").split(",") if p]
        child["APM_TEST_LOOPBACK_PORTS"] = ",".join([*ports, str(self.port)])
        return child


@contextlib.contextmanager
def local_gitlab_policy_fixture(directory: Path) -> Iterator[LocalGitLabPolicyFixture]:
    """Serve GitLab ``apm-policy.yml`` projects over a loopback MITM proxy."""
    ca_pem, leaf_pem, leaf_key = _mint_certificates(directory, (GITLAB_HOST,))
    with _stdlib_server_tls():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(leaf_pem), keyfile=str(leaf_key))
    server = _ProxyServer(("127.0.0.1", 0), _ProxyHandler)
    server.tls_context = context
    server.projects = {}
    server.requests = []
    server.lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, name="gitlab-policy", daemon=True)
    thread.start()
    fixture = LocalGitLabPolicyFixture(server, thread, ca_pem)
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
