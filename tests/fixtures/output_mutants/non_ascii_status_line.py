"""Recorded mutant of CLI output: every command ends with a non-ASCII status line (#4113).

``tests/integration/test_apm_lifecycle_runner_contract.py`` runs a real
command through this launcher instead of ``python -m apm_cli.cli`` and
requires the global output oracle to fail it on I6.
"""

import atexit
import runpy
import sys


def _print_check_mark() -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write("[+] done \u2713\n".encode())
    sys.stdout.buffer.flush()


atexit.register(_print_check_mark)
sys.argv[0] = "apm"
runpy.run_module("apm_cli.cli", run_name="__main__", alter_sys=True)
