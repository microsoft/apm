"""Round 30 -- PERSISTENT AUDIT BLACKOUT via a planted non-regular log node,
and the S_ISREG/flock rotation guard under concurrent threshold crossings.

Domain: install / audit-evasion / concurrency-race.

Target surface (REAL): ``_append_to_script_log`` (O_NONBLOCK + O_NOFOLLOW +
S_ISREG fail-closed -- the round-29 fix) and ``_rotate_log_if_large``, driven
directly (the exact path ``_execute_command`` uses).

THE GENUINE, NOVEL HARM (test 1/2)
==================================
The round-29 fix made ``_append_to_script_log`` FAIL CLOSED on a FIFO log path
so the install no longer WEDGES (good). But it fails closed by simply
``return``-ing -- it NEVER clears the tampered node. A FIFO (or any non-regular
node) a hostile script ``mkfifo``'d over ``~/.apm/logs/scripts.log`` therefore
PERSISTS, and the open path is:

    fd = os.open(log_path, O_WRONLY|O_CREAT|O_APPEND|O_NOFOLLOW|O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        return            # <-- node left in place, write dropped, no self-heal

Contrast with a script that merely ``rm``'s the log: the very next append
``O_CREAT``s a fresh REGULAR file and logging RESUMES (self-heal). With a FIFO
the ``O_CREAT`` is a no-op (the node exists) and ``S_ISREG`` drops the write --
FOREVER. So a single ``mkfifo`` by one malicious package's install script
produces a PERMANENT, self-perpetuating audit blackout that:

  * suppresses that script's OWN execution record (audit-evasion), AND
  * silently drops the audit records of EVERY LATER script and EVERY FUTURE
    ``apm install`` of UNRELATED packages -- blinding the auditor to OTHER
    actors, with effects that OUTLAST the malicious package (even after it is
    removed) until someone manually deletes the FIFO.

This is strictly WORSE than the ``rm`` case the audit log already tolerates,
and is a residual the round-29 fix introduced (it chose drop-and-leave over
self-heal). The install stays bounded -- but the audit trail is durably dead.

SECURE CONTRACT (asserted; FAILS on HEAD 059f2f4e2):
  * a legitimate append AFTER a non-regular node was planted must still record
    its line in a readable regular log (logging SELF-HEALS), exactly as it does
    after an ``rm``; while
  * every append stays BOUNDED (never blocks on the FIFO).

PROPOSED FIX (preserves bounded install; no daemon-survival surface here):
  A non-regular node at our OWN 0700-dir log path holds no legitimate audit
  data, so unlink it and retry the create exclusively. BOTH the no-reader-FIFO
  case (the ``O_NONBLOCK`` open raises ``ENXIO`` -- the dominant case this probe
  hits) AND the reader-present / socket / device case (``S_ISREG`` false on the
  fstat) must self-heal::

      flags = O_WRONLY|O_CREAT|O_APPEND|O_NOFOLLOW|O_NONBLOCK
      try:
          fd = os.open(log_path, flags, 0o600)
      except OSError:                       # no-reader FIFO -> ENXIO
          with contextlib.suppress(OSError):
              os.unlink(log_path)           # drop the hostile node
          try:
              fd = os.open(log_path, flags | os.O_EXCL, 0o600)   # self-heal
          except OSError:
              return
      try:
          if not stat.S_ISREG(os.fstat(fd).st_mode):  # reader-present / sock / dev
              os.close(fd)
              with contextlib.suppress(OSError):
                  os.unlink(log_path)
              fd = os.open(log_path, flags | os.O_EXCL, 0o600)
              if not stat.S_ISREG(os.fstat(fd).st_mode):
                  return                    # lost a race -> drop THIS write only
          os.write(fd, ...)
      finally:
          os.close(fd)

  This keeps THIS one racing write best-effort but restores logging for every
  subsequent append, matching the ``rm`` self-heal and closing the permanent
  blackout. ``O_NONBLOCK`` still bounds the open; ``O_NOFOLLOW`` + the 0700 dir
  still reject symlink/parent-dir tricks.

PART B -- rotation concurrency (asserted, expected CLEAN)
========================================================
N processes each cross the 5 MiB rotation threshold under the S_ISREG + flock
guard. Contract: no torn record (no foreign writer's token spliced inside
another writer's multi-line entry).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from apm_cli.core.script_executors import (
    _append_to_script_log,
    _get_scripts_log_path,
)

_LOGWRITER = str(Path(__file__).parent / "_workers" / "rt25_logwriter.py")
_CEILING_S = 4.0


def _read_regular_log(log_path: Path) -> str | None:
    if log_path.exists() and stat.S_ISREG(os.stat(log_path).st_mode):
        return log_path.read_text(errors="replace")
    return None


def _bounded_append(token: str, ceiling: float = _CEILING_S) -> float:
    """Run a real append on a daemon thread; return elapsed, assert bounded."""
    done: dict[str, bool] = {}

    def _run() -> None:
        _append_to_script_log(
            "future-install", "command", f"echo {token}", stdout=token, status="ok"
        )
        done["ok"] = True

    th = threading.Thread(target=_run, daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(ceiling)
    elapsed = time.monotonic() - t0
    assert not th.is_alive(), f"append for {token} blocked >{ceiling}s on the log path"
    assert done.get("ok") is True
    return elapsed


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX mkfifo required")
def test_round30_fifo_logpath_persistent_blackout(apm_home):
    """A FIFO planted at scripts.log must not durably blackout the audit log:
    a later legitimate append must self-heal and record, as it does after rm.

    RED on HEAD: the FIFO persists, the legit line is never recorded.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # --- Control: an rm'd (absent) log self-heals on the next append. ---
    if log_path.exists() or log_path.is_fifo():
        log_path.unlink()
    _bounded_append("TOKEN_AFTER_RM")
    rm_log = _read_regular_log(log_path)
    assert rm_log is not None and "TOKEN_AFTER_RM" in rm_log, (
        "control failed: append after rm did not recreate/record a regular log"
    )

    # --- Attack: plant a FIFO, then a LEGIT (future-install) append. ---
    if log_path.exists() or log_path.is_fifo():
        log_path.unlink()
    os.mkfifo(log_path)
    assert stat.S_ISFIFO(os.stat(log_path).st_mode)

    elapsed = _bounded_append("TOKEN_AFTER_FIFO")
    assert elapsed < _CEILING_S  # install path stays bounded (round-29 win)

    # SECURE CONTRACT (FAILS on head): logging self-heals -- the legit line is
    # recorded in a readable regular log, exactly like the rm control.
    healed = _read_regular_log(log_path)
    try:
        assert healed is not None and "TOKEN_AFTER_FIFO" in healed, (
            "PERSISTENT AUDIT BLACKOUT: a FIFO planted at scripts.log durably "
            "suppresses every later audit append (the legit future-install line "
            "was dropped and never recorded); the appender never self-heals, "
            "unlike the rm case -- blinding the auditor to all subsequent actors"
        )
    finally:
        if log_path.is_fifo():
            log_path.unlink()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX mkfifo required")
