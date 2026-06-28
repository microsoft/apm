"""Round 32 -- PERMANENT AUDIT BLACKOUT via a DIRECTORY planted at the log path.

Domain: install / audit-evasion (self-heal gap).

Target surface (REAL): ``_append_to_script_log`` round-30/31 self-heal
(``except OSError: unlink + O_EXCL recreate``) and the live install firing path
``_execute_command``, driven directly. No reimplementation.

THE GENUINE, NOVEL BREAK
========================
The round-30 self-heal was introduced precisely to stop a PERMANENT audit
blackout: a hostile lifecycle script (same uid -- the documented threat model:
"one malicious package's install script") that plants a non-regular node over
``~/.apm/logs/scripts.log`` must NOT durably blind the auditor. For a FIFO /
symlink / socket the heal works: the open raises (ENXIO / ELOOP) or the fstat
fails S_ISREG, and the code does::

    try:
        fd = os.open(log_path, flags, 0o600)        # O_NOFOLLOW|O_NONBLOCK
    except OSError:
        with contextlib.suppress(FileNotFoundError): # <-- ONLY FileNotFoundError
            os.unlink(log_path)
        fd = os.open(log_path, excl_flags, 0o600)    # O_EXCL recreate

But a planted **DIRECTORY** named ``scripts.log`` defeats this deterministically:

  * the first ``os.open(dir, O_WRONLY|...)`` raises ``IsADirectoryError`` (EISDIR)
    -> the ``except OSError`` branch runs;
  * ``os.unlink(dir)`` raises ``PermissionError`` (EPERM on macOS / EISDIR on
    Linux) -- which is NOT ``FileNotFoundError``, so ``contextlib.suppress``
    does NOT swallow it -- the exception propagates out of the inner try, is
    caught by the outer ``except Exception``, and the write is silently DROPPED.

``os.unlink`` can never remove a directory (that needs ``os.rmdir``), so the
self-heal can NEVER clear the node. The result is identical to the round-29
FIFO blackout the round-30 heal was built to fix -- but reachable with the same
``mkdir`` capability the heal assumed it had defeated:

  * the planting script's OWN execution record is suppressed; AND
  * EVERY later script and EVERY FUTURE ``apm install`` of UNRELATED packages
    is silently dropped -- a durable, self-perpetuating audit blackout that
    OUTLASTS the malicious package (until someone manually rmdir's the node).

This is strictly the harm class round-30 closed for FIFOs, left open for the
directory variant. Deterministic: no timing race, no concurrency -- a single
``mkdir`` blackholes the audit trail forever.

SECURE CONTRACT (asserted; FAILS on HEAD 46c5adb03):
  A legitimate append AFTER a directory node was planted must still record its
  line in a readable 0600 REGULAR log (logging SELF-HEALS), exactly as it does
  after a FIFO/symlink plant; and every append stays BOUNDED.

CONTROL (round-30/31 regression guard):
  A FIFO planted at the path still self-heals to a 0600 regular file -- proving
  the proposed fix does not break the existing self-heal.

PROPOSED FIX (script_executors.py :: _append_to_script_log, except branch):
  The hostile node may be a directory; ``os.unlink`` cannot remove it. Clear
  whatever node is present before the O_EXCL recreate::

      except OSError:
          with contextlib.suppress(FileNotFoundError):
              try:
                  os.unlink(log_path)
              except IsADirectoryError:
                  os.rmdir(log_path)        # empty dir plant -> self-heal
              except PermissionError:        # macOS reports EPERM for a dir
                  with contextlib.suppress(OSError):
                      os.rmdir(log_path)
          fd = os.open(log_path, excl_flags, 0o600)

  This heals the empty-directory plant (the minimal, deterministic attack) the
  same way the FIFO plant heals. A NON-empty attacker directory still cannot be
  rmdir'd (a documented residual -- but a harder plant, and indistinguishable
  from the attacker simply filling the dir; the common ``mkdir scripts.log``
  blackout is closed). No daemon-survival surface is touched.
"""

from __future__ import annotations

import contextlib
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


def _clear(log_path: Path) -> None:
    """Remove whatever node sits at the log path (file/symlink/fifo/dir)."""
    if os.path.islink(log_path):
        os.unlink(log_path)
        return
    if log_path.is_dir():
        # may be planted with content by a test; clear recursively
        for child in log_path.iterdir():
            child.unlink()
        os.rmdir(log_path)
        return
    if log_path.exists():
        log_path.unlink()


