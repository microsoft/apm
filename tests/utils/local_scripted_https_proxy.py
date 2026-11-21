"""Loopback HTTPS proxy whose origins are answered by per-host Python handlers.

The installed CLI and its git children reach ``https://`` hosts through
``HTTPS_PROXY``. This proxy terminates every CONNECT tunnel with a leaf
certificate minted for the configured host names by a private CA, hands each
request to the handler registered for that host, and records method, path,
headers and body size so tests can assert which origin received which
credential. Unknown hosts are refused, so the fixture never contacts real
hosts.

``git_smart_http`` serves a bare repository through ``git http-backend`` so
real ``git clone`` / ``ls-remote`` traffic can be answered behind an auth gate.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import functools
import gzip
import os
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from tests.utils.local_git_http_server import _stdlib_server_tls
from tests.utils.local_github_https_fixture import _mint_certificates


@dataclass(frozen=True)
class ScriptedRequest:
    """One request received by a scripted origin."""

    host: str
    method: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    status: int = 0

    @property
    def path(self) -> str:
        return urlsplit(self.target).path

    @property
    def query(self) -> str:
        return urlsplit(self.target).query

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None

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


@dataclass
class ScriptedResponse:
    """Response produced by a handler."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


Handler = Callable[[ScriptedRequest], ScriptedResponse]


def basic_authorization(username: str, password: str) -> str:
    """Return the ``Authorization`` value git sends for *username*/*password*."""
    raw = f"{username}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


