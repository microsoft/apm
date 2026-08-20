"""Integration proof for Codex runtime archive checksum enforcement."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.utils.runtime_setup_codex import (
    codex_platform_name,
    run_setup,
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
