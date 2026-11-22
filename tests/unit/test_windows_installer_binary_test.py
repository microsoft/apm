"""Behavioral coverage for install.ps1's staged-binary smoke test (#3521).

Windows PowerShell 5.1 turns the first native stderr line into a terminating
error under ``$ErrorActionPreference = "Stop"``. The installer must still
report the full combined output of a failed ``apm.exe --version`` run.

The extracted ``Invoke-StagedBinaryTest`` function runs under every available
PowerShell host. On Windows CI that includes ``powershell.exe`` (5.1), which
reproduces the truncation; ``pwsh`` covers the line-preserving formatting.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.windows_compat

ROOT = Path(__file__).resolve().parents[2]
TRACEBACK_LINES = (
    "Traceback (most recent call last):",
    'File "apm.py", line 1, in main',
    "ImportError: DLL load failed while importing _ssl",
)
POWERSHELL_HOSTS = ("powershell", "pwsh")


def _extract_function(name: str) -> str:
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^function {re.escape(name)} \{{.*?^\}}", text)
    assert match is not None, f"{name} not found in install.ps1"
    return match.group(0)


def _write_fake_binary(directory: Path, stdout_lines: tuple[str, ...], stderr_lines, code: int):
    if sys.platform == "win32":
        path = directory / "fake-apm.cmd"
        body = ["@echo off"]
        body += [f"echo {line}" for line in stdout_lines]
        body += [f"1>&2 echo {line}" for line in stderr_lines]
        body.append(f"exit /b {code}")
        path.write_text("\r\n".join(body) + "\r\n", encoding="ascii")
        return path
    path = directory / "fake-apm"
    body = ["#!/bin/sh"]
    body += [f"printf '%s\\n' '{line}'" for line in stdout_lines]
    body += [f"printf '%s\\n' '{line}' >&2" for line in stderr_lines]
    body.append(f"exit {code}")
    path.write_text("\n".join(body) + "\n", encoding="ascii")
    path.chmod(0o755)
    return path


def _run_binary_test(host: str, tmp_path: Path, binary: Path) -> dict[str, str]:
    executable = shutil.which(host)
    if executable is None:
        pytest.skip(f"{host} is not available")
    function = _extract_function("Invoke-StagedBinaryTest")
    literal_path = str(binary).replace("'", "''")
    script = tmp_path / "run-binary-test.ps1"
    script.write_text(
        "\n".join(
            [
                '$ErrorActionPreference = "Stop"',
                function,
                f"$result = Invoke-StagedBinaryTest -Path '{literal_path}' -Arguments @('--version')",
                'Write-Output "OUTPUT-BEGIN"',
                "Write-Output $result.Output",
                'Write-Output "OUTPUT-END"',
                'Write-Output "FAILURE-BEGIN"',
                "Write-Output $result.Failure",
                'Write-Output "FAILURE-END"',
            ]
        )
        + "\n",
        encoding="ascii",
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.rstrip() for line in result.stdout.splitlines()]

    def _section(name: str) -> str:
        start = lines.index(f"{name}-BEGIN") + 1
        end = lines.index(f"{name}-END")
        return "\n".join(line for line in lines[start:end] if line)

    return {"output": _section("OUTPUT"), "failure": _section("FAILURE")}


@pytest.mark.parametrize("host", POWERSHELL_HOSTS)
def test_staged_binary_failure_reports_full_multiline_traceback(host: str, tmp_path: Path):
    binary = _write_fake_binary(tmp_path, (), TRACEBACK_LINES, 1)

    result = _run_binary_test(host, tmp_path, binary)

    failure_lines = [line.strip() for line in result["failure"].splitlines()]
    assert "exit code 1" in failure_lines
    for line in TRACEBACK_LINES:
        assert line in failure_lines, result["failure"]


@pytest.mark.parametrize("host", POWERSHELL_HOSTS)
def test_staged_binary_success_reports_version_output(host: str, tmp_path: Path):
    binary = _write_fake_binary(tmp_path, ("apm version 9.9.9",), (), 0)

    result = _run_binary_test(host, tmp_path, binary)

    assert result["failure"] == ""
    assert result["output"] == "apm version 9.9.9"


@pytest.mark.parametrize("host", POWERSHELL_HOSTS)
def test_staged_binary_launch_error_keeps_exception_text(host: str, tmp_path: Path):
    missing = tmp_path / "missing-apm.exe"

    result = _run_binary_test(host, tmp_path, missing)

    assert result["output"] == ""
    assert "missing-apm.exe" in result["failure"]


def test_staged_binary_test_scopes_native_stderr_error_action_guard() -> None:
    """The Continue guard is what prevents 5.1 truncation; pwsh 7 cannot detect its loss."""
    body = _extract_function("Invoke-StagedBinaryTest")

    previous_guard = "$previousErrorActionPreference = $ErrorActionPreference"
    continue_guard = '$ErrorActionPreference = "Continue"'
    invocation = "$output = & $Path @Arguments 2>&1"
    restore_guard = "$ErrorActionPreference = $previousErrorActionPreference"

    assert body.count(continue_guard) == 1
    assert body.index(previous_guard) < body.index(continue_guard)
    assert body.index(continue_guard) < body.index(invocation)
    assert body.index(invocation) < body.index("finally {") < body.index(restore_guard)


def test_installer_reports_binary_test_failure_text() -> None:
    text = (ROOT / "install.ps1").read_text(encoding="utf-8")

    call = "$binaryTest = Invoke-StagedBinaryTest -Path $stagedExe -Arguments @('--version')"
    assert call in text
    assert "$testFailure = $binaryTest.Failure" in text
    assert "Test-AccessDeniedError -Text $testFailure" in text
    assert "Test-AntivirusBlockError -Text $testFailure" in text
    assert 'Write-ErrorText "Downloaded binary failed to run: $testFailure"' in text
