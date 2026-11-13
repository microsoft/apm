"""Credential headers stay on their origin across redirects (#3649)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from apm_cli.utils.http_credentials import CredentialScopedSession


class _Recorder(ThreadingHTTPServer):
    daemon_threads = True
    location: str | None = None
    seen: list[dict[str, str]]


class _Handler(BaseHTTPRequestHandler):
    server: _Recorder

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep test output silent."""

    def do_GET(self) -> None:
        self.server.seen.append({k.lower(): v for k, v in self.headers.items()})
        if self.server.location and self.path == "/start":
            self.send_response(302)
            self.send_header("Location", self.server.location)
        else:
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@contextmanager
def _server(location: str | None = None) -> Iterator[_Recorder]:
    server = _Recorder(("127.0.0.1", 0), _Handler)
    server.location = location
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_credential_headers_are_dropped_on_cross_origin_redirect() -> None:
    with _server() as target:
        target_url = f"http://localhost:{target.server_address[1]}/landing"
        with _server(target_url) as origin:
            origin_url = f"http://127.0.0.1:{origin.server_address[1]}/start"
            with CredentialScopedSession() as session:
                session.trust_env = False
                session.get(
                    origin_url,
                    headers={"PRIVATE-TOKEN": "tok-1", "Authorization": "Bearer tok-2"},
                    timeout=10,
                )
        assert origin.seen[0]["private-token"] == "tok-1"
        assert "private-token" not in target.seen[0]
        assert "authorization" not in target.seen[0]


def test_credential_headers_are_kept_on_same_origin_redirect() -> None:
    with _server() as origin:
        port = origin.server_address[1]
        origin.location = f"http://127.0.0.1:{port}/landing"
        with CredentialScopedSession() as session:
            session.trust_env = False
            session.get(
                f"http://127.0.0.1:{port}/start",
                headers={"PRIVATE-TOKEN": "tok-1"},
                timeout=10,
            )
        assert [seen.get("private-token") for seen in origin.seen] == ["tok-1", "tok-1"]
