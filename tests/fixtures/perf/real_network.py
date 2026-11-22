"""Real-network mode of the perf fixtures: curated REAL public APM packages.

``REAL_PACKAGES`` lists public packages on github.com that real users install,
pinned to immutable refs so runs compare: whole-repo packages, virtual skill
directories and virtual single-file primitives of ``github/awesome-copilot``.
``python -m tests.benchmarks.journeys verify-real`` installs each one alone and
records whether it resolves; re-verify after changing this list.

``counting_tunnel`` is a plain CONNECT proxy for this mode: it forwards TLS
untouched to the real host and counts tunnels and bytes per command (request
counts need TLS termination, which only the offline mode does).
"""

from __future__ import annotations

import contextlib
import select
import socket
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# github/awesome-copilot main at fixture authoring time (2026-11-22).
AWESOME_COPILOT_SHA = "f10fdb8b2660ce85c98f220b2f1ca24653768a5c"
_AC = "github/awesome-copilot"

REAL_PACKAGES: tuple[str, ...] = (
    "microsoft/apm-sample-package#v1.0.0",
    "microsoft/apm/packages/apm-guide#v0.37.0",
    f"{_AC}/skills/review-and-refactor#{AWESOME_COPILOT_SHA}",
    f"{_AC}/skills/documentation-writer#{AWESOME_COPILOT_SHA}",
    f"{_AC}/skills/agent-governance#{AWESOME_COPILOT_SHA}",
    f"{_AC}/skills/api-breaking-change-detector#{AWESOME_COPILOT_SHA}",
    f"{_AC}/agents/janitor.agent.md#{AWESOME_COPILOT_SHA}",
    f"{_AC}/agents/principal-software-engineer.agent.md#{AWESOME_COPILOT_SHA}",
    f"{_AC}/instructions/go.instructions.md#{AWESOME_COPILOT_SHA}",
    f"{_AC}/instructions/security-and-owasp.instructions.md#{AWESOME_COPILOT_SHA}",
)

# Hosts the real-network mode may reach; anything else is refused and counted.
ALLOWED_HOSTS = frozenset(
    {"github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com"}
)


@dataclass(frozen=True)
class TunnelTraffic:
    connects: int
    refused: int
    bytes_up: int
    bytes_down: int

    def as_dict(self) -> dict[str, int]:
        return {
            "connects": self.connects,
            "refused": self.refused,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
        }


class _TunnelServer(ThreadingHTTPServer):
    daemon_threads = True
    lock: threading.Lock
    counts: dict[str, int]


class _TunnelHandler(BaseHTTPRequestHandler):
    server: _TunnelServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Silent."""

    def _count(self, name: str, amount: int = 1) -> None:
        with self.server.lock:
            self.server.counts[name] += amount

    def do_CONNECT(self) -> None:
        host, _, port = self.path.rpartition(":")
        if host.lower() not in ALLOWED_HOSTS:
            self._count("refused")
            self.send_error(502, "host not allowed in real-network perf mode")
            return
        try:
            upstream = socket.create_connection((host, int(port or 443)), timeout=30)
        except OSError:
            self._count("refused")
            self.send_error(502, "upstream unreachable")
            return
        self._count("connects")
        self.send_response(200, "Connection Established")
        self.end_headers()
        self._pump(self.connection, upstream)
        upstream.close()
        self.close_connection = True

    def _pump(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = [client, upstream]
        while True:
            readable, _, broken = select.select(sockets, [], sockets, 60)
            if broken or not readable:
                return
            for source in readable:
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                target = upstream if source is client else client
                target.sendall(data)
                self._count("bytes_up" if source is client else "bytes_down", len(data))


class CountingTunnel:
    def __init__(self, server: _TunnelServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def traffic(self) -> TunnelTraffic:
        with self._server.lock:
            return TunnelTraffic(**self._server.counts)

    def reset_traffic(self) -> None:
        with self._server.lock:
            for key in self._server.counts:
                self._server.counts[key] = 0

    def child_environment(self, environment: Mapping[str, str]) -> dict[str, str]:
        child = dict(environment)
        for name in ("ALL_PROXY", "HTTP_PROXY", "all_proxy", "http_proxy"):
            child.pop(name, None)
        child["HTTPS_PROXY"] = child["https_proxy"] = self.proxy_url
        child["NO_PROXY"] = child["no_proxy"] = "localhost"
        child["GIT_ALLOW_PROTOCOL"] = "file:https"
        return child


@contextlib.contextmanager
def counting_tunnel() -> Iterator[CountingTunnel]:
    server = _TunnelServer(("127.0.0.1", 0), _TunnelHandler)
    server.lock = threading.Lock()
    server.counts = {"connects": 0, "refused": 0, "bytes_up": 0, "bytes_down": 0}
    thread = threading.Thread(target=server.serve_forever, name="perf-tunnel", daemon=True)
    thread.start()
    try:
        yield CountingTunnel(server, thread)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
