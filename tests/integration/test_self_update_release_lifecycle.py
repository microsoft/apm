"""Installed-binary lifecycle coverage for deterministic self-update releases."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_Route = tuple[int, str, str]
_SELF_UPDATE_ENV = (
    "APM_E2E_TESTS",
    "APM_INSTALLER_BASE_URL",
    "APM_INSTALL_DIR",
    "APM_NO_DIRECT_FALLBACK",
    "APM_RELEASE_BASE_URL",
    "APM_RELEASE_METADATA_URL",
    "APM_REPO",
    "APM_SELF_UPDATE_CHANNEL",
    "GITHUB_URL",
    "VERSION",
)


@dataclass(frozen=True)
class _FixtureServer:
    base_url: str
    requested_paths: list[str]


@contextmanager
def _serve(routes: Mapping[str, _Route]) -> Iterator[_FixtureServer]:
    requested_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested_paths.append(self.path)
            status, content_type, body = routes.get(
                self.path,
                (404, "text/plain", "not found"),
            )
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _FixtureServer(
            base_url=f"http://{host}:{port}",
            requested_paths=requested_paths,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _installer_script(*, exit_code: int = 0) -> str:
    if os.name == "nt":
        return "\n".join(
            (
                "$env:VERSION | Set-Content -NoNewline -Path "
                '(Join-Path $env:APM_INSTALL_DIR "observed-version.txt")',
                "$env:APM_RELEASE_BASE_URL | Set-Content -NoNewline -Path "
                '(Join-Path $env:APM_INSTALL_DIR "observed-release-base.txt")',
                f"exit {exit_code}",
            )
        )
    return "\n".join(
        (
            "#!/usr/bin/env bash",
            "set -eu",
            'printf "%s" "$VERSION" > "$APM_INSTALL_DIR/observed-version.txt"',
            'printf "%s" "${APM_RELEASE_BASE_URL:-}" '
            '> "$APM_INSTALL_DIR/observed-release-base.txt"',
            f"exit {exit_code}",
        )
    )


def _scenario(
    tmp_path: Path,
    *,
    name: str,
    server: _FixtureServer,
    overrides: Mapping[str, str],
) -> tuple[IsolatedApmEnvironment, dict[str, str], Path]:
    isolated = IsolatedApmEnvironment.create(
        tmp_path / name,
        base_env=dict(os.environ),
    )
    environment = isolated.subprocess_env()
    for env_name in _SELF_UPDATE_ENV:
        environment.pop(env_name, None)

    # IsolatedApmEnvironment denies all IP traffic by default. This scenario
    # permits only the loopback fixture URL asserted below.
    environment.pop("PYTHONPATH", None)
    install_dir = isolated.root / "installed"
    install_dir.mkdir()
    environment.update(
        {
            "APM_E2E_TESTS": "1",
            "APM_INSTALL_DIR": str(install_dir),
            "APM_REPO": "corp/apm",
            "GITHUB_URL": server.base_url,
            **overrides,
        }
    )
    return isolated, environment, install_dir


@pytest.mark.parametrize(
    ("channel", "metadata_path", "metadata", "version"),
    (
        (
            "stable",
            "/api/v3/repos/corp/apm/releases/latest",
            {"tag_name": "v91.0.0"},
            "91.0.0",
        ),
        (
            "prerelease",
            "/api/v3/repos/corp/apm/releases?per_page=5",
            [{"tag_name": "v92.0.0rc1", "draft": False, "prerelease": True}],
            "92.0.0rc1",
        ),
    ),
)
def test_selected_release_controls_installer_ref_and_version(
    tmp_path: Path,
    apm_binary_path: Path,
    channel: str,
    metadata_path: str,
    metadata: object,
    version: str,
) -> None:
    """Stable and prerelease discovery must drive identical installer inputs."""
    script_name = "install.ps1" if os.name == "nt" else "install.sh"
    installer_path = f"/corp/apm/raw/v{version}/{script_name}"
    routes = {
        metadata_path: (200, "application/json", json.dumps(metadata)),
        installer_path: (200, "text/plain", _installer_script()),
    }
    with _serve(routes) as server:
        isolated, environment, install_dir = _scenario(
            tmp_path,
            name=f"selected-{channel}",
            server=server,
            overrides={
                "APM_RELEASE_BASE_URL": f"{server.base_url}/assets",
                "APM_SELF_UPDATE_CHANNEL": channel,
            },
        )
        result = ApmLifecycleRunner((str(apm_binary_path),)).run(
            ("self-update",),
            scenario_id=f"self-update-{channel}-release",
            cwd=isolated.work_root,
            env=environment,
        )

    assert result.returncode == 0, result.stderr or result.stdout
    assert (install_dir / "observed-version.txt").read_text(encoding="utf-8") == f"v{version}"
    assert server.requested_paths == [metadata_path, installer_path]


def test_installer_mirror_remains_authoritative(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Mirror metadata and installer URLs must avoid custom GitHub routing."""
    script_name = "install.ps1" if os.name == "nt" else "install.sh"
    metadata_path = "/mirror/latest.json"
    installer_path = f"/mirror/installers/{script_name}"
    routes = {
        metadata_path: (200, "application/json", json.dumps({"tag_name": "v93.0.0"})),
        installer_path: (200, "text/plain", _installer_script()),
    }
    with _serve(routes) as server:
        isolated, environment, install_dir = _scenario(
            tmp_path,
            name="mirror-authority",
            server=server,
            overrides={
                "APM_INSTALLER_BASE_URL": f"{server.base_url}/mirror/installers",
                "APM_NO_DIRECT_FALLBACK": "1",
                "APM_RELEASE_BASE_URL": f"{server.base_url}/mirror/assets",
                "APM_RELEASE_METADATA_URL": f"{server.base_url}{metadata_path}",
            },
        )
        result = ApmLifecycleRunner((str(apm_binary_path),)).run(
            ("self-update",),
            scenario_id="self-update-mirror-authority",
            cwd=isolated.work_root,
            env=environment,
        )

    assert result.returncode == 0, result.stderr or result.stdout
    assert (install_dir / "observed-version.txt").read_text(encoding="utf-8") == "v93.0.0"
    assert (install_dir / "observed-release-base.txt").read_text(
        encoding="utf-8"
    ) == f"{server.base_url}/mirror/assets"
    assert server.requested_paths == [metadata_path, installer_path]


