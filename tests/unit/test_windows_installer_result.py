"""Executable contracts for production installer JUnit validation."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RESULT_SCRIPT = ROOT / "scripts/windows/validate-installer-result.ps1"


@pytest.mark.parametrize(
    ("cases", "exit_code", "accepted"),
    [
        ('<testcase name="installer"/>', 0, True),
        ('<testcase name="installer"><skipped/></testcase>', 0, False),
        ("", 0, False),
        ('<testcase name="installer"><failure/></testcase>', 0, False),
        ('<testcase name="installer"><failure/></testcase>', 1, True),
    ],
)
def test_installer_outcome_requires_executed_cases(
    tmp_path: Path, cases: str, exit_code: int, accepted: bool
) -> None:
    """A green pytest exit with skipped assertions is not installer evidence."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    report = tmp_path / "junit.xml"
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(RESULT_SCRIPT),
            "-ValidateInstallerJUnit",
            str(report),
            "-InstallerExitCode",
            str(exit_code),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (completed.returncode == 0) is accepted, completed.stdout + completed.stderr
    if accepted:
        assert json.loads(completed.stdout)["skipped"] == 0
