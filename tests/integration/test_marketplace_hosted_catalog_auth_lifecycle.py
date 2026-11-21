"""Installed-CLI lifecycle for private hosted HTTPS marketplace catalogs (#3300).

A loopback HTTPS server owned by the test serves a ``marketplace.json`` that
requires a credential, plus redirect endpoints and a dumb-HTTP Git package
inside the catalog's own path scope. The installed CLI runs add, browse,
search, view, install and refresh with ``APM_MARKETPLACE_TOKEN_<NAME>``
(Bearer) and ``APM_MARKETPLACE_USER_/PASS_<NAME>`` (Basic). Every request the
server sees is recorded, so the tests assert where a credential went, not
just what the CLI printed.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_http_server import _stdlib_server_tls
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX openssl/git fixture"),
    pytest.mark.promise("P-OWN-marketplace-catalog-credentials"),
]

_NAME = "corp"
_TOKEN = "tok-corp-good-0123456789"
_FORBIDDEN_TOKEN = "tok-corp-forbidden-0123456789"
_USER = "alice"
_PASSWORD = "pw-corp-good-0123456789"
_BASIC = "Basic " + base64.b64encode(f"{_USER}:{_PASSWORD}".encode()).decode()
_NETRC_BASIC = "Basic " + base64.b64encode(b"netrc-user:netrc-sentinel").decode()
_ACCEPTED = frozenset({f"Bearer {_TOKEN}", _BASIC})
_PLUGIN = "hello"
_INSTALL = ("--target", "copilot", "--no-policy", "--parallel-downloads", "0")


@dataclass(frozen=True)
class _Seen:
    host: str
    path: str
    authorization: str | None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    package_root: Path
    catalog: bytes
    seen: list[_Seen]
    lock: threading.Lock
    port: int
    mirror_port: int


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep test output quiet."""

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        authorization = self.headers.get("Authorization")
        with self.server.lock:
            self.server.seen.append(_Seen(self.headers.get("Host", ""), path, authorization))
        json_headers = {"Content-Type": "application/json"}
        if path == "/private/marketplace.json":
            if authorization == f"Bearer {_FORBIDDEN_TOKEN}":
                self._send(403)
            elif authorization in _ACCEPTED:
                self._send(200, self.server.catalog, json_headers)
            else:
                self._send(401, headers={"WWW-Authenticate": 'Bearer realm="corp"'})
        elif path == "/redir-cross/marketplace.json":
            target = f"https://127.0.0.1:{self.server.mirror_port}/public/marketplace.json"
            self._send(302, headers={"Location": target})
        elif path == "/redir-out/marketplace.json":
            self._send(302, headers={"Location": "/public/marketplace.json"})
        elif path == "/public/marketplace.json":
            self._send(200, self.server.catalog, json_headers)
        elif path.startswith("/private/pkg.git/"):
            self._git_backend(path[len("/private/pkg.git") :], b"")
        else:
            self._send(404)

    def do_POST(self) -> None:
        path = unquote(urlsplit(self.path).path)
        with self.server.lock:
            self.server.seen.append(
                _Seen(self.headers.get("Host", ""), path, self.headers.get("Authorization"))
            )
        body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        if path.startswith("/private/pkg.git/"):
            self._git_backend(path[len("/private/pkg.git") :], body)
        else:
            self._send(404)

    def _git_backend(self, rest: str, body: bytes) -> None:
        """Serve the package over smart HTTP through ``git http-backend``."""
        repository = self.server.package_root
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_PROJECT_ROOT": str(repository.parent),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": f"/{repository.name}{rest}",
            "QUERY_STRING": urlsplit(self.path).query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            "REMOTE_ADDR": "127.0.0.1",
        }
        result = subprocess.run(
            ("git", "http-backend"), input=body, env=environment, capture_output=True, timeout=60
        )
        head, _, payload = result.stdout.partition(b"\r\n\r\n")
        status, headers = 200, {}
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name:
                headers[name] = value.strip()
        self._send(status, payload, headers)

    do_HEAD = do_GET


