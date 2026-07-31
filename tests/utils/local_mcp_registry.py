"""Local MCP Registry v0.1 fixtures for real-binary lifecycle tests."""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


class _RegistryServer(ThreadingHTTPServer):
    """HTTP server carrying one immutable MCP Registry server document."""

    daemon_threads = True

    def __init__(
        self,
        server_document: Mapping[str, Any],
        request_paths: deque[str],
    ) -> None:
        self.server_document = dict(server_document)
        self.request_paths = request_paths
        super().__init__(("127.0.0.1", 0), _RegistryRequestHandler)


class _RegistryRequestHandler(BaseHTTPRequestHandler):
    """Serve the search and detail routes used by ``SimpleRegistryClient``."""

    server: _RegistryServer

    def do_GET(self) -> None:
        """Return one v0.1 search or detail response."""
        parsed = urlparse(self.path)
        self.server.request_paths.append(self.path)
        if parsed.path == "/v0.1/servers":
            payload = {
                "servers": [
                    {
                        "server": {
                            "name": self.server.server_document["name"],
                            "description": self.server.server_document.get("description", ""),
                        }
                    }
                ],
                "metadata": {},
            }
            self._write_json(200, payload)
            return

        prefix = "/v0.1/servers/"
        suffix = "/versions/latest"
        if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
            encoded_name = parsed.path[len(prefix) : -len(suffix)]
            if unquote(encoded_name) == self.server.server_document["name"]:
                self._write_json(200, {"server": self.server.server_document})
                return
        self._write_json(404, {"error": "not found"})

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep lifecycle output deterministic and quiet."""

    def _write_json(self, status: int, payload: Mapping[str, Any]) -> None:
        """Write one compact JSON response."""
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@dataclass
class LocalMcpRegistry:
    """One bounded in-process MCP Registry v0.1 endpoint."""

    _server: _RegistryServer
    _thread: threading.Thread
    request_paths: deque[str] = field(default_factory=deque)

    @property
    def url(self) -> str:
        """Return the loopback endpoint accepted by ``registry``."""
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        """Stop the endpoint and wait for its serving thread."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("local MCP registry thread did not stop")

    def __enter__(self) -> LocalMcpRegistry:
        """Return the running registry."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop the registry."""
        self.close()


class LocalMcpRegistryFactory:
    """Create isolated registry endpoints from authored v0.1 documents."""

    def __init__(self, root: Path) -> None:
        """Reserve a scenario-owned root for registry fixture state."""
        root.mkdir(parents=True, exist_ok=False)
        self._root = root

    def start(self, server_document: Mapping[str, Any]) -> LocalMcpRegistry:
        """Start a registry endpoint for one immutable server document."""
        if not isinstance(server_document.get("name"), str):
            raise ValueError("server_document.name must be a string")
        request_paths: deque[str] = deque()
        server = _RegistryServer(server_document, request_paths)
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"mcp-registry-{self._root.name}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            server.server_close()
            raise
        if not thread.is_alive():
            server.server_close()
            raise RuntimeError("local MCP registry thread did not start")
        return LocalMcpRegistry(
            _server=server,
            _thread=thread,
            request_paths=request_paths,
        )