def test_round30_fifo_blackout_outlasts_many_appends(apm_home):
    """The blackout is self-perpetuating: many later appends never recover.

    RED on head: after N legit appends the node is still a FIFO and nothing was
    recorded. Bounded throughout.
    """
    log_path = _get_scripts_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if log_path.exists() or log_path.is_fifo():
        log_path.unlink()
    os.mkfifo(log_path)

    for i in range(6):
        _bounded_append(f"TOK_SEQ_{i}")

    recorded = _read_regular_log(log_path)
    try:
        assert recorded is not None and "TOK_SEQ_5" in recorded, (
            "audit blackout outlasted 6 legitimate appends with no self-heal "
            "(the planted FIFO permanently disables the audit log)"
        )
    finally:
        if log_path.is_fifo():
            log_path.unlink()


def test_round30_concurrent_rotation_no_torn_record(apm_home):
    """N writers crossing the rotation threshold -> no torn multi-line entry."""
    apm_home_path = os.environ["APM_HOME"]
    nwriters = 8
    count = 60  # 8 * 60 * ~8 KiB ~ 3.8 MiB; bumps near/over the 5 MiB cap
    procs = []
    for idx in range(nwriters):
        p = subprocess.Popen(
            [sys.executable, _LOGWRITER, apm_home_path, str(idx), str(count)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(p)
    t0 = time.monotonic()
    for p in procs:
        p.wait()
    elapsed = time.monotonic() - t0
    assert elapsed < 30.0, f"concurrent log writers took {elapsed:.1f}s"

    log_path = _get_scripts_log_path()
    blobs = []
    for cand in (log_path, log_path.with_name(log_path.name + ".1")):
        if cand.exists() and stat.S_ISREG(os.stat(cand).st_mode):
            blobs.append(cand.read_text(errors="replace"))
    combined = "\n".join(blobs)
    assert combined, "no log content was written at all"

    torn = []
    for line in combined.splitlines():
        m = re.search(r"(?:stdout|stderr): (K\d\dK.*)", line)
        if not m:
            continue
        field = m.group(1)
        tokens = set(re.findall(r"K(\d\d)K", field))
        if len(tokens) > 1:
            torn.append((tokens, field[:80]))
    assert not torn, f"torn/interleaved log records under rotation: {torn[:3]}"
