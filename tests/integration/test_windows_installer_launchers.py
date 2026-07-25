"""End-to-end regression coverage for Windows installer launchers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_windows,
]

ROOT = Path(__file__).resolve().parents[2]


def test_windows_installer_exposes_stable_executable() -> None:
    """Install APM and launch bare ``apm`` through Windows process boundaries."""
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "windows" / "test-install-script.ps1"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("All install.ps1 integration tests passed.")
