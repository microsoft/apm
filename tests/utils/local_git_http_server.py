"""Loopback dumb-HTTP Git repository fixtures."""

from __future__ import annotations

import base64
import functools
import subprocess
import threading
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from apm_cli.utils.path_security import ensure_path_within
from tests.utils.local_git_repository import LocalGitRepository


@dataclass(frozen=True)
class GitHttpObservation:
    """One request observed by the local Git HTTP fixture."""

    method: str
    path: str
    authorization_present: bool
    accepted: bool


class _GitHttpServer(ThreadingHTTPServer):
    """HTTP server state shared with request handlers."""

    daemon_threads = True
    fixture_root: Path
    private_repositories: frozenset[str]
    expected_authorization: str
    observations: list[GitHttpObservation]
    observations_lock: threading.Lock


class _GitHttpHandler(SimpleHTTPRequestHandler):
    """Serve dumb-HTTP Git data and hide private repositories anonymously."""

    server: _GitHttpServer

    def do_CONNECT(self) -> None:
        """Reject HTTPS proxy tunnelling without contacting the target host."""
        self._record(accepted=False)
        self.send_error(502, "Hermetic fixture does not proxy external hosts")

    def do_GET(self) -> None:
        """Serve a file after enforcing the fixture repository policy."""
        if not self._authorize_request():
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        """Serve metadata after enforcing the fixture repository policy."""
        if not self._authorize_request():
            return
        super().do_HEAD()

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep hermetic test output silent."""

    def _authorize_request(self) -> bool:
        repository = self._repository_name()
        authorization = self.headers.get("Authorization")
        accepted = (
            repository not in self.server.private_repositories
            or authorization == self.server.expected_authorization
        )
        self._record(accepted=accepted)
        if not accepted:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="fixture-private"')
            self.send_header("Content-Length", "0")
            self.end_headers()
        return accepted

    def _repository_name(self) -> str:
        path = unquote(urlparse(self.path).path).lstrip("/")
        return path.split("/", 1)[0]

    def _record(self, *, accepted: bool) -> None:
        observation = GitHttpObservation(
            method=self.command,
            path=urlparse(self.path).path,
            authorization_present=self.headers.get("Authorization") is not None,
            accepted=accepted,
        )
        with self.server.observations_lock:
            self.server.observations.append(observation)


class LocalGitHttpServer:
    """Running loopback server for one or more bare Git repositories."""

    def __init__(self, server: _GitHttpServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def proxy_url(self) -> str:
        """Return a loopback URL suitable for HTTPS_PROXY."""
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def observations(self) -> tuple[GitHttpObservation, ...]:
        """Return a stable snapshot of observed HTTP requests."""
        with self._server.observations_lock:
            return tuple(self._server.observations)

    def remote_url(self, repository: LocalGitRepository) -> str:
        """Return the local dumb-HTTP URL for *repository*."""
        return f"{self.proxy_url}/{repository.origin.name}"

    def close(self) -> None:
        """Stop the server and release its loopback socket."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("Local Git HTTP server did not stop")

    def __enter__(self) -> LocalGitHttpServer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


class LocalGitHttpServerFactory:
    """Publish owned bare repositories through one loopback HTTP server."""

    def __init__(
        self,
        root: Path,
        *,
        real_git: Path,
        env: dict[str, str],
    ) -> None:
        self._root = root.resolve()
        self._real_git = real_git.resolve()
        self._env = dict(env)

    def start(
        self,
        repositories: tuple[LocalGitRepository, ...],
        *,
        password: str,
        private_repositories: tuple[LocalGitRepository, ...] = (),
        username: str = "x-access-token",
    ) -> LocalGitHttpServer:
        """Prepare and serve repositories with optional Basic-auth policy."""
        if not repositories:
            raise ValueError("At least one repository is required")
        private_names = {repository.origin.name for repository in private_repositories}
        repository_names = {repository.origin.name for repository in repositories}
        if not private_names <= repository_names:
            raise ValueError("Private repositories must be included in repositories")

        for repository in repositories:
            ensure_path_within(repository.origin, self._root)
            subprocess.run(
                (
                    str(self._real_git),
                    "--git-dir",
                    str(repository.origin),
                    "update-server-info",
                ),
                env=self._env,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )

        credentials = base64.b64encode(f"{username}:{password}".encode("ascii")).decode("ascii")
        handler = functools.partial(_GitHttpHandler, directory=str(self._root))
        server = _GitHttpServer(("127.0.0.1", 0), handler)
        server.fixture_root = self._root
        server.private_repositories = frozenset(private_names)
        server.expected_authorization = f"Basic {credentials}"
        server.observations = []
        server.observations_lock = threading.Lock()
        thread = threading.Thread(
            target=server.serve_forever,
            name="local-git-http",
            daemon=True,
        )
        thread.start()
        return LocalGitHttpServer(server, thread)
