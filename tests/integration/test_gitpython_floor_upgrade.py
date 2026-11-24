"""Opt-in: installing apm-cli upgrades a GitPython below the advisory floor (#5005).

GitPython releases before 3.1.41 carry published Windows advisories
(CVE-2023-40590, CVE-2024-22190). An environment that already holds 3.1.40
must not keep it when apm-cli is installed: pip's resolver has to choose
3.1.41 or later. Opt-in (``APM_RUN_INTEGRATION_TESTS=1``): pip resolves
against the package index.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.version import Version

pytestmark = [
    pytest.mark.component,
    pytest.mark.integration,
    pytest.mark.requires_network_integration,
    pytest.mark.promise("P-SEC-gitpython-advisory-floor"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pip_install_upgrades_a_gitpython_below_the_floor(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=120)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "GitPython==3.1.40"],
        check=True,
        timeout=300,
    )

    report = tmp_path / "report.json"
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--quiet",
            "--report",
            str(report),
            str(_REPO_ROOT),
        ],
        check=True,
        timeout=600,
    )

    planned = {
        entry["metadata"]["name"].lower(): Version(entry["metadata"]["version"])
        for entry in json.loads(report.read_text(encoding="utf-8"))["install"]
    }
    assert "gitpython" in planned, "pip kept GitPython 3.1.40 instead of upgrading it"
    assert planned["gitpython"] >= Version("3.1.41")
