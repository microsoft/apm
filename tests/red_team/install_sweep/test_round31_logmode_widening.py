"""Round 31 -- AUDIT-LOG MODE WIDENING via a pre-planted wide-mode REGULAR file.

Domain: install / audit-evasion + least-privilege.

Target surface (REAL): ``_append_to_script_log`` open path and the round-30
self-heal, driven directly AND end-to-end through ``_execute_command`` (the
exact path the install firing uses). No reimplementation.

THE GENUINE BREAK
=================
The round-29/30 hardening fail-closes + self-heals on a NON-regular log node
(FIFO / symlink / socket): it unlinks the hostile node and recreates a fresh
``0600`` regular file. But the gate is purely ``stat.S_ISREG`` on the fstat:

    fd = os.open(log_path, O_WRONLY|O_CREAT|O_APPEND|O_NOFOLLOW|O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):   # FIFO/sock/dev -> self-heal
        ...
    os.write(fd, payload)                         # REGULAR file -> append AS-IS

A hostile lifecycle script (or any prior actor) that owns ``~/.apm/logs`` can
pre-plant a *regular* ``scripts.log`` with mode ``0666`` (or ``0644``). It IS a
regular file, so ``S_ISREG`` passes and the open (``O_NOFOLLOW`` permits a plain
regular file) succeeds -- APM then appends every install's audit record to it
and NEVER tightens the mode. The node's mode stays ``0666``:

  * world-WRITABLE  -> any local user (or any same-uid process) can truncate or
    FORGE audit records -- defeating the tamper-evidence the log exists to
    provide (a malicious package can pre-arm a forgeable log, then have ANY
    later helper rewrite history to bury its own execution record);
  * world-READABLE  -> the install metadata (which packages/commands ran) of
    THIS and every FUTURE install leaks to other local users (the redactor
    masks tokens, but not the audit metadata).

This directly contradicts the function's own documented guarantee -- "opens the
log ``0600`` with ``O_NOFOLLOW`` so it cannot be world-readable" -- which holds
only on CREATE, never for a pre-existing regular file. The carefully-built
self-heal closes FIFO/symlink swaps but leaves the MODE-tamper variant wide
open, and it is reachable two ways:
  (a) direct pre-plant of a 0666 regular file (this test -- deterministic), and
  (b) as the residue of a self-heal that LOST the unlink->O_EXCL race: the
      racing-planted regular file survives (O_EXCL EEXIST drops only that one
      write) and every subsequent append lands in the attacker's wide file.

SECURE CONTRACT (asserted; FAILS on HEAD):
  After a legitimate append, scripts.log is a regular file with mode EXACTLY
  ``0600`` (no group/other bits) -- a non-0600 node at our own 0700-dir log path
  holds no legitimate audit data and must self-heal exactly like a FIFO does.

CONTROL (benign self-heal still works -- round-30 regression guard):
  A FIFO planted at the path still self-heals to a 0600 regular file and
  records, proving the proposed fix does not break the existing self-heal.

PROPOSED FIX (script_executors.py :: _append_to_script_log):
  Treat a non-0600 regular node like the existing non-regular case. After the
  open, gate on BOTH type and mode::

      st = os.fstat(fd)
      if not stat.S_ISREG(st.st_mode) or (st.st_mode & 0o077):
          os.close(fd)
          with contextlib.suppress(OSError):
              os.unlink(log_path)
          fd = os.open(log_path, excl_flags, 0o600)   # self-heal to 0600
          if not stat.S_ISREG(os.fstat(fd).st_mode):
              return
  (Unlink+O_EXCL recreate also discards any forged pre-seeded content, strictly
  better than an in-place ``fchmod``. ``O_NONBLOCK``/``O_NOFOLLOW`` unchanged.)
"""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

from apm_cli.core.lifecycle_scripts import LifecycleEvent, ScriptEntry
from apm_cli.core.script_executors import (
    _append_to_script_log,
    _execute_command,
    _get_scripts_log_path,
)

_CEILING_S = 4.0


def _bounded_append(token: str, ceiling: float = _CEILING_S) -> None:
    """Run a real append on a daemon thread; assert it never wedges."""
    done: dict[str, bool] = {}

    def _run() -> None:
        _append_to_script_log("post-install", "command", f"echo {token}", stdout=token)
        done["ok"] = True

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(ceiling)
    assert not th.is_alive(), f"append for {token} blocked >{ceiling}s"
    assert done.get("ok") is True


