"""Loopback HTTPS proxy that serves GitHub raw and REST fixtures from local Git.

The installed CLI (uv console script or frozen binary) reaches
``raw.githubusercontent.com`` and ``api.github.com`` through ``HTTPS_PROXY``.
This proxy terminates each CONNECT tunnel with a leaf certificate minted for
those two hosts by a private CA, answers from owned bare repositories, and
records every request so tests can count raw and Contents API traffic. Any
other CONNECT target is refused, so the fixture never contacts real hosts.
"""

from __future__ import annotations

import base64
import contextlib
import json
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from tests.utils.local_git_http_server import _stdlib_server_tls
from tests.utils.local_git_repository import LocalGitRepository

RAW_HOST = "raw.githubusercontent.com"
API_HOST = "api.github.com"
_SERVED_HOSTS = (RAW_HOST, API_HOST)

_CA_CNF = """\
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = APM GitHub Fixture Root CA
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
"""

_LEAF_CNF = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = api.github.com
[v3_leaf]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:raw.githubusercontent.com, DNS:api.github.com
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def openssl_executable() -> str | None:
    """Return the OpenSSL CLI used to mint fixture certificates, if any."""
    return shutil.which("openssl")


@dataclass(frozen=True)
class GitHubHttpObservation:
    """One request answered (or refused) by the fixture."""

    host: str
    method: str
    path: str
    query: str
    status: int
    authenticated: bool

    @property
    def is_contents_api(self) -> bool:
        """Whether this request used the REST Contents API."""
        return self.host == API_HOST and "/contents/" in self.path

    @property
    def is_raw(self) -> bool:
        """Whether this request used the raw-file CDN."""
        return self.host == RAW_HOST


def _mint_certificates(directory: Path) -> tuple[Path, Path, Path]:
    """Create a private CA and a GitHub-host leaf; return (ca, leaf, key)."""
    openssl = openssl_executable()
    if openssl is None:
        raise RuntimeError("openssl CLI is required to mint fixture certificates")
    directory.mkdir(parents=True, exist_ok=True)
    ca_cnf, leaf_cnf = directory / "ca.cnf", directory / "leaf.cnf"
    ca_cnf.write_text(_CA_CNF, encoding="ascii")
    leaf_cnf.write_text(_LEAF_CNF, encoding="ascii")
    ca_key, ca_pem = directory / "ca.key", directory / "ca.pem"
    leaf_key, leaf_csr, leaf_pem = (
        directory / "leaf.key",
        directory / "leaf.csr",
        directory / "leaf.pem",
    )

    def _run(*args: object) -> None:
        subprocess.run(
            [openssl, *(str(arg) for arg in args)],
            check=True,
            capture_output=True,
            timeout=60,
        )

    _run(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key,
        "-out", ca_pem, "-days", "2", "-config", ca_cnf,
    )  # fmt: skip
    _run(
        "req", "-newkey", "rsa:2048", "-nodes", "-keyout", leaf_key,
        "-out", leaf_csr, "-config", leaf_cnf,
    )  # fmt: skip
    _run(
        "x509", "-req", "-in", leaf_csr, "-CA", ca_pem, "-CAkey", ca_key,
        "-CAcreateserial", "-out", leaf_pem, "-days", "2",
        "-extfile", leaf_cnf, "-extensions", "v3_leaf",
    )  # fmt: skip
    return ca_pem, leaf_pem, leaf_key


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    tls_context: ssl.SSLContext
    repositories: Mapping[str, LocalGitRepository]
    private_repositories: frozenset[str]
    accepted_tokens: frozenset[str]
    real_git: str
    observations: list[GitHubHttpObservation]
    observations_lock: threading.Lock


