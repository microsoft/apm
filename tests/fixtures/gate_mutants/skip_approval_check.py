"""Recorded mutant of the executable gate owner: the approval check is skipped (#4031).

``tests/integration/test_generated_gate_state_machine.py`` runs the GATE spine
with this launcher instead of ``python -m apm_cli.cli`` and requires the
machine to fail.
"""

import runpy
import sys

from apm_cli.install import exec_gate


def _approve_everything(*_args, **_kwargs):
    return True, True, True, True, True


exec_gate.check_executable_approval = _approve_everything
sys.argv[0] = "apm"
runpy.run_module("apm_cli.cli", run_name="__main__", alter_sys=True)