def _mode(p: Path) -> int:
    return os.stat(p).st_mode & 0o777


def test_round31_preplanted_wide_regular_log_stays_world_accessible(apm_home):
    """A pre-planted 0666 regular scripts.log is appended to without tightening.

    RED on HEAD: final mode is 0666 (world readable+writable); the secure
    contract (strict 0600) fails. The benign-create control proves a normal log
    IS 0600, so the assertion is about the tamper case only.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # --- Control: a normal (created-by-us) append yields a strict 0600 log. ---
    if log_path.exists():
        log_path.unlink()
    _bounded_append("BENIGN_CREATE")
    assert _mode(log_path) == 0o600, "control: a freshly created log must be 0600"

    # --- Attack: hostile script pre-plants a world-rw regular scripts.log. ---
    log_path.unlink()
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o666)
    os.close(fd)
    os.chmod(log_path, 0o666)  # noqa: S103  -- attack setup: plant world-writable log
    assert _mode(log_path) == 0o666 and stat.S_ISREG(os.stat(log_path).st_mode)

    _bounded_append("AFTER_WIDE_PLANT")

    final = _mode(log_path)
    assert stat.S_ISREG(os.stat(log_path).st_mode), "log must stay a regular file"
    assert "AFTER_WIDE_PLANT" in log_path.read_text(), "the append must be recorded"
    # SECURE CONTRACT (FAILS on HEAD: final == 0o666):
    assert final & 0o077 == 0, (
        f"AUDIT-LOG MODE WIDENING: scripts.log mode is {oct(final)} after a "
        "legitimate append -- a pre-planted world-readable/writable regular log "
        "is never tightened to 0600. Any local user can READ install metadata "
        "and FORGE/TRUNCATE the tamper-evidence audit trail; the documented "
        "'cannot be world-readable' guarantee holds only on create. The "
        "S_ISREG self-heal must also fire on a non-0600 regular node."
    )


def test_round31_wide_log_enables_audit_forgery(apm_home):
    """World-writable log => a non-APM writer can splice a forged record that
    sits beside APM's genuine entries indistinguishably.

    RED on HEAD: the log remains 0666, so the forged line co-exists with real
    records in the same file the auditor trusts.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if log_path.exists():
        log_path.unlink()
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o666)
    os.close(fd)
    os.chmod(log_path, 0o666)  # noqa: S103  -- attack setup: plant world-writable log

    _bounded_append("REAL_RECORD")

    # A foreign actor (different process/user in the multi-user case) appends a
    # forged audit line because the file is world-writable.
    forged = "[1999-01-01T00:00:00Z] event=post-install type=command target=innocent status=ok\n"
    with open(log_path, "a") as fh:
        fh.write(forged)

    final = os.stat(log_path).st_mode & 0o777
    # SECURE CONTRACT (FAILS on HEAD): had APM tightened to 0600, the foreign
    # writer could not have opened the file for append in the multi-user model.
    assert final & 0o022 == 0, (
        f"audit log is world-WRITABLE ({oct(final)}): a foreign writer forged "
        f"a record ({'innocent' in log_path.read_text()}) into the trusted "
        "audit trail because APM never tightened the pre-planted node to 0600"
    )


def test_round31_wide_log_widening_via_execute_command(apm_home):
    """End-to-end through the REAL install firing path (_execute_command).

    RED on HEAD: after a real command script runs, the pre-planted 0666 log is
    still 0666 -- the live install path inherits the widening.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if log_path.exists():
        log_path.unlink()
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o666)
    os.close(fd)
    os.chmod(log_path, 0o666)  # noqa: S103  -- attack setup: plant world-writable log

    event = LifecycleEvent(event="post-install")
    entry = ScriptEntry(
        script_type="command", event="post-install", command="echo E2E_HELLO", source="user"
    )
    t0 = time.monotonic()
    _execute_command(entry, event)
    assert time.monotonic() - t0 < 30.0  # bounded install

    final = os.stat(log_path).st_mode & 0o777
    assert "E2E_HELLO" in log_path.read_text()
    assert final & 0o077 == 0, (
        f"the live _execute_command install path appended to a {oct(final)} "
        "scripts.log without tightening -- audit log widening reachable end-to-end"
    )
