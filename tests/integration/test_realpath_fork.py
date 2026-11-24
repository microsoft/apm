"""A forked child proves the /proc view again before trusting kernel names."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from apm_cli.utils import realpath

pytestmark = [pytest.mark.component, pytest.mark.promise("P-LAW-portability.byte_deterministic")]

_CHILD = textwrap.dedent(
    """
    import os, sys
    from pathlib import Path
    from apm_cli.utils import realpath

    root = Path(sys.argv[1])
    assert realpath.resolve_path(root) == root.resolve()
    assert realpath._proc_state["proven"]
    pid = os.fork()
    if pid == 0:
        fresh = realpath._proc_state == {"proven": False, "refused": False}
        again = realpath.resolve_path(root) == root.resolve() and realpath._proc_state["proven"]
        os._exit(0 if fresh and again else 3)
    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))
    """
)


@pytest.mark.skipif(not realpath._FAST, reason="kernel form is Linux-only")
def test_a_forked_child_proves_the_proc_view_again(tmp_path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
