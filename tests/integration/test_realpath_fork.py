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

    realpath._openat2_state["usable"] = False  # exercise the /proc proof itself
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


_LOCKED_FORK = textwrap.dedent(
    """
    import os, sys, threading, time
    from pathlib import Path
    from apm_cli.utils import realpath

    realpath._openat2_state["usable"] = False
    root = Path(sys.argv[1])
    held, release = threading.Event(), threading.Event()

    def hold():
        with realpath._proof_lock:
            held.set()
            release.wait(30)

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    held.wait(30)
    pid = os.fork()
    if pid == 0:
        ok = realpath.resolve_path(root) == root.resolve()
        os._exit(0 if ok else 3)
    release.set()
    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))
    """
)


@pytest.mark.skipif(not realpath._FAST, reason="kernel form is Linux-only")
def test_a_child_forked_while_the_proof_lock_is_held_does_not_hang(tmp_path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _LOCKED_FORK, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
