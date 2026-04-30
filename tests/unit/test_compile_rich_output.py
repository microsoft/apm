from __future__ import annotations

import subprocess
import sys
from pathlib import Path  # noqa: F401

from ..utils.constitution_fixtures import DEFAULT_CONSTITUTION, temp_project_with_constitution

CLI = [sys.executable, "-m", "apm_cli.cli", "compile", "--single-agents"]


def test_rich_output_contains_table_and_status():
    with temp_project_with_constitution(constitution_text=DEFAULT_CONSTITUTION) as proj:
        proc = subprocess.run(CLI, cwd=str(proj), capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 0
        out = proc.stdout + proc.stderr
        # Table title or fallback line
        assert "Constitution" in out
        assert "Hash:" in out
