"""Shared fixtures for hermetic Codex runtime setup script tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import stat
import subprocess
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = PROJECT_ROOT / "scripts" / "runtime" / "setup-codex.sh"
TEST_VERSION = "rust-v9.9.9"


def codex_platform_name() -> str | None:
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


def write_fake_archive(archive_path: Path) -> None:
    codex_payload = b"#!/bin/sh\nprintf 'codex test version\\n'\n"

    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo(name="codex")
        info.mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        info.size = len(codex_payload)
        archive.addfile(info, io.BytesIO(codex_payload))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def write_release_metadata(
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


def write_fake_curl(script_path: Path) -> None:
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
            if [ -n "${EXPECTED_AUTH_HEADER:-}" ] && [ "$2" != "$EXPECTED_AUTH_HEADER" ]; then
                echo "unexpected authorization header" >&2
                exit 1
            fi
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


def run_setup(
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
    write_fake_curl(bin_dir / "curl")

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
    env.pop("GH_TOKEN", None)
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
