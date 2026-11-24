"""Research probe (CI only): which real subdir deps lever S serves, and why the rest fall back."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.fixtures.perf.generator import consumer_manifest
from tests.fixtures.perf.real_network import REAL_PACKAGES


def test_real_engagement(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(consumer_manifest("probe", list(REAL_PACKAGES), ("copilot",)))
    env = {
        **os.environ,
        "APM_LOG_LEVEL": "DEBUG",
        "APM_CACHE_DIR": str(tmp_path / "cache"),
        "HOME": str(tmp_path / "home"),
    }
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "-m", "apm_cli.cli", "install"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    for line in (result.stdout + result.stderr).splitlines():
        if "in-process" in line or "In-process" in line or "rror" in line:
            print("ENGAGE", line[-300:])
    assert result.returncode == 0, result.stderr[-3000:]
