"""Round 29 -- a FIFO (named pipe) planted at the ``scripts.log`` path WEDGES
the synchronous install firing path forever.

Domain: install / process / concurrency / FILESYSTEM.

Target surface (REAL, no reimplementation):
``apm_cli.core.script_executors._append_to_script_log`` and the end-to-end
``_execute_command`` / ``LifecycleScriptRunner.fire`` firing path.

THE GENUINE, NOVEL HARM
=======================
``_append_to_script_log`` hardens the log open against a *symlink* swap with
``O_NOFOLLOW`` (so a pre-planted ``scripts.log -> /etc/passwd`` symlink fails
closed). But ``O_NOFOLLOW`` rejects ONLY a symlink as the final path
component -- it does NOT reject a FIFO. The open is::

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | O_NOFOLLOW   # no O_NONBLOCK
    fd = os.open(log_path, flags, 0o600)

POSIX semantics: opening a FIFO ``O_WRONLY`` WITHOUT ``O_NONBLOCK`` BLOCKS the
caller until some process opens the read end. Nothing ever does. So a hostile
lifecycle script -- which runs as the SAME user that owns ``~/.apm/logs`` and
can therefore ``os.mkfifo`` over the log path -- turns the installer's own
post-run audit append into an UNBOUNDED HANG on the SYNCHRONOUS firing path.

This is distinct from every prior round: r21..r28 routed pipe-deadlock,
setsid escape, false reap, log-rotation clobber and torn records, but NONE
routed the log FILE TYPE. ``_rotate_log_if_large`` only ``stat()``s (which a
FIFO satisfies, returning a tiny size, so it returns early) and never guards
the file type; the blocking open is reached unconditionally.

Two harms in one:
  * UNBOUNDED ELAPSED -- the install hangs forever (no timeout bounds a
    blocking ``open()``; the capture timeout only covers the child process).
  * FAILURE-ISOLATION GAP -- a script that plants the FIFO and exits 0 wedges
    its OWN executor's audit append inside ``fire()``, so every LATER script
    in the same event never runs.

SECURE CONTRACT asserted here (FAILS on HEAD 427ed91de):
  * ``_append_to_script_log`` returns within a bounded grace even when the log
    path is a FIFO (it must fail closed -- skip the write -- never block); and
  * ``fire()`` runs a LATER sentinel script even after an earlier script
    replaced ``scripts.log`` with a FIFO, within a bounded wall-clock ceiling.

Control test proves a regular-file log path completes promptly and runs both
scripts -- so the wedge is FIFO-specific, not a flaky timeout.

PROPOSED FIX
============
Open the log non-blocking and fail closed on a non-regular file::

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(log_path, flags, 0o600)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd); return            # FIFO / device / socket -> skip
    # O_NONBLOCK on a regular file is a no-op for the subsequent O_APPEND write.

``O_NONBLOCK`` makes the FIFO open raise ``ENXIO`` (caught by the existing
``except Exception``) instead of blocking; ``S_ISREG`` closes the residual
window where a reader is briefly present. Mirror the same guard in
``_rotate_log_if_large`` before ``os.replace``.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from apm_cli.core.lifecycle_scripts import LifecycleEvent, LifecycleScriptRunner
from apm_cli.core.script_executors import _append_to_script_log, _get_scripts_log_path

from .conftest import PYEXE, make_command_entry

_PLANT_FIFO = str(Path(__file__).parent / "_workers" / "rt29_plant_fifo.py")

# Wall-clock ceiling. A fail-closed append + a fast sentinel is sub-second;
# 4s leaves generous CI headroom while still proving the unbounded hang is
# gone (HEAD blocks forever on the FIFO open).
_CEILING_S = 4.0


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX mkfifo required")
def test_round29_fifo_logpath_append_is_bounded(apm_home):
    """REAL ``_append_to_script_log`` must not block when scripts.log is a FIFO.

    Plants a FIFO at the exact log path, then drives the real append on a
    daemon thread. Secure contract: it returns (fails closed) within the
    ceiling. On HEAD the ``O_WRONLY`` open with no ``O_NONBLOCK`` blocks
    forever waiting for a reader -- the thread stays alive past the ceiling.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if log_path.exists() or log_path.is_fifo():
        log_path.unlink()
    os.mkfifo(log_path)
    assert stat.S_ISFIFO(os.stat(log_path).st_mode)

    done: dict[str, bool] = {}

    def _run() -> None:
        _append_to_script_log(
            "post-install", "command", "echo hi", stdout="hi", exit_code=0, status="ok"
        )
        done["ok"] = True

    th = threading.Thread(target=_run, daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(_CEILING_S)
    elapsed = time.monotonic() - t0

    # RED on HEAD: the blocking FIFO open never returns -> thread still alive.
    assert not th.is_alive(), (
        f"_append_to_script_log blocked on a FIFO log path for >{_CEILING_S}s "
        "(O_WRONLY open without O_NONBLOCK wedges the synchronous install)"
    )
    assert done.get("ok") is True
    assert elapsed < _CEILING_S


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX mkfifo required")
def test_round29_fifo_logpath_does_not_abort_later_scripts(apm_home, tmp_path):
    """End-to-end ``fire()``: a FIFO planted by one script must not wedge the
    install nor abort a LATER sentinel script.

    Script 1 (REAL subprocess) replaces scripts.log with a FIFO and exits 0.
    Script 2 is a sentinel that writes a token file. Secure contract: the
    sentinel runs and ``fire()`` returns within the ceiling. On HEAD, script
    1's OWN post-run audit append blocks forever inside ``_execute_command``,
    so ``fire()`` never reaches the sentinel.
    """
    log_path = _get_scripts_log_path()
    sentinel = tmp_path / "sentinel.txt"

    plant_cmd = f'{PYEXE} {_PLANT_FIFO} "{log_path}"'
    sentinel_cmd = (
        f'{PYEXE} -c "import sys; open(sys.argv[1], chr(119)).write(chr(111)+chr(107))" '
        f'"{sentinel}"'
    )

    runner = LifecycleScriptRunner(
        scripts=[
            make_command_entry(plant_cmd, event="post-install"),
            make_command_entry(sentinel_cmd, event="post-install"),
        ],
        logger=None,
        verbose=False,
        project_root=str(tmp_path),
    )
    event = LifecycleEvent(event="post-install")

    done: dict[str, bool] = {}

    def _run() -> None:
        runner.fire("post-install", event)
        done["ok"] = True

    th = threading.Thread(target=_run, daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(_CEILING_S)
    elapsed = time.monotonic() - t0

    assert not th.is_alive(), (
        f"fire() wedged for >{_CEILING_S}s after a script planted a FIFO at the "
        "scripts.log path (blocking O_WRONLY append aborts the install)"
    )
    assert sentinel.exists(), "later sentinel script never ran (failure-isolation gap)"
    assert done.get("ok") is True
    assert elapsed < _CEILING_S


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX mkfifo required")
def test_round29_regular_logfile_control_completes(apm_home, tmp_path):
    """Control: a REGULAR-FILE log path runs both scripts promptly.

    Proves the wedge in the sibling tests is FIFO-specific, not a flaky
    timeout: with an ordinary scripts.log, ``fire()`` runs the sentinel and
    returns well within the ceiling on HEAD too.
    """
    sentinel = tmp_path / "ctrl_sentinel.txt"
    sentinel_cmd = (
        f'{PYEXE} -c "import sys; open(sys.argv[1], chr(119)).write(chr(111)+chr(107))" '
        f'"{sentinel}"'
    )
    runner = LifecycleScriptRunner(
        scripts=[
            make_command_entry("echo first", event="post-install"),
            make_command_entry(sentinel_cmd, event="post-install"),
        ],
        logger=None,
        verbose=False,
        project_root=str(tmp_path),
    )
    event = LifecycleEvent(event="post-install")

    t0 = time.monotonic()
    runner.fire("post-install", event)
    elapsed = time.monotonic() - t0

    assert sentinel.exists()
    assert elapsed < _CEILING_S