@functools.cache
def _git_exec_path(git: str) -> str:
    return subprocess.run(
        (git, "--exec-path"), capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()


def git_smart_http(
    repository: Path,
    request: ScriptedRequest,
    service_path: str,
    *,
    real_git: str | None = None,
) -> ScriptedResponse:
    """Answer one smart-HTTP request for bare *repository* via git http-backend.

    *service_path* is the remainder after the repository prefix, for example
    ``/info/refs`` or ``/git-upload-pack``.
    """
    git = real_git or shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is required")
    if service_path not in ("/info/refs", "/git-upload-pack", "/HEAD"):
        return ScriptedResponse(404)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_PROJECT_ROOT": str(repository.parent),
        "GIT_HTTP_EXPORT_ALL": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "PATH_INFO": f"/{repository.name}{service_path}",
        "QUERY_STRING": request.query,
        "REQUEST_METHOD": request.method,
        "CONTENT_TYPE": request.header("Content-Type") or "",
        "REMOTE_ADDR": "127.0.0.1",
        "GIT_EXEC_PATH": _git_exec_path(git),
    }
    body = request.body
    if (request.header("Content-Encoding") or "").lower() == "gzip":
        body = gzip.decompress(body)
    environment["CONTENT_LENGTH"] = str(len(body))
    protocol = request.header("Git-Protocol")
    if protocol:
        environment["GIT_PROTOCOL"] = protocol
    completed = subprocess.run(
        (git, "http-backend"),
        input=body,
        env=environment,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0 and not completed.stdout:
        return ScriptedResponse(500, {"Content-Type": "text/plain"}, completed.stderr)
    head, _, payload = completed.stdout.partition(b"\r\n\r\n")
    if not _:
        head, _, payload = completed.stdout.partition(b"\n\n")
    status = 200
    headers: dict[str, str] = {}
    for line in head.decode("latin-1").splitlines():
        name, _, value = line.partition(":")
        if name.lower() == "status":
            status = int(value.strip().split(" ", 1)[0])
        elif name:
            headers[name.strip()] = value.strip()
    return ScriptedResponse(status, headers, payload)


class _ScriptedServer(ThreadingHTTPServer):
    daemon_threads = True
    tls_context: ssl.SSLContext
    handlers: Mapping[str, Handler]
    observations: list[ScriptedRequest]
    observations_lock: threading.Lock


class _OriginHandler(BaseHTTPRequestHandler):
    """Answer one tunnelled origin through its scripted handler."""

    protocol_version = "HTTP/1.1"
    server: _ScriptedServer

    def __init__(self, request, client_address, server, *, target_host: str) -> None:
        self.target_host = target_host
        super().__init__(request, client_address, server)

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep hermetic test output silent."""

    def _read_body(self) -> bytes:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            chunks: list[bytes] = []
            while True:
                size_line = self.rfile.readline().strip()
                size = int(size_line.split(b";", 1)[0] or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _answer(self) -> None:
        request = ScriptedRequest(
            host=self.target_host,
            method=self.command,
            target=self.path,
            headers=tuple(self.headers.items()),
            body=self._read_body(),
        )
        try:
            response = self.server.handlers[self.target_host](request)
        except Exception as exc:  # pragma: no cover - surfaced to the client
            response = ScriptedResponse(500, {"Content-Type": "text/plain"}, repr(exc).encode())
        with self.server.observations_lock:
            self.server.observations.append(dataclasses.replace(request, status=response.status))
        self.send_response(response.status)
        for name, value in response.headers.items():
            if name.lower() not in ("content-length", "transfer-encoding", "connection"):
                self.send_header(name, value)
        body = b"" if self.command == "HEAD" else response.body
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _answer


class _ProxyHandler(BaseHTTPRequestHandler):
    """Tunnel CONNECT for configured hosts and terminate TLS locally."""

    server: _ScriptedServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep hermetic test output silent."""

    def do_CONNECT(self) -> None:
        host = self.path.rsplit(":", 1)[0].lower()
        if host not in self.server.handlers:
            with self.server.observations_lock:
                self.server.observations.append(
                    ScriptedRequest(host, "CONNECT", self.path, (), b"")
                )
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
        except (ssl.SSLError, OSError):
            pass
        finally:
            with contextlib.suppress(OSError):
                tls.shutdown(socket.SHUT_RDWR)
            tls.close()
            self.close_connection = True

    def do_GET(self) -> None:
        self.send_error(502, "Hermetic fixture only tunnels HTTPS")


class ScriptedHttpsProxy:
    """Running proxy plus the trust material a child process must use."""

    def __init__(self, server: _ScriptedServer, thread: threading.Thread, ca_path: Path) -> None:
        self._server = server
        self._thread = thread
        self.ca_path = ca_path

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def observations(self) -> tuple[ScriptedRequest, ...]:
        with self._server.observations_lock:
            return tuple(self._server.observations)

    def requests_to(self, host: str) -> tuple[ScriptedRequest, ...]:
        return tuple(o for o in self.observations if o.host == host and o.method != "CONNECT")

    def clear(self) -> None:
        with self._server.observations_lock:
            self._server.observations.clear()

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
        for name in (
            "ALL_PROXY",
            "HTTP_PROXY",
            "all_proxy",
            "http_proxy",
            "CURL_CA_BUNDLE",
            "APM_EXTRA_CA_BUNDLE",
        ):
            child.pop(name, None)
        child["HTTPS_PROXY"] = self.proxy_url
        child["https_proxy"] = self.proxy_url
        child["NO_PROXY"] = "localhost"
        child["no_proxy"] = "localhost"
        child["REQUESTS_CA_BUNDLE"] = str(self.ca_path)
        child["SSL_CERT_FILE"] = str(self.ca_path)
        child["GIT_SSL_CAINFO"] = str(self.ca_path)
        ports = [p for p in child.get("APM_TEST_LOOPBACK_PORTS", "").split(",") if p]
        child["APM_TEST_LOOPBACK_PORTS"] = ",".join([*ports, str(self.port)])
        return child

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextlib.contextmanager
def local_scripted_https_proxy(
    directory: Path,
    handlers: Mapping[str, Handler],
) -> Iterator[ScriptedHttpsProxy]:
    """Serve each host in *handlers* over a recording HTTPS proxy."""
    hosts = tuple(sorted(host.lower() for host in handlers))
    ca_pem, leaf_pem, leaf_key = _mint_certificates(directory, hosts)
    with _stdlib_server_tls():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(leaf_pem), keyfile=str(leaf_key))
    server = _ScriptedServer(("127.0.0.1", 0), _ProxyHandler)
    server.tls_context = context
    server.handlers = {host.lower(): handler for host, handler in handlers.items()}
    server.observations = []
    server.observations_lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, name="scripted-https", daemon=True)
    thread.start()
    proxy = ScriptedHttpsProxy(server, thread, ca_pem)
    try:
        yield proxy
    finally:
        proxy.close()