class _GitHubHandler(BaseHTTPRequestHandler):
    """Answer one TLS tunnel's GitHub requests from owned bare repositories."""

    protocol_version = "HTTP/1.1"
    server: _ProxyServer
    target_host: str

    def __init__(self, request, client_address, server, *, target_host: str) -> None:
        self.target_host = target_host
        super().__init__(request, client_address, server)

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep hermetic test output silent."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if self.target_host == RAW_HOST:
            status, body, content_type = self._raw(path)
        else:
            status, body, content_type = self._api(path, parse_qs(parsed.query))
        self._record(path, parsed.query, status)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self) -> bool:
        header = self.headers.get("Authorization") or ""
        scheme, _, credential = header.partition(" ")
        return scheme.lower() in ("token", "bearer") and credential in self.server.accepted_tokens

    def _visible(self, slug: str) -> bool:
        return slug not in self.server.private_repositories or self._authenticated()

    def _git(self, slug: str, *args: str) -> subprocess.CompletedProcess[bytes]:
        repository = self.server.repositories[slug]
        return subprocess.run(
            (self.server.real_git, "--git-dir", str(repository.origin), *args),
            capture_output=True,
            check=False,
            timeout=30,
        )

    def _blob(self, slug: str, ref: str, file_path: str) -> bytes | None:
        result = self._git(slug, "cat-file", "blob", f"{ref}:{file_path}")
        return result.stdout if result.returncode == 0 else None

    def _not_found(self) -> tuple[int, bytes, str]:
        return 404, b'{"message": "Not Found"}', "application/json"

    def _raw(self, path: str) -> tuple[int, bytes, str]:
        parts = path.lstrip("/").split("/")
        if len(parts) < 4:
            return self._not_found()
        slug = f"{parts[0]}/{parts[1]}"
        if slug not in self.server.repositories or slug in self.server.private_repositories:
            # Real raw.githubusercontent.com answers 404 for private content.
            return self._not_found()
        # Like the real host, accept slash-containing refs such as release/pilot.
        for split in range(3, len(parts)):
            content = self._blob(slug, "/".join(parts[2:split]), "/".join(parts[split:]))
            if content is not None:
                return 200, content, "text/plain; charset=utf-8"
        return self._not_found()

    def _api(self, path: str, query: dict[str, list[str]]) -> tuple[int, bytes, str]:
        parts = path.lstrip("/").split("/", 4)
        if len(parts) < 3 or parts[0] != "repos":
            return self._not_found()
        slug = f"{parts[1]}/{parts[2]}"
        if slug not in self.server.repositories or not self._visible(slug):
            return self._not_found()
        if len(parts) == 3:
            payload = {"full_name": slug, "private": slug in self.server.private_repositories}
            return 200, json.dumps(payload).encode(), "application/json"
        if len(parts) == 5 and parts[3] == "commits":
            result = self._git(slug, "rev-parse", "--verify", "--quiet", f"{parts[4]}^{{commit}}")
            if result.returncode != 0:
                return self._not_found()
            sha = result.stdout.decode("ascii").strip()
            if "sha" in (self.headers.get("Accept") or ""):
                return 200, sha.encode("ascii"), "text/plain"
            return 200, json.dumps({"sha": sha}).encode(), "application/json"
        if len(parts) == 5 and parts[3] == "contents":
            ref = (query.get("ref") or ["HEAD"])[0]
            content = self._blob(slug, ref, parts[4])
            if content is None:
                return self._not_found()
            if "raw" in (self.headers.get("Accept") or ""):
                return 200, content, "application/octet-stream"
            payload = {
                "type": "file",
                "name": parts[4].rsplit("/", 1)[-1],
                "path": parts[4],
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            }
            return 200, json.dumps(payload).encode(), "application/json"
        return self._not_found()

    def _record(self, path: str, query: str, status: int) -> None:
        observation = GitHubHttpObservation(
            host=self.target_host,
            method=self.command,
            path=path,
            query=query,
            status=status,
            authenticated=self.headers.get("Authorization") is not None,
        )
        with self.server.observations_lock:
            self.server.observations.append(observation)


class _ProxyHandler(BaseHTTPRequestHandler):
    """Accept CONNECT only for GitHub fixture hosts and terminate TLS locally."""

    server: _ProxyServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep hermetic test output silent."""

    def do_CONNECT(self) -> None:
        host = self.path.rsplit(":", 1)[0].lower()
        if host not in _SERVED_HOSTS:
            with self.server.observations_lock:
                self.server.observations.append(
                    GitHubHttpObservation(host, "CONNECT", self.path, "", 502, False)
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
            _GitHubHandler(tls, self.client_address, self.server, target_host=host)
        finally:
            with contextlib.suppress(OSError):
                tls.shutdown(socket.SHUT_RDWR)
            tls.close()
            self.close_connection = True

    def do_GET(self) -> None:
        self.send_error(502, "Hermetic fixture only tunnels HTTPS")


class LocalGitHubHttpsFixture:
    """Running proxy plus the trust material a child process must use."""

    def __init__(self, server: _ProxyServer, thread: threading.Thread, ca_path: Path) -> None:
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
    def observations(self) -> tuple[GitHubHttpObservation, ...]:
        with self._server.observations_lock:
            return tuple(self._server.observations)

    def clear(self) -> None:
        """Forget observations so the next command is counted in isolation."""
        with self._server.observations_lock:
            self._server.observations.clear()

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
        ports = [p for p in child.get("APM_TEST_LOOPBACK_PORTS", "").split(",") if p]
        child["APM_TEST_LOOPBACK_PORTS"] = ",".join([*ports, str(self.port)])
        return child

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextlib.contextmanager
def local_github_https_fixture(
    directory: Path,
    repositories: Mapping[str, LocalGitRepository],
    *,
    private: tuple[str, ...] = (),
    tokens: tuple[str, ...] = (),
) -> Iterator[LocalGitHubHttpsFixture]:
    """Serve ``owner/repo`` slugs from owned bare repositories over a MITM proxy."""
    real_git = shutil.which("git")
    if real_git is None:
        raise RuntimeError("git executable is required")
    unknown = set(private) - set(repositories)
    if unknown:
        raise ValueError(f"Private slugs are not served repositories: {sorted(unknown)}")
    ca_pem, leaf_pem, leaf_key = _mint_certificates(directory)
    with _stdlib_server_tls():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(leaf_pem), keyfile=str(leaf_key))
    server = _ProxyServer(("127.0.0.1", 0), _ProxyHandler)
    server.tls_context = context
    server.repositories = dict(repositories)
    server.private_repositories = frozenset(private)
    server.accepted_tokens = frozenset(tokens)
    server.real_git = real_git
    server.observations = []
    server.observations_lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, name="github-https-fixture", daemon=True)
    thread.start()
    fixture = LocalGitHubHttpsFixture(server, thread, ca_pem)
    try:
        yield fixture
    finally:
        fixture.close()