def _bounded_append(token: str, ceiling: float = _CEILING_S) -> float:
    """Run a real append on a daemon thread; assert it never wedges."""
    done: dict[str, bool] = {}

    def _run() -> None:
        _append_to_script_log("post-install", "command", f"echo {token}", stdout=token)
        done["ok"] = True

    th = threading.Thread(target=_run, daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(ceiling)
    elapsed = time.monotonic() - t0
    assert not th.is_alive(), f"append for {token} blocked >{ceiling}s (install hang)"
    assert done.get("ok") is True
    return elapsed


def _regular_0600(p: Path) -> bool:
    if not (p.exists() and not os.path.islink(p)):
        return False
    st = os.stat(p)
    return stat.S_ISREG(st.st_mode) and (st.st_mode & 0o777) == 0o600


def test_round32_directory_at_logpath_permanent_blackout(apm_home):
    """A directory planted at scripts.log durably blackholes the audit trail.

    RED on HEAD: the self-heal's ``os.unlink`` cannot remove a directory, so the
    write is dropped and NO regular log is ever created -- not for this append
    nor any future install. The secure contract (self-heal -> 0600 regular file
    with the record) fails.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # --- Control: a normal append yields a 0600 regular log. ---
    _clear(log_path)
    _bounded_append("BENIGN_CREATE")
    assert _regular_0600(log_path), "control: a freshly created log must be 0600 regular"
    assert "BENIGN_CREATE" in log_path.read_text()

    # --- Attack: hostile (same-uid) script plants an EMPTY directory. ---
    _clear(log_path)
    os.mkdir(log_path)
    assert log_path.is_dir()

    # Append stays bounded (no hang) -- but on HEAD the record is dropped.
    elapsed = _bounded_append("AFTER_DIR_PLANT")
    assert elapsed < _CEILING_S

    # SECURE CONTRACT (FAILS on HEAD): the node self-heals to a 0600 regular
    # file holding the audit record, exactly as a FIFO/symlink plant does.
    assert not log_path.is_dir(), (
        "PERMANENT AUDIT BLACKOUT: a directory planted at scripts.log is never "
        "cleared by the self-heal (os.unlink cannot remove a directory; EPERM/"
        "EISDIR is not FileNotFoundError so it is not suppressed) -- the write "
        "is dropped and the node persists, blackholing every future install's "
        "audit record. os.rmdir the empty-dir plant like the FIFO self-heal."
    )
    assert _regular_0600(log_path), "self-healed log must be a 0600 regular file"
    assert "AFTER_DIR_PLANT" in log_path.read_text(), "the append must be recorded"


def test_round32_dir_blackout_is_durable_across_future_installs(apm_home):
    """The blackout OUTLASTS the planting script: later UNRELATED installs are
    also silently dropped while the directory persists.

    RED on HEAD: after N appends the node is still a directory and no regular
    log exists -- proving the blindness is self-perpetuating, not one-shot.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _clear(log_path)
    os.mkdir(log_path)

    for i in range(5):
        _bounded_append(f"FUTURE_INSTALL_{i}")

    # SECURE CONTRACT (FAILS on HEAD): logging must have self-healed at the
    # latest by some append, so a regular log with at least the last record
    # exists. On HEAD the directory persists and nothing is ever recorded.
    assert _regular_0600(log_path), (
        "durable blackout: 5 successive installs all dropped their audit line "
        "-- the directory node was never cleared; the auditor is permanently "
        "blind to ALL future activity, not just the planting script"
    )
    assert "FUTURE_INSTALL_4" in log_path.read_text()


def test_round32_dir_blackout_via_execute_command(apm_home):
    """End-to-end through the REAL install firing path (_execute_command).

    RED on HEAD: after a real command script runs with a directory planted at
    the log path, no audit record exists -- the live install path inherits the
    blackout. The install itself stays BOUNDED (the script still runs).
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _clear(log_path)
    os.mkdir(log_path)

    event = LifecycleEvent(event="post-install")
    entry = ScriptEntry(
        script_type="command",
        event="post-install",
        command="echo E2E_BLACKOUT",
        source="user",
    )
    done: dict[str, bool] = {}

    def _run() -> None:
        _execute_command(entry, event)
        done["ok"] = True

    th = threading.Thread(target=_run, daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(30.0)
    assert not th.is_alive(), "install via _execute_command hung past deadline"
    assert done.get("ok") is True
    assert time.monotonic() - t0 < 30.0

    # SECURE CONTRACT (FAILS on HEAD): the live install path records its line in
    # a self-healed 0600 regular log.
    assert _regular_0600(log_path), (
        "the live _execute_command install path could not record its audit line "
        "because a directory node at scripts.log was never self-healed -- "
        "audit blackout reachable end-to-end through the real firing path"
    )
    assert "E2E_BLACKOUT" in log_path.read_text()


def test_round32_fifo_self_heal_regression_guard(apm_home):
    """CONTROL: the round-30 FIFO self-heal still works -- a FIFO planted at the
    path heals to a 0600 regular file. Guards the proposed dir fix against
    regressing the existing heal. CLEAN on HEAD and after the fix.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _clear(log_path)
    os.mkfifo(log_path)

    elapsed = _bounded_append("FIFO_HEAL")
    assert elapsed < _CEILING_S
    assert _regular_0600(log_path), "FIFO must self-heal to a 0600 regular file"
    assert "FIFO_HEAL" in log_path.read_text()

    with contextlib.suppress(OSError):
        _clear(log_path)