@pytest.mark.parametrize(
    ("installer_status", "installer_body", "scenario_id", "installer_started"),
    (
        (503, "mirror unavailable", "self-update-installer-network-error", False),
        (
            200,
            _installer_script(exit_code=7),
            "self-update-installer-subprocess-error",
            True,
        ),
    ),
)
def test_installer_failure_preserves_existing_binary_state(
    tmp_path: Path,
    apm_binary_path: Path,
    installer_status: int,
    installer_body: str,
    scenario_id: str,
    installer_started: bool,
) -> None:
    """Download and subprocess failures must not report success or replace state."""
    script_name = "install.ps1" if os.name == "nt" else "install.sh"
    metadata_path = "/mirror/latest.json"
    installer_path = f"/mirror/installers/{script_name}"
    routes = {
        metadata_path: (200, "application/json", json.dumps({"tag_name": "v94.0.0"})),
        installer_path: (installer_status, "text/plain", installer_body),
    }
    with _serve(routes) as server:
        isolated, environment, install_dir = _scenario(
            tmp_path,
            name=scenario_id,
            server=server,
            overrides={
                "APM_INSTALLER_BASE_URL": f"{server.base_url}/mirror/installers",
                "APM_NO_DIRECT_FALLBACK": "1",
                "APM_RELEASE_METADATA_URL": f"{server.base_url}{metadata_path}",
            },
        )
        existing_binary = install_dir / "apm"
        existing_binary.write_text("existing-binary", encoding="utf-8")
        result = ApmLifecycleRunner((str(apm_binary_path),)).run(
            ("self-update",),
            scenario_id=scenario_id,
            cwd=isolated.work_root,
            env=environment,
        )

    assert result.returncode == 1
    assert existing_binary.read_text(encoding="utf-8") == "existing-binary"
    observed_version = install_dir / "observed-version.txt"
    if installer_started:
        assert observed_version.read_text(encoding="utf-8") == "v94.0.0"
    else:
        assert not observed_version.exists()
    assert list(isolated.temp_root.iterdir()) == []
    assert server.requested_paths == [metadata_path, installer_path]
