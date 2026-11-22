"""Loading the CLI never loads the no-op fast path (#4770).

A fresh interpreter imports the install command the way the CLI does. The
fast-path modules load only when ``apm install`` runs, so no audit hook, stat
probe or recorder exists for any other command, whatever the flag says.
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


@pytest.mark.parametrize("flag", [None, "1", "0"])
def test_importing_the_install_command_never_loads_the_fast_path(flag) -> None:
    result = _probe(flag)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "fast_path": False,
        "recorder": False,
    }
