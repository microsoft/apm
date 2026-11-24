"""Research probe (CI only): does a default Git for Windows user engage B1 on main?"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.fixtures.perf.generator import consumer_manifest
from tests.fixtures.perf.real_network import REAL_PACKAGES


def test_main_engagement(tmp_path: Path) -> None:
    for args in (
        ["--version"],
        ["config", "--system", "--get", "http.sslBackend"],
        ["config", "--system", "--get", "http.sslCAInfo"],
        ["config", "--show-origin", "--get-all", "http.sslBackend"],
        ["config", "--show-origin", "--get-all", "http.sslCAInfo"],
    ):
        out = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
        print("GITINFO", " ".join(args), "->", repr(out.stdout.strip()), out.returncode)
    project = tmp_path / "project"
    project.mkdir()
    (project / "apm.yml").write_text(consumer_manifest("probe", list(REAL_PACKAGES), ("copilot",)))
    env = {**os.environ, "APM_LOG_LEVEL": "DEBUG", "APM_CACHE_DIR": str(tmp_path / "cache")}
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_APM_PAT"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-m", "apm_cli.cli", "install"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    for line in (result.stdout + result.stderr).splitlines():
        if "in-process" in line.lower():
            print("ENGAGE", line[-300:])
    assert result.returncode == 0, result.stderr[-3000:]
