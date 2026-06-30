"""Hermetic tests for the Unix Codex runtime setup script."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Bash scripts not available")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = PROJECT_ROOT / "scripts" / "runtime" / "setup-codex.sh"
TEST_VERSION = "rust-v9.9.9"


def _codex_platform() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin":
        if machine == "arm64":
            return "aarch64-apple-darwin"
        if machine == "x86_64":
            return "x86_64-apple-darwin"

    if system == "linux":
        if machine in {"x86_64", "amd64"}:
            return "x86_64-unknown-linux-gnu"
        if machine in {"aarch64", "arm64"}:
            return "aarch64-unknown-linux-gnu"

    return None


def _write_fake_archive(archive_path: Path) -> None:
    codex_payload = b"#!/bin/sh\nprintf 'codex test version\\n'\n"

    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo(name="codex")
        info.mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        info.size = len(codex_payload)
        archive.addfile(info, io.BytesIO(codex_payload))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_release_metadata(
    metadata_path: Path, *, asset_name: str, digest: str, version: str = TEST_VERSION
) -> None:
    metadata = {
        "tag_name": version,
        "assets": [
            {
                "name": asset_name,
                "digest": f"sha256:{digest}",
                "browser_download_url": (
                    f"https://github.com/openai/codex/releases/download/{version}/{asset_name}"
                ),
            }
        ],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def _write_fake_curl(script_path: Path) -> None:
    script_path.write_text(
        """#!/bin/sh
set -eu

auth="no"
output=""
url=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        -o|--output)
            output="$2"
            shift 2
            ;;
        -H|--header)
            case "$2" in
                Authorization:*)
                    auth="yes"
                    ;;
            esac
            shift 2
            ;;
        *)
            case "$1" in
                -*)
                    shift
                    ;;
                *)
                    url="$1"
                    shift
                    ;;
            esac
            ;;
    esac
done

case "$url" in
    https://api.github.com/repos/*/releases/*)
        printf 'api auth=%s url=%s\\n' "$auth" "$url" >> "$FAKE_CURL_LOG"
        cat "$FAKE_RELEASE_JSON"
        ;;
    https://github.com/*/releases/download/*)
        printf 'download auth=%s url=%s\\n' "$auth" "$url" >> "$FAKE_CURL_LOG"
        if [ -z "$output" ]; then
            echo "missing output path" >&2
            exit 1
        fi
        cp "$FAKE_TARBALL" "$output"
        ;;
    *)
        echo "unexpected url: $url" >&2
        exit 1
        ;;
esac
""",
        encoding="utf-8",
    )
    script_path.chmod(0o755)


def _run_setup(
    tmp_path: Path,
    *,
    release_json: Path,
    tarball: Path,
    env_updates: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home_dir = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home_dir.mkdir()
    bin_dir.mkdir()
    _write_fake_curl(bin_dir / "curl")

    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', os.defpath)}"
    env["SHELL"] = "/bin/bash"
    env["TMPDIR"] = str(tmp_path)
    env["FAKE_CURL_LOG"] = str(tmp_path / "curl.log")
    env["FAKE_RELEASE_JSON"] = str(release_json)
    env["FAKE_TARBALL"] = str(tarball)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITHUB_APM_PAT", None)
    if env_updates is not None:
        env.update(env_updates)

    return subprocess.run(
        ["bash", str(SETUP_SCRIPT), "--vanilla", TEST_VERSION],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def codex_platform() -> str:
    platform_name = _codex_platform()
    if platform_name is None:
        pytest.skip("Unsupported platform for setup-codex.sh test")
    return platform_name


def test_setup_codex_verifies_checksum_before_extracting(
    tmp_path: Path, codex_platform: str
) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    _write_fake_archive(tarball)
    _write_release_metadata(metadata, asset_name=asset_name, digest=_sha256(tarball))

    result = _run_setup(tmp_path, release_json=metadata, tarball=tarball)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verified Codex archive checksum" in result.stdout

    codex_binary = tmp_path / "home" / ".apm" / "runtimes" / "codex"
    assert codex_binary.exists()
    assert os.access(codex_binary, os.X_OK)
    assert not (tmp_path / "home" / ".codex" / "config.toml").exists()


def test_setup_codex_rejects_mismatched_checksum(tmp_path: Path, codex_platform: str) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    _write_fake_archive(tarball)
    _write_release_metadata(metadata, asset_name=asset_name, digest="0" * 64)

    result = _run_setup(tmp_path, release_json=metadata, tarball=tarball)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "Checksum verification failed" in output
    assert not (tmp_path / "home" / ".apm" / "runtimes" / "codex").exists()


def test_setup_codex_runs_when_path_is_unset(
    tmp_path: Path, codex_platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    _write_fake_archive(tarball)
    _write_release_metadata(metadata, asset_name=asset_name, digest=_sha256(tarball))
    monkeypatch.delenv("PATH", raising=False)

    result = _run_setup(tmp_path, release_json=metadata, tarball=tarball)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verified Codex archive checksum" in result.stdout


def test_setup_codex_uses_token_for_metadata_fetch(tmp_path: Path, codex_platform: str) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    _write_fake_archive(tarball)
    _write_release_metadata(metadata, asset_name=asset_name, digest=_sha256(tarball))

    result = _run_setup(
        tmp_path,
        release_json=metadata,
        tarball=tarball,
        env_updates={"GITHUB_TOKEN": "ghp_test_token"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "api auth=yes" in (tmp_path / "curl.log").read_text(encoding="utf-8")


def test_setup_codex_aborts_when_digest_absent_from_metadata(
    tmp_path: Path, codex_platform: str
) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    _write_fake_archive(tarball)
    _write_release_metadata(
        metadata,
        asset_name="codex-unrelated-platform.tar.gz",
        digest=_sha256(tarball),
    )

    result = _run_setup(tmp_path, release_json=metadata, tarball=tarball)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert f"Failed to find checksum metadata for {asset_name}." in output
    assert "download auth=" not in (tmp_path / "curl.log").read_text(encoding="utf-8")
    assert not (tmp_path / "home" / ".apm" / "runtimes" / "codex").exists()


def test_setup_codex_rejects_malformed_digest_format(tmp_path: Path, codex_platform: str) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    _write_fake_archive(tarball)
    _write_release_metadata(metadata, asset_name=asset_name, digest="notahex_short")

    result = _run_setup(tmp_path, release_json=metadata, tarball=tarball)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "did not include a valid SHA-256 digest" in output
    assert not (tmp_path / "home" / ".apm" / "runtimes" / "codex").exists()
