"""Installed-binary lifecycle coverage for deterministic self-update releases."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

INSTALLER = Path(__file__).resolve().parents[2] / "install.sh"
_HISTORICAL_PROBE = (
    "apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm /usr/local/lib/apm/apm"
)
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_Route = tuple[int, str, str | bytes]
_SELF_UPDATE_ENV = (
    "APM_E2E_TESTS",
    "APM_INSTALLER_BASE_URL",
    "APM_INSTALL_DIR",
    "APM_LIB_DIR",
    "APM_NO_DIRECT_FALLBACK",
    "APM_RELEASE_BASE_URL",
    "APM_RELEASE_METADATA_URL",
    "APM_REPO",
    "APM_SELF_UPDATE_CHANNEL",
    "APM_SELF_UPDATE_SOURCE",
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
            payload = body if isinstance(body, bytes) else body.encode("utf-8")
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


def _native_platform_dir() -> str:
    system = os.uname().sysname
    machine = os.uname().machine
    if system == "Darwin":
        platform_name = "darwin"
    elif system == "Linux":
        platform_name = "linux"
    else:
        pytest.skip(f"unsupported Unix installer platform: {system}")
    if machine in {"arm64", "aarch64"}:
        arch = "arm64"
    elif machine == "x86_64":
        arch = "x86_64"
    else:
        pytest.skip(f"unsupported Unix installer architecture: {machine}")
    return f"apm-{platform_name}-{arch}"


def _native_archive_bytes(tmp_path: Path, *, version: str) -> bytes:
    platform_dir = _native_platform_dir()
    release_root = tmp_path / f"release-{version}"
    extracted = release_root / platform_dir
    extracted.mkdir(parents=True)
    binary = extracted / "apm"
    binary.write_text(
        "#!/bin/sh\n"
        f'if [ "${{1:-}}" = "--version" ]; then printf "apm {version}\\n"; exit 0; fi\n'
        'printf "apm updated fixture\\n"\n',
        encoding="ascii",
    )
    binary.chmod(0o755)
    (extracted / "VERSION").write_text(f"{version}\n", encoding="ascii")
    archive = tmp_path / f"{platform_dir}-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(extracted, arcname=platform_dir)
    return archive.read_bytes()


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


@pytest.mark.skipif(os.name == "nt", reason="Unix installer source-identity contract")
def test_off_path_self_update_passes_identity_and_persisted_destination(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A real off-PATH binary preserves its configured destination at installer launch."""
    metadata_path = "/mirror/latest.json"
    installer_path = "/mirror/installers/install.sh"
    installer = _installer_script() + (
        '\nprintf "%s" "$APM_SELF_UPDATE_SOURCE" > "$APM_INSTALL_DIR/observed-source.txt"\n'
    )
    # The shared script's final exit is useful to failure tests, but this fixture
    # must record identity after the release fields before it exits.
    installer = installer.replace("exit 0\n", "")
    routes = {
        metadata_path: (200, "application/json", json.dumps({"tag_name": "v95.0.0"})),
        installer_path: (200, "text/plain", installer),
    }
    with _serve(routes) as server:
        isolated, environment, install_dir = _scenario(
            tmp_path,
            name="off-path-identity",
            server=server,
            overrides={
                "APM_INSTALLER_BASE_URL": f"{server.base_url}/mirror/installers",
                "APM_NO_DIRECT_FALLBACK": "1",
                "APM_RELEASE_METADATA_URL": f"{server.base_url}{metadata_path}",
            },
        )
        environment.pop("APM_INSTALL_DIR")
        bundle = isolated.root / "custom/lib/apm"
        shutil.copytree(apm_binary_path.parent, bundle)
        binary = bundle / "apm"
        (install_dir / "apm").symlink_to(binary)
        runner = ApmLifecycleRunner((str(binary),))
        configured = runner.run(
            ("config", "set", "self-update.install-dir", str(install_dir)),
            scenario_id="self-update-persist-destination",
            cwd=isolated.work_root,
            env=environment,
        )
        assert configured.returncode == 0, configured.stderr or configured.stdout
        result = runner.run(
            ("self-update",),
            scenario_id="self-update-off-path-identity",
            cwd=isolated.work_root,
            env=environment,
        )

    assert result.returncode == 0, result.stderr or result.stdout
    assert Path((install_dir / "observed-source.txt").read_text(encoding="utf-8")) == binary
    assert "Please restart your terminal" not in result.stdout
    assert "run 'apm --version'" not in result.stdout
    assert (install_dir / "observed-version.txt").read_text(encoding="utf-8") == "v95.0.0"
    home = Path(environment["HOME"])
    assert not (home / ".local/bin/apm").exists()
    assert not (home / ".local/lib/apm").exists()
    assert server.requested_paths == [metadata_path, installer_path]


