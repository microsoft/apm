"""A standalone ``apm --version`` loads only the version constant and click (#4810)."""

import subprocess
import sys

import pytest

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-cli-startup-import-budget")]


def test_standalone_version_loads_only_the_version_constant_and_click() -> None:
    """``apm --version`` from a shipped binary must not pay for the CLI, rich or colorama."""
    probe = (
        "import sys\n"
        "import apm_cli.version as v\n"
        "v.__BUILD_VERSION__, v.__BUILD_SHA__ = '1.2.3', 'abc1234'\n"
        "sys.argv = ['apm', '--version']\n"
        "from apm_cli.entrypoint import main\n"
        "try:\n"
        "    main()\n"
        "except SystemExit:\n"
        "    pass\n"
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'rich', 'colorama', 'git', 'requests', 'yaml'} or m == 'apm_cli.cli')\n"
        "print('HEAVY=' + ','.join(heavy))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "Agent Package Manager (APM) CLI version 1.2.3 (abc1234)" in result.stdout
    assert "HEAVY=\n" in result.stdout
