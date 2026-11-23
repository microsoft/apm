"""The update notice loads the install stack only when it has work to do.

``apm_cli.commands._helpers`` imports most of the install stack; the root CLI
runs the update notice on every command. While the daily check is fresh the
notice prints and fetches nothing, so a fast ``apm install`` hit (and every
other command) must not import the helpers for it (owner:
cli-startup-import-budget).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-cli-startup-import-budget")]

_PROBE = """
import sys
from apm_cli.update_notice import check_and_notify_updates
check_and_notify_updates()
print("helpers-loaded", "apm_cli.commands._helpers" in sys.modules)
"""


def _run(home: Path, **env: str) -> subprocess.CompletedProcess:
    child = {
        k: v for k, v in os.environ.items() if k not in ("APM_E2E_TESTS", "USERPROFILE", "HOME")
    }
    child.update(HOME=str(home), USERPROFILE=str(home), **env)
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=child,
    )


def test_a_fresh_daily_check_never_loads_the_install_helpers(tmp_path: Path) -> None:
    cache = (
        tmp_path / "AppData" / "Local" / "apm" / "cache"
        if sys.platform == "win32"
        else tmp_path / ".cache" / "apm"
    )
    cache.mkdir(parents=True)
    (cache / "last_version_check").touch()  # checked just now
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == ["helpers-loaded", "False"]
    assert "A new version of APM is available" not in result.stdout + result.stderr


def test_e2e_mode_never_loads_the_install_helpers(tmp_path: Path) -> None:
    result = _run(tmp_path, APM_E2E_TESTS="1")
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == ["helpers-loaded", "False"]
