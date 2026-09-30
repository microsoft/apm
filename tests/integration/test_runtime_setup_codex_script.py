"""Integration proof for Codex runtime archive checksum enforcement."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests.utils.runtime_setup_codex import (
    TEST_VERSION,
    codex_platform_name,
    run_setup,
    sha256,
    write_fake_archive,
    write_release_metadata,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Bash scripts not available")


@pytest.fixture
def codex_platform() -> str:
    platform_name = codex_platform_name()
    if platform_name is None:
        pytest.skip("Unsupported platform for setup-codex.sh test")
    return platform_name


def test_setup_codex_refuses_tampered_archive_before_extracting(
    tmp_path: Path, codex_platform: str
) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    write_fake_archive(tarball)
    write_release_metadata(metadata, asset_name=asset_name, digest="0" * 64)

    result = run_setup(tmp_path, release_json=metadata, tarball=tarball)

    assert result.returncode != 0
    assert "Checksum verification failed" in result.stdout + result.stderr
    assert not (tmp_path / "home" / ".apm" / "runtimes" / "codex").exists()


@contextmanager
def _archive_server(
    statuses: tuple[int, ...], payload: bytes, *, truncated: bool = False
) -> Iterator[tuple[str, list[str]]]:
    """Serve only a local archive, recording actual HTTP attempts."""
    requested_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = statuses[min(len(requested_paths), len(statuses) - 1)]
            requested_paths.append(self.path)
            body = payload if status == 200 else b"Controlled HTTP failure\n"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body) + (10 if truncated else 0)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/archive", requested_paths
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _http_curl_shim(path: Path, real_curl: str, archive_name: str) -> None:
    """Route the exact archive URL locally without changing curl's flags."""
    upstream = f"https://github.com/openai/codex/releases/download/{TEST_VERSION}/{archive_name}"
    metadata = f"https://api.github.com/repos/openai/codex/releases/tags/{TEST_VERSION}"
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[-1] == {metadata!r}:
    sys.stdout.write(Path(os.environ["FAKE_RELEASE_JSON"]).read_text())
elif {upstream!r} in args:
    args[args.index({upstream!r})] = os.environ["TEST_ARCHIVE_URL"]
    output = args[args.index("-o") + 1]
    Path(os.environ["TEST_DOWNLOAD_TRACE"]).write_text(json.dumps(output))
    os.execv({real_curl!r}, [{real_curl!r}, "--disable", "--noproxy", "*", *args])
else:
    sys.exit("Unexpected curl request; external network is forbidden")
""",
        encoding="ascii",
    )
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("statuses", "digest_matches", "truncated", "attempts", "error"),
    [
        pytest.param((200,), True, False, 1, None, id="valid-200"),
        pytest.param((503, 200), True, False, 2, None, id="503-then-200"),
        pytest.param((503,), True, False, 4, "Failed to download", id="exhausted-503"),
        pytest.param((404,), True, False, 1, "Failed to download", id="non-retried-404"),
        pytest.param((200,), False, False, 1, "Checksum verification failed", id="200-mismatch"),
        pytest.param((200,), True, True, 1, "Failed to download", id="truncated-200"),
    ],
)
@pytest.mark.parametrize("preinstalled", [False, True], ids=["fresh", "installed"])
def test_setup_codex_http_download(
    tmp_path: Path,
    codex_platform: str,
    statuses: tuple[int, ...],
    digest_matches: bool,
    truncated: bool,
    attempts: int,
    error: str | None,
    preinstalled: bool,
) -> None:
    """Exercise real curl through setup, never executing an upstream binary."""
    real_curl = shutil.which("curl")
    real_tar = shutil.which("tar")
    assert real_curl is not None
    assert real_tar is not None
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"
    write_fake_archive(tarball)
    write_release_metadata(
        metadata, asset_name=asset_name, digest=sha256(tarball) if digest_matches else "0" * 64
    )
    curl_script = tmp_path / "http-curl"
    _http_curl_shim(curl_script, real_curl, asset_name)
    extract_marker = tmp_path / "extracted"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tar_spy = bin_dir / "tar"
    tar_spy.write_text(
        f"#!/bin/sh\n"
        f"printf 'extract\\n' >> {shlex.quote(str(extract_marker))}\n"
        f'exec {shlex.quote(real_tar)} "$@"\n',
        encoding="ascii",
    )
    tar_spy.chmod(0o755)
    installed = tmp_path / "home" / ".apm" / "runtimes" / "codex"
    config = tmp_path / "home" / ".codex" / "config.toml"
    if preinstalled:
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b"existing runtime\n")
        installed.chmod(0o755)
        config.parent.mkdir()
        config.write_bytes(b"existing configuration\n")
    download_trace = tmp_path / "download.json"

    with _archive_server(statuses, tarball.read_bytes(), truncated=truncated) as (url, paths):
        result = run_setup(
            tmp_path,
            release_json=metadata,
            tarball=tarball,
            curl_script=curl_script,
            env_updates={
                "TEST_ARCHIVE_URL": url,
                "TEST_DOWNLOAD_TRACE": str(download_trace),
            },
        )

    output = result.stdout + result.stderr
    assert [urlparse(path).path for path in paths] == ["/archive"] * attempts
    destination = Path(json.loads(download_trace.read_text()))
    assert destination.parent.parent == tmp_path
    assert not destination.exists()
    assert not destination.parent.exists()
    assert not list(tmp_path.glob("apm-codex-install.*"))
    if error is None:
        assert result.returncode == 0, output
        assert "Verified Codex archive checksum" in output
        assert extract_marker.read_text() == "extract\n"
        assert installed.read_bytes() == b"#!/bin/sh\nprintf 'codex test version\\n'\n"
        assert os.access(installed, os.X_OK)
    else:
        assert result.returncode != 0, output
        assert error in output
        assert not extract_marker.exists()
        assert "Extracting Codex binary" not in output
        if error == "Failed to download":
            assert "Checksum verification failed" not in output
            assert "Check your connection and retry" in output
        if preinstalled:
            assert installed.read_bytes() == b"existing runtime\n"
            assert os.access(installed, os.X_OK)
        else:
            assert not installed.exists()
    if preinstalled:
        assert config.read_bytes() == b"existing configuration\n"
    else:
        assert not config.exists()
