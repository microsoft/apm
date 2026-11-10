"""Installed-CLI contract: PROXY_REGISTRY_ONLY never enumerates the source host.

Issue #3448. With ``PROXY_REGISTRY_ONLY=1`` the ref and version enumeration
paths (``apm view <pkg> versions``, semver range resolution in
``apm install``, and ref-to-SHA lookup via the GitHub commits API) must not
contact a direct VCS host. Exact pins keep resolving through the proxy.

Each scenario runs the real CLI in an isolated environment with two
observation seams:

* a ``git`` shim first on ``PATH`` that records and fails every network
  subcommand (``ls-remote``, ``clone``, ``fetch``) and delegates the rest;
* a loopback HTTP forward proxy (``HTTP_PROXY`` / ``HTTPS_PROXY``) that
  serves the registry-proxy archive for the fake proxy host and records
  -- then refuses -- every other request, including ``CONNECT`` tunnels to
  ``api.github.com``.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import threading
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.deps.lockfile import LockFile
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX git shim"),
]

_PROXY_HOST = "registry-proxy.example.invalid"
_PROXY_URL = f"http://{_PROXY_HOST}/artifactory/github"
_PACKAGE = "acme/pkg"


def _package_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pkg-1.0.0/", "")
        archive.writestr("pkg-1.0.0/apm.yml", "name: pkg\nversion: 1.0.0\n")
        archive.writestr(
            "pkg-1.0.0/.apm/instructions/guide.instructions.md",
            "---\napplyTo: '**'\n---\nRegistry-only fixture.\n",
        )
    return buffer.getvalue()


class _RecordingProxy(ThreadingHTTPServer):
    archive: bytes
    requests: list[tuple[str, str]]


class _RecordingHandler(BaseHTTPRequestHandler):
    server: _RecordingProxy

    def _handle(self) -> None:
        self.server.requests.append((self.command, self.path))
        target = urlparse(self.path)
        if self.command == "GET" and target.hostname == _PROXY_HOST:
            body = self.server.archive
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_HEAD = do_POST = do_CONNECT = _handle

    def log_message(self, fmt: str, *args: object) -> None:
        return


@contextmanager
def _recording_proxy() -> Iterator[_RecordingProxy]:
    server = _RecordingProxy(("127.0.0.1", 0), _RecordingHandler)
    server.archive = _package_zip()
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@dataclass
class _Scenario:
    project: Path
    git_log: Path
    proxy: _RecordingProxy
    result: CommandResult

    @property
    def git_network_calls(self) -> list[str]:
        if not self.git_log.exists():
            return []
        return self.git_log.read_text(encoding="utf-8").splitlines()

    @property
    def upstream_requests(self) -> list[tuple[str, str]]:
        return [
            (method, target)
            for method, target in self.proxy.requests
            if urlparse(target).hostname != _PROXY_HOST
        ]

    @property
    def output(self) -> str:
        return " ".join((self.result.stdout + self.result.stderr).split())


def _install_git_shim(bin_dir: Path, git_log: Path) -> None:
    real_git = shutil.which("git")
    assert real_git is not None, "git is required for this contract"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        f'    ls-remote|clone|fetch) echo "$@" >> "{git_log}"; exit 128;;\n'
        "  esac\n"
        "done\n"
        f'exec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _run(
    tmp_path: Path,
    apm_binary_path: Path,
    args: tuple[str, ...],
    *,
    registry_only: bool,
    dependency_ref: str | None = None,
) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    project = isolated.work_root / "consumer"
    project.mkdir()
    if dependency_ref is not None:
        (project / "apm.yml").write_text(
            "name: consumer\nversion: 0.0.1\n"
            f"dependencies:\n  apm:\n    - {_PACKAGE}#{dependency_ref}\n",
            encoding="utf-8",
        )
    git_log = isolated.root / "git-network.log"
    bin_dir = isolated.root / "shim-bin"
    _install_git_shim(bin_dir, git_log)

    with _recording_proxy() as proxy:
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        overrides = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PROXY_REGISTRY_URL": _PROXY_URL,
            "PROXY_REGISTRY_ALLOW_HTTP": "1",
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "NO_PROXY": "",
            "APM_E2E_TESTS": "1",
        }
        if registry_only:
            overrides["PROXY_REGISTRY_ONLY"] = "1"
        environment = isolated.subprocess_env(overrides=overrides)
        if not registry_only:
            environment.pop("PROXY_REGISTRY_ONLY", None)
            environment.pop("ARTIFACTORY_ONLY", None)
        environment["APM_TEST_LOOPBACK_PORTS"] = str(proxy.server_port)
        result = ApmLifecycleRunner((str(apm_binary_path),)).run(
            args, scenario_id="registry-only-enumeration", cwd=project, env=environment
        )
    return _Scenario(project=project, git_log=git_log, proxy=proxy, result=result)


def test_view_versions_aborts_without_contacting_source_host(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _run(tmp_path, apm_binary_path, ("view", _PACKAGE, "versions"), registry_only=True)

    assert scenario.result.returncode == 1, scenario.output
    assert "registry-only mode" in scenario.output
    assert "unset PROXY_REGISTRY_ONLY" in scenario.output
    assert scenario.git_network_calls == []
    assert scenario.upstream_requests == []


def test_view_versions_lists_from_source_host_when_mode_is_off(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Sentinel: proves the git shim observes the enumeration it guards."""
    scenario = _run(tmp_path, apm_binary_path, ("view", _PACKAGE, "versions"), registry_only=False)

    assert any("ls-remote" in call for call in scenario.git_network_calls), scenario.output


def test_install_range_fails_with_remedy_without_contacting_source_host(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _run(
        tmp_path,
        apm_binary_path,
        ("install", "--target", "copilot", "--no-policy"),
        registry_only=True,
        dependency_ref="^1.0.0",
    )

    assert scenario.result.returncode != 0, scenario.output
    assert "Cannot resolve the range '^1.0.0'" in scenario.output
    assert "unset PROXY_REGISTRY_ONLY" in scenario.output
    assert scenario.git_network_calls == []
    assert scenario.upstream_requests == []
    assert not (scenario.project / "apm.lock.yaml").exists()


def test_install_exact_pin_locks_through_proxy_without_commits_api(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _run(
        tmp_path,
        apm_binary_path,
        ("install", "--target", "copilot", "--no-policy"),
        registry_only=True,
        dependency_ref="1.0.0",
    )

    assert scenario.result.returncode == 0, scenario.output
    assert scenario.git_network_calls == []
    assert scenario.upstream_requests == []
    proxied = [urlparse(target) for _method, target in scenario.proxy.requests]
    assert proxied, "the archive must be fetched through the registry proxy"
    assert {parsed.hostname for parsed in proxied} == {_PROXY_HOST}
    lockfile = LockFile.read(scenario.project / "apm.lock.yaml")
    assert lockfile is not None
    locked = lockfile.get_dependency(_PACKAGE)
    assert locked is not None
    assert locked.resolved_ref == "1.0.0"
    assert locked.host == _PROXY_HOST


def test_install_exact_pin_lists_source_tags_when_mode_is_off(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Sentinel: without the mode an exact pin resolves from source-host tags."""
    scenario = _run(
        tmp_path,
        apm_binary_path,
        ("install", "--target", "copilot", "--no-policy"),
        registry_only=False,
        dependency_ref="1.0.0",
    )

    assert any("ls-remote" in call for call in scenario.git_network_calls), scenario.output
