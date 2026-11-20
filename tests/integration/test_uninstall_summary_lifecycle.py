"""Installed-binary contract for the uninstall summary wording (#4184)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
]

_COUNT_LINE = re.compile(r"\b\d+ packages?\b|package\(s\)")


def _run(binary: Path, *args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_local_uninstall_summary_counts_once_with_manifest_spelling(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """One package yields one count line, its manifest name, and no absolute paths."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "project"
    skill = project / "pkg" / ".apm" / "skills" / "demo"
    skill.mkdir(parents=True)
    (project / "apm.yml").write_text(
        "name: project\nversion: 1.0.0\ntargets: [claude]\n", encoding="utf-8"
    )
    (project / "pkg" / "apm.yml").write_text("name: pkg\nversion: 1.0.0\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# Demo\n", encoding="utf-8"
    )

    install = _run(apm_binary_path, "install", "./pkg", cwd=project, env=env)
    assert install.returncode == 0, install.stdout + install.stderr

    uninstall = _run(apm_binary_path, "uninstall", "./pkg", cwd=project, env=env)
    output = uninstall.stdout + uninstall.stderr
    assert uninstall.returncode == 0, output

    summary_lines = [
        line for line in output.splitlines() if "[*]" in line and _COUNT_LINE.search(line)
    ]
    assert len(summary_lines) == 1, output
    assert re.search(r"\b1 package\b", summary_lines[0]), output
    assert "package(s)" not in output
    assert "_local/" not in output
    assert "./pkg" in output
    assert str(project) not in output
    assert str(project.resolve()) not in output
    assert not (project / "apm_modules" / "_local" / "pkg").exists()