class _CatalogServer:
    def __init__(self, server: _Server, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def base(self) -> str:
        return f"https://127.0.0.1:{self._server.port}"

    @property
    def ports(self) -> str:
        return f"{self._server.port},{self._server.mirror_port}"

    @property
    def seen(self) -> tuple[_Seen, ...]:
        with self._server.lock:
            return tuple(self._server.seen)

    def clear(self) -> None:
        with self._server.lock:
            self._server.seen.clear()


def _mint_certificate(root: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required for the HTTPS catalog fixture")
    certificate, key = root / "catalog-cert.pem", root / "catalog-key.pem"
    subprocess.run(
        (
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(certificate),
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-sha256", "-days", "1",
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )  # fmt: skip
    return certificate, key


@contextlib.contextmanager
def _catalog_server(certificate: Path, key: Path, package_root: Path) -> Iterator[_CatalogServer]:
    servers = []
    with _stdlib_server_tls():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(certificate), keyfile=str(key))
        for _ in range(2):
            instance = _Server(("127.0.0.1", 0), _Handler)
            instance.socket = context.wrap_socket(instance.socket, server_side=True)
            servers.append(instance)
    # The mirror is another origin (same host, other port) sharing one log.
    server, mirror = servers
    seen: list[_Seen] = []
    lock = threading.Lock()
    for instance in servers:
        instance.package_root = package_root.resolve()
        instance.seen = seen
        instance.lock = lock
        instance.port = instance.server_address[1]
        instance.mirror_port = mirror.server_address[1]
    package_url = f"https://127.0.0.1:{server.port}/private/pkg.git"
    server.catalog = json.dumps(
        {
            "name": "corp-catalog",
            "owner": {"name": "Acme"},
            "plugins": [
                {
                    "name": _PLUGIN,
                    "description": "Hello from the private catalog",
                    "version": "1.0.0",
                    "source": {"source": "url", "url": package_url},
                }
            ],
        }
    ).encode()
    mirror.catalog = server.catalog
    threads = [
        threading.Thread(target=instance.serve_forever, name="catalog-https", daemon=True)
        for instance in servers
    ]
    for thread in threads:
        thread.start()
    try:
        yield _CatalogServer(server, threads[0])
    finally:
        for instance, thread in zip(servers, threads, strict=True):
            instance.shutdown()
            instance.server_close()
            thread.join(timeout=10)


class _Scenario:
    """One isolated CLI home talking to the protected catalog server."""

    def __init__(self, tmp_path: Path, apm_binary_path: Path) -> None:
        self.isolated = IsolatedApmEnvironment.create(
            tmp_path / "scenario", base_env=dict(os.environ)
        )
        self.project = self.isolated.work_root / "consumer"
        self.project.mkdir()
        (self.project / "apm.yml").write_text(
            "name: consumer\nversion: 1.0.0\ntargets:\n  - copilot\ndependencies:\n  apm: []\n",
            encoding="utf-8",
        )
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
        self.certificate, self.key = _mint_certificate(self.isolated.root)
        self.package_root = self._publish_package()

    def _publish_package(self) -> Path:
        environment = self.isolated.subprocess_env()
        repositories = LocalGitRepositoryFactory(self.isolated.repository_root, env=environment)
        repository = repositories.create("pkg")
        plugin = repository.worktree
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": _PLUGIN, "version": "1.0.0", "description": "hello"}),
            encoding="utf-8",
        )
        skill = plugin / "skills" / "hello-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: hello-skill\ndescription: private catalog fixture\n---\n# Hello\n",
            encoding="utf-8",
        )
        repositories.commit(repository, message="hello plugin")
        return repository.origin

    def env(self, server: _CatalogServer, **credentials: str) -> dict[str, str]:
        environment = self.isolated.subprocess_env()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
            environment.pop(name, None)
        environment.update(
            {
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "REQUESTS_CA_BUNDLE": str(self.certificate),
                "GIT_ALLOW_PROTOCOL": "file:http:https",
                "APM_TEST_LOOPBACK_PORTS": server.ports,
                # Package/Git credentials that must never reach the catalog.
                "GITHUB_APM_PAT": "github-apm-sentinel",
                "APM_REGISTRY_TOKEN_CORP": "registry-sentinel",
            }
        )
        environment.update(credentials)
        git_config = Path(environment["GIT_CONFIG_GLOBAL"])
        subprocess.run(
            ("git", "config", "--file", str(git_config), "http.sslCAInfo", str(self.certificate)),
            check=True,
            capture_output=True,
        )
        # A real .netrc for the catalog host proves ambient credentials stay off.
        netrc = self.isolated.home / ".netrc"
        netrc.write_text(
            "machine 127.0.0.1 login netrc-user password netrc-sentinel\n", encoding="utf-8"
        )
        netrc.chmod(0o600)
        return environment

    def run(self, env: dict[str, str], *args: str, step: str) -> CommandResult:
        return self.runner.run(
            args, scenario_id=f"hosted-catalog-auth-{step}", cwd=self.project, env=env
        )


