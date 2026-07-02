"""E2E proof that JetBrains runtimes are not install targets."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _write_minimal_project(project: Path) -> None:
    """Create a hermetic project with no dependency work to perform."""
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: intellij-target-truth\nversion: 0.1.0\n",
        encoding="utf-8",
    )


def _run_apm(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real CLI entrypoint in a project directory."""
    env = os.environ.copy()
    for name in ("APM_TARGET", "APM_CONFIG", "APM_HOME"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "apm_cli.cli", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _valid_targets_line(output: str) -> str:
    """Return the CLI error line that enumerates supported targets."""
    for line in output.splitlines():
        if line.startswith("Valid targets:"):
            return line
    raise AssertionError(f"Missing valid targets line in output:\n{output}")


def test_install_target_intellij_is_rejected_with_runtime_guidance(tmp_path: Path) -> None:
    """Truth-correction: intellij remains a runtime alias, not a target."""
    project = tmp_path / "project"
    _write_minimal_project(project)

    result = _run_apm(project, "install", "--target", "intellij")

    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert "[x] Unknown target 'intellij'" in output
    assert "'intellij' is a runtime alias, not an install target." in output
    assert "--runtime intellij" in output
    assert "maps to target 'copilot'" in output
    assert "apm install <pkg> --target copilot" in output
    valid_targets = _valid_targets_line(output)
    assert "intellij" not in valid_targets
    assert "jetbrains" not in valid_targets


def test_install_target_copilot_still_resolves_as_supported_control(tmp_path: Path) -> None:
    """Control: a genuine supported target still reaches install resolution."""
    project = tmp_path / "project"
    _write_minimal_project(project)

    result = _run_apm(project, "install", "--target", "copilot", "--dry-run")

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Dry run complete - no changes made" in output
    assert "Unknown target" not in output
