"""Contracts for the gh-aw shared APM workflow boundary (pure logic cases)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.workflow_contracts import run_bash as _run_bash

pytestmark = pytest.mark.unit


@pytest.mark.windows_compat
@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_workflow_shell_resolves_native_bash(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """Never dispatch a workflow through Windows' ambient WSL launcher."""
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("PROGRAMFILES", "C:/Program Files")
    search_path = str(Path("C:/Program Files") / "Git" / "bin") if platform == "win32" else None
    executable = str(Path(search_path) / "bash.exe") if search_path else "/bin/bash"
    with (
        patch("shutil.which", return_value=executable) as which,
        patch("subprocess.run") as run,
    ):
        result = _run_bash("printf '%s' \"$VALUE\"", env={"VALUE": "workflow value"})

    which.assert_called_once_with("bash", path=search_path)
    run.assert_called_once_with(
        (executable, "-c", "printf '%s' \"$VALUE\""),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "VALUE": "workflow value"},
        timeout=30,
    )
    assert result is run.return_value


@pytest.mark.windows_compat
def test_workflow_shell_does_not_fall_back_when_native_bash_is_missing() -> None:
    """A missing prerequisite must fail rather than skip or dispatch to WSL."""
    with patch("shutil.which", return_value=None), patch("subprocess.run") as run:
        with pytest.raises(FileNotFoundError, match="require native Bash"):
            _run_bash("exit 0", env={})
    run.assert_not_called()


@pytest.mark.windows_compat
@pytest.mark.parametrize(
    ("platform", "program_files", "message"),
    [
        (
            "win32",
            None,
            "Workflow contract tests require PROGRAMFILES to locate Git for Windows Bash",
        ),
        (
            "win32",
            "",
            "Workflow contract tests require PROGRAMFILES to locate Git for Windows Bash",
        ),
        (
            "win32",
            "C:/Program Files",
            "Workflow contract tests require native Bash; install Git for Windows",
        ),
        (
            "linux",
            None,
            "Workflow contract tests require native Bash; install bash on PATH",
        ),
        (
            "darwin",
            None,
            "Workflow contract tests require native Bash; install bash on PATH",
        ),
    ],
)
def test_workflow_shell_reports_missing_native_bash_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    program_files: str | None,
    message: str,
) -> None:
    """Missing prerequisites fail explicitly without dispatching another shell."""
    monkeypatch.setattr(sys, "platform", platform)
    if program_files is None:
        monkeypatch.delenv("PROGRAMFILES", raising=False)
    else:
        monkeypatch.setenv("PROGRAMFILES", program_files)

    with patch("shutil.which", return_value=None) as which, patch("subprocess.run") as run:
        with pytest.raises(FileNotFoundError, match=f"^{re.escape(message)}$"):
            _run_bash("exit 0", env={})

    if platform == "win32" and not program_files:
        which.assert_not_called()
    run.assert_not_called()
