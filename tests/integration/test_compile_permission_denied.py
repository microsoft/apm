from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ..utils.constitution_fixtures import DEFAULT_CONSTITUTION, temp_project_with_constitution

CLI = [sys.executable, "-m", "apm_cli.cli", "compile", "--single-agents"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows handles read-only directories differently"
)
def test_permission_denied_graceful(tmp_path: Path):
    # Use temp project with constitution to force write
    with temp_project_with_constitution(constitution_text=DEFAULT_CONSTITUTION) as proj:
        agents = Path(proj) / "AGENTS.md"
        agents.write_text("placeholder", encoding="utf-8")

        # Make the directory unwriteable (this prevents tempfile creation during atomic write)
        proj_path = Path(proj)
        original_mode = proj_path.stat().st_mode
        proj_path.chmod(stat.S_IREAD | stat.S_IEXEC)  # read + execute only, no write

        try:
            proc = subprocess.run(CLI, cwd=str(proj), capture_output=True, text=True)
            # Expect non-zero exit due to write failure
            assert proc.returncode != 0
            combined = proc.stdout + proc.stderr
            assert "Failed to write" in combined or "permission" in combined.lower()
        finally:
            # Restore permissions so context manager can cleanup
            proj_path.chmod(original_mode)
