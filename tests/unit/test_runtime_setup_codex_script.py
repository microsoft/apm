"""Hermetic tests for the Unix Codex runtime setup script."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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


def test_setup_codex_verifies_checksum_before_extracting(
    tmp_path: Path, codex_platform: str
) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    write_fake_archive(tarball)
    write_release_metadata(metadata, asset_name=asset_name, digest=sha256(tarball))

    result = run_setup(tmp_path, release_json=metadata, tarball=tarball)

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

    write_fake_archive(tarball)
    write_release_metadata(metadata, asset_name=asset_name, digest="0" * 64)

    result = run_setup(tmp_path, release_json=metadata, tarball=tarball)
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

    write_fake_archive(tarball)
    write_release_metadata(metadata, asset_name=asset_name, digest=sha256(tarball))
    monkeypatch.delenv("PATH", raising=False)

    result = run_setup(tmp_path, release_json=metadata, tarball=tarball)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verified Codex archive checksum" in result.stdout


def test_setup_codex_reads_minified_release_metadata(tmp_path: Path, codex_platform: str) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    write_fake_archive(tarball)
    metadata.write_text(
        json.dumps(
            {
                "tag_name": TEST_VERSION,
                "assets": [
                    {
                        "name": asset_name,
                        "digest": f"sha256:{sha256(tarball)}",
                        "browser_download_url": (
                            "https://github.com/openai/codex/releases/download/"
                            f"{TEST_VERSION}/{asset_name}"
                        ),
                    }
                ],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    result = run_setup(tmp_path, release_json=metadata, tarball=tarball)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verified Codex archive checksum" in result.stdout


def test_setup_codex_uses_token_for_metadata_fetch(tmp_path: Path, codex_platform: str) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    write_fake_archive(tarball)
    write_release_metadata(metadata, asset_name=asset_name, digest=sha256(tarball))

    result = run_setup(
        tmp_path,
        release_json=metadata,
        tarball=tarball,
        env_updates={
            "GITHUB_TOKEN": "ghp_test_token",
            "EXPECTED_AUTH_HEADER": "Authorization: Bearer ghp_test_token",
        },
    )
    output = result.stdout + result.stderr
    curl_log = (tmp_path / "curl.log").read_text(encoding="utf-8")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "api auth=yes" in curl_log
    assert "ghp_test_token" not in output
    assert "ghp_test_token" not in curl_log


def test_setup_codex_uses_gh_token_for_metadata_fetch(tmp_path: Path, codex_platform: str) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    write_fake_archive(tarball)
    write_release_metadata(metadata, asset_name=asset_name, digest=sha256(tarball))

    result = run_setup(
        tmp_path,
        release_json=metadata,
        tarball=tarball,
        env_updates={
            "GH_TOKEN": "ghp_cli_token",
            "EXPECTED_AUTH_HEADER": "Authorization: Bearer ghp_cli_token",
        },
    )
    output = result.stdout + result.stderr
    curl_log = (tmp_path / "curl.log").read_text(encoding="utf-8")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "api auth=yes" in curl_log
    assert "ghp_cli_token" not in output
    assert "ghp_cli_token" not in curl_log


def test_setup_codex_aborts_when_digest_absent_from_metadata(
    tmp_path: Path, codex_platform: str
) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    write_fake_archive(tarball)
    write_release_metadata(
        metadata,
        asset_name="codex-unrelated-platform.tar.gz",
        digest=sha256(tarball),
    )

    result = run_setup(tmp_path, release_json=metadata, tarball=tarball)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert f"Failed to find checksum metadata for {asset_name}." in output
    assert "download auth=" not in (tmp_path / "curl.log").read_text(encoding="utf-8")
    assert not (tmp_path / "home" / ".apm" / "runtimes" / "codex").exists()


def test_setup_codex_rejects_malformed_digest_format(tmp_path: Path, codex_platform: str) -> None:
    asset_name = f"codex-{codex_platform}.tar.gz"
    tarball = tmp_path / asset_name
    metadata = tmp_path / "release.json"

    write_fake_archive(tarball)
    write_release_metadata(metadata, asset_name=asset_name, digest="notahex_short")

    result = run_setup(tmp_path, release_json=metadata, tarball=tarball)
    output = result.stdout + result.stderr

    assert result.returncode != 0
    assert "did not include a valid SHA-256 digest" in output
    assert not (tmp_path / "home" / ".apm" / "runtimes" / "codex").exists()
