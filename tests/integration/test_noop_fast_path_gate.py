"""Default-off costs nothing: unflagged, apm install never loads the fast path (#4770).

A fresh interpreter imports the install command the way the CLI does. With
``APM_INSTALL_FAST_PATH`` unset the fast-path modules stay unimported, so no
audit hook, stat probe or recorder can exist; with it set they load.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-INV-I2")]

_PROBE = """
import json, sys
import apm_cli.commands.install
print(json.dumps({
    "fast_path": "apm_cli.install.noop_fast_path" in sys.modules,
    "recorder": "apm_cli.install.noop_recorder" in sys.modules,
}))
"""


def _probe(flag: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "APM_INSTALL_FAST_PATH"}
    if flag is not None:
        env["APM_INSTALL_FAST_PATH"] = flag
    return subprocess.run([sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True)


def test_unset_flag_never_imports_the_fast_path() -> None:
    result = _probe(None)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "fast_path": False,
        "recorder": False,
    }


def test_set_flag_engages_the_fast_path() -> None:
    result = _probe("1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "fast_path": True,
        "recorder": True,
    }