@pytest.mark.skipif(os.name == "nt", reason="Unix shell setup contract")
@pytest.mark.parametrize("modify_path", ["managed", "disabled"])
@pytest.mark.parametrize("shell_name", ["fish", "zsh"])
def test_self_update_with_real_installer_preserves_shell_setup_state(
    tmp_path: Path,
    shell_name: str,
    modify_path: str,
) -> None:
    """opt-out-self-update: production install.sh updates without profile enrollment."""
    shell_path = shutil.which(shell_name)
    if shell_path is None:
        pytest.skip(f"required shell is not available: {shell_name}")
    version = "96.0.0"
    metadata_path = "/mirror/latest.json"
    installer_path = "/mirror/installers/install.sh"
    asset_path = f"/assets/v{version}/{_native_platform_dir()}.tar.gz"
    routes = {
        metadata_path: (200, "application/json", json.dumps({"tag_name": f"v{version}"})),
        installer_path: (
            200,
            "text/plain",
            INSTALLER.read_text(encoding="ascii").replace(
                _HISTORICAL_PROBE,
                'apm_resolve_install_paths "$APM_FIXTURE_HISTORICAL_APM"',
            ),
        ),
        asset_path: (
            200,
            "application/gzip",
            _native_archive_bytes(tmp_path, version=version),
        ),
    }
    with _serve(routes) as server:
        isolated, environment, install_dir = _scenario(
            tmp_path,
            name="self-update-real-installer-shell-state",
            server=server,
            overrides={
                "APM_INSTALLER_BASE_URL": f"{server.base_url}/mirror/installers",
                "APM_LIB_DIR": str(tmp_path / "self-update-lib/apm"),
                "APM_NO_DIRECT_FALLBACK": "1",
                "APM_NO_MODIFY_PATH": "0",
                "APM_RELEASE_BASE_URL": f"{server.base_url}/assets",
                "APM_RELEASE_METADATA_URL": f"{server.base_url}{metadata_path}",
                "APM_FIXTURE_HISTORICAL_APM": str(tmp_path / "missing-apm"),
            },
        )
        environment["SHELL"] = shell_path
        lib_dir = Path(environment["APM_LIB_DIR"])
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        lib_dir.mkdir(parents=True)
        (lib_dir / "apm").write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            f"sys.path.insert(0, {str(INSTALLER.parent / 'src')!r})\n"
            "from apm_cli.cli import main\n"
            "main()\n",
            encoding="ascii",
        )
        (lib_dir / "apm").chmod(0o755)
        (lib_dir / ".apm-installed").touch()
        launcher = install_dir / "apm"
        launcher.symlink_to(lib_dir / "apm")
        hook_dir = Path(environment["HOME"]) / ".apm/shell"
        hook_dir.mkdir(parents=True)
        hook = hook_dir / "env"
        hook.write_text("# existing managed hook\n", encoding="ascii")
        profile = Path(environment["HOME"]) / ".bash_profile"
        profile.write_text(
            "# user profile\n# >>> apm shell setup >>>\n. existing-hook\n# <<< apm shell setup <<<\n",
            encoding="ascii",
        )
        if modify_path == "disabled":
            hook.unlink()
            hook_dir.rmdir()
            profile.unlink()
        receipt = lib_dir / ".apm-shell-setup"
        receipt.write_text(
            "\n".join(
                [
                    "version=1",
                    "owner=native",
                    f"selected_bin={install_dir}",
                    f"hook_dir={hook_dir if modify_path == 'managed' else 'none'}",
                    f"hook_kind={'posix' if modify_path == 'managed' else 'none'}",
                    f"hook_digest={'fixture-digest' if modify_path == 'managed' else 'none'}",
                    f"modify_path={modify_path}",
                    "",
                ]
            ),
            encoding="ascii",
        )
        before_profile = profile.read_bytes() if profile.exists() else None
        before_hook = hook.read_bytes() if hook.exists() else None
        before_receipt = receipt.read_bytes()
        result = ApmLifecycleRunner((str(lib_dir / "apm"),)).run(
            ("self-update",),
            scenario_id="self-update-real-installer-shell-state",
            cwd=isolated.work_root,
            env=environment,
        )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "Self-update leaves existing shell PATH setup unchanged." in result.stdout
    assert (profile.read_bytes() if profile.exists() else None) == before_profile
    assert (hook.read_bytes() if hook.exists() else None) == before_hook
    home = Path(environment["HOME"])
    assert not (home / ".zshrc").exists()
    assert not (home / ".config/fish/conf.d/apm.fish").exists()
    assert (lib_dir / ".apm-shell-setup").read_bytes() == before_receipt
    assert (lib_dir / "apm").read_text(encoding="ascii").startswith("#!/bin/sh")
    assert server.requested_paths == [metadata_path, installer_path, asset_path]