def _output(result: CommandResult) -> str:
    return " ".join(f"{result.stdout}\n{result.stderr}".split())


def _expect_ok(result: CommandResult) -> str:
    output = _output(result)
    assert result.returncode == 0, output
    return output


def _catalog_authorizations(server: _CatalogServer) -> list[str | None]:
    return [seen.authorization for seen in server.seen if seen.path.endswith("marketplace.json")]


def _scenario(tmp_path: Path, apm_binary_path: Path) -> _Scenario:
    if shutil.which("git") is None:
        pytest.skip("git executable not available")
    return _Scenario(tmp_path, apm_binary_path)


@pytest.mark.parametrize(
    ("credentials", "expected"),
    [
        pytest.param({"APM_MARKETPLACE_TOKEN_CORP": _TOKEN}, f"Bearer {_TOKEN}", id="bearer"),
        pytest.param(
            {"APM_MARKETPLACE_USER_CORP": _USER, "APM_MARKETPLACE_PASS_CORP": _PASSWORD},
            _BASIC,
            id="basic",
        ),
        pytest.param(
            {
                "APM_MARKETPLACE_TOKEN_CORP": _TOKEN,
                "APM_MARKETPLACE_USER_CORP": "wrong-user",
                "APM_MARKETPLACE_PASS_CORP": "wrong-password",
            },
            f"Bearer {_TOKEN}",
            id="bearer-over-basic",
        ),
    ],
)
def test_private_catalog_lifecycle_sends_only_the_bound_credential(
    tmp_path: Path, apm_binary_path: Path, credentials: dict[str, str], expected: str
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path)
    with _catalog_server(scenario.certificate, scenario.key, scenario.package_root) as server:
        env = scenario.env(server, **credentials)
        url = f"{server.base}/private/marketplace.json"

        add = _expect_ok(scenario.run(env, "marketplace", "add", url, "--name", _NAME, step="add"))
        assert f"Marketplace '{_NAME}' registered (1 plugin)" in add
        browse = _expect_ok(scenario.run(env, "marketplace", "browse", _NAME, step="browse"))
        assert _PLUGIN in browse
        search = _expect_ok(scenario.run(env, "search", f"hello@{_NAME}", step="search"))
        assert f"{_PLUGIN}@{_NAME}" in search
        view = _expect_ok(scenario.run(env, "view", f"{_PLUGIN}@{_NAME}", step="view"))
        assert "Hello from the private catalog" in view
        catalog_auth = _catalog_authorizations(server)
        server.clear()
        _expect_ok(scenario.run(env, "install", f"{_PLUGIN}@{_NAME}", *_INSTALL, step="install"))
        install_seen = server.seen

    assert len(catalog_auth) >= 2, "add and browse must fetch the catalog"
    assert set(catalog_auth) == {expected}
    assert set(_catalog_authorizations(server)) <= {expected}
    git_requests = [seen for seen in install_seen if seen.path.startswith("/private/pkg.git/")]
    assert git_requests, "the package was not downloaded from the catalog host"
    assert all(seen.authorization is None for seen in git_requests)
    deployed = scenario.project / ".agents" / "skills" / "hello-skill" / "SKILL.md"
    assert deployed.is_file()
    saved = json.loads((scenario.isolated.config_root / "marketplaces.json").read_text())
    serialized = json.dumps(saved) + (scenario.project / "apm.yml").read_text()
    serialized += (scenario.project / "apm.lock.yaml").read_text()
    for secret in (_TOKEN, _PASSWORD, "netrc-sentinel"):
        assert secret not in serialized
    assert _NETRC_BASIC not in {seen.authorization for seen in server.seen}


@pytest.mark.parametrize(
    ("credentials", "fragment"),
    [
        pytest.param({}, "HTTP 401): no credential is set", id="missing"),
        pytest.param(
            {"APM_MARKETPLACE_TOKEN_CORP": "tok-corp-wrong-0123456789"},
            "HTTP 401) for the credential from APM_MARKETPLACE_TOKEN_CORP",
            id="wrong",
        ),
        pytest.param(
            {"APM_MARKETPLACE_TOKEN_CORP": _FORBIDDEN_TOKEN},
            "HTTP 403) for the credential from APM_MARKETPLACE_TOKEN_CORP",
            id="forbidden",
        ),
    ],
)
def test_denied_access_fails_with_the_variable_to_set(
    tmp_path: Path, apm_binary_path: Path, credentials: dict[str, str], fragment: str
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path)
    with _catalog_server(scenario.certificate, scenario.key, scenario.package_root) as server:
        env = scenario.env(server, **credentials)
        url = f"{server.base}/private/marketplace.json"
        result = scenario.run(env, "marketplace", "add", url, "--name", _NAME, step="denied")

    output = _output(result)
    assert result.returncode == 1, output
    assert f"Access to marketplace '{_NAME}' was denied (" in output
    assert fragment in output
    assert "APM_MARKETPLACE_TOKEN_CORP" in output
    registry = scenario.isolated.config_root / "marketplaces.json"
    assert not registry.exists() or _NAME not in registry.read_text(encoding="utf-8")
    for secret in credentials.values():
        assert secret not in output
    # The isolated HOME has a .netrc for 127.0.0.1; it is never sent.
    expected = [None] if not credentials else [f"Bearer {next(iter(credentials.values()))}"]
    assert [seen.authorization for seen in server.seen] == expected


@pytest.mark.parametrize("route", ["redir-cross", "redir-out"])
def test_out_of_binding_redirect_carries_no_credential(
    tmp_path: Path, apm_binary_path: Path, route: str
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path)
    with _catalog_server(scenario.certificate, scenario.key, scenario.package_root) as server:
        env = scenario.env(server, APM_MARKETPLACE_TOKEN_CORP=_TOKEN)
        url = f"{server.base}/{route}/marketplace.json"
        _expect_ok(scenario.run(env, "marketplace", "add", url, "--name", _NAME, step=route))

    hops = [(seen.path, seen.authorization) for seen in server.seen]
    assert hops == [
        (f"/{route}/marketplace.json", f"Bearer {_TOKEN}"),
        ("/public/marketplace.json", None),
    ]


def test_denied_refresh_keeps_the_cached_catalog_and_is_not_fresh(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path)
    with _catalog_server(scenario.certificate, scenario.key, scenario.package_root) as server:
        good = scenario.env(server, APM_MARKETPLACE_TOKEN_CORP=_TOKEN)
        url = f"{server.base}/private/marketplace.json"
        _expect_ok(scenario.run(good, "marketplace", "add", url, "--name", _NAME, step="seed"))
        cache = scenario.isolated.config_root / "cache" / "marketplace"
        before = {path.name: path.read_bytes() for path in cache.iterdir()}
        assert before

        revoked = scenario.env(server, APM_MARKETPLACE_TOKEN_CORP="tok-corp-revoked-0123456789")
        refresh = scenario.run(revoked, "marketplace", "update", _NAME, step="refresh")
        browse = scenario.run(revoked, "marketplace", "browse", _NAME, step="browse-denied")

    for result in (refresh, browse):
        output = _output(result)
        assert result.returncode == 1, output
        assert "was denied (HTTP 401)" in output
        assert "updated" not in output
        assert _PLUGIN not in output
    assert {path.name: path.read_bytes() for path in cache.iterdir()} == before


def test_malformed_token_is_redacted_in_verbose_output(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path)
    malformed = "tok-malformed secret-0123456789"
    with _catalog_server(scenario.certificate, scenario.key, scenario.package_root) as server:
        env = scenario.env(server, APM_MARKETPLACE_TOKEN_CORP=malformed)
        url = f"{server.base}/private/marketplace.json"
        result = scenario.run(
            env, "marketplace", "add", url, "--name", _NAME, "--verbose", step="malformed"
        )

    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 1, output
    assert "APM_MARKETPLACE_TOKEN_CORP is malformed" in output
    assert "secret-0123456789" not in output
    assert "tok-malformed" not in output
    assert server.seen == ()
