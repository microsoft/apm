"""Round-25 -- concurrent installs racing scripts.log ROTATION destroy the
audit trail (lost-update / log-clobber).

TARGET
======
``_rotate_log_if_large`` + ``_append_to_script_log`` in
``core/script_executors.py``. On every append a process does::

    if log_path.stat().st_size >= _MAX_LOG_BYTES:        # 5 MiB
        os.replace(log_path, log_path + ".1")            # rotate

then opens ``scripts.log`` ``O_APPEND`` and writes its entry. There is NO lock
around the stat+rename: the rotation is not atomic across processes.

THE BUG
=======
When several ``apm install`` processes append to the SAME
``~/.apm/logs/scripts.log`` and it crosses 5 MiB, MORE THAN ONE process can
observe ``size >= 5 MiB`` before any of them renames. Process A renames the full
~5 MiB ``scripts.log`` to ``scripts.log.1`` (preserving ~560 audit records).
Process B -- which already stat'd the big file -- then calls
``os.replace(scripts.log, scripts.log.1)`` AGAIN, but ``scripts.log`` is now a
freshly-recreated SMALL file (other writers reopened it ``O_CREAT|O_APPEND``).
B's rename CLOBBERS the ~5 MiB ``scripts.log.1`` with a tiny current log,
PERMANENTLY DESTROYING every record A had just preserved.

Observed: ~80% of audit records vanish (e.g. 816 written -> ~165 survive) on a
single 5 MiB crossing with 24 concurrent writers. Total volume here (~7.3 MiB)
is BELOW the two-file retention capacity (~10 MiB = current < 5 MiB + one 5 MiB
``.1``), so a CORRECT single-rotation implementation loses ZERO records; every
missing record is the clobber, not by-design rotation.

SECURITY RELEVANCE
==================
``scripts.log`` is the audit trail that exists to catch malicious lifecycle
scripts. An attacker (or just unlucky parallel installs) can race rotation to
silently erase the evidence of a malicious script's execution. A hostile
package can even DELIBERATELY flood the log to force a 5 MiB crossing and race
the rename to bury its own record.

SECURE CONTRACT
===============
Rotation must be atomic across processes (e.g. an ``fcntl`` exclusive lock with
a double-checked size test inside the critical section, mirroring the trust
store's lock). Below the two-file capacity, NO record may be lost. This probe
drives several rounds of a 24-writer stampede across the 5 MiB boundary and
asserts every round retains essentially all its records.

This probe FAILS at head eb48f93d9 (intermittent ~80% audit loss) and PASSES
once rotation is serialized.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from apm_cli.core import script_executors as se

from .conftest import PYEXE

WORKER = Path(__file__).parent / "_workers" / "rt25_logwriter.py"

_ROT_WRITERS = 24
_ROT_PER = 34  # 24*34=816 entries * ~8.9 KiB ~ 7.3 MiB -> single 5 MiB crossing
_ROUNDS = 6
_HDR_RE = re.compile(r"^\[[^\]]+\] event=W(\d{2}) ")
_RUN_BOUND_SEC = 90.0
# Below two-file capacity a correct rotation loses nothing; tolerate a tiny
# slack but flag the ~80% clobber collapse.
_RETENTION_FLOOR = 0.9


def _stampede_once(apm_home: Path) -> int:
    procs = [
        subprocess.Popen([PYEXE, str(WORKER), str(apm_home), str(i), str(_ROT_PER)])
        for i in range(_ROT_WRITERS)
    ]
    for p in procs:
        p.wait(timeout=_RUN_BOUND_SEC)
    for p in procs:
        assert p.returncode == 0, f"writer exited {p.returncode}"
    survived = 0
    for f in sorted((apm_home / "logs").glob("scripts.log*")):
        survived += sum(
            1
            for line in f.read_text(encoding="utf-8", errors="replace").split("\n")
            if _HDR_RE.match(line)
        )
    return survived


def test_concurrent_rotation_preserves_audit_records(tmp_path) -> None:
    """A 5 MiB-crossing install stampede must not clobber the rotated log."""
    expected = _ROT_WRITERS * _ROT_PER
    floor = int(expected * _RETENTION_FLOOR)
    assert expected * 9000 < 2 * se._MAX_LOG_BYTES, (
        "test misconfigured: volume exceeds two-file capacity -> by-design loss"
    )

    worst: tuple[int, int] | None = None  # (round, survived)
    t0 = time.monotonic()
    for r in range(_ROUNDS):
        home = tmp_path / f"home_{r}"
        (home / "logs").mkdir(parents=True)
        import os

        os.environ["APM_HOME"] = str(home)
        survived = _stampede_once(home)
        if worst is None or survived < worst[1]:
            worst = (r, survived)
        if survived < floor:
            break
    elapsed = time.monotonic() - t0
    assert elapsed < _RUN_BOUND_SEC * _ROUNDS, "stampede unbounded"

    assert worst is not None
    assert worst[1] >= floor, (
        "AUDIT-LOG CLOBBER: a concurrent rotation race destroyed audit records. "
        f"round {worst[0]} retained {worst[1]} of {expected} entries "
        f"(floor {floor}); the unlocked stat+os.replace rotation let a second "
        "writer overwrite the freshly-rotated scripts.log.1, erasing ~5 MiB of "
        "audit trail. Rotation must be serialized (fcntl lock + double-checked "
        "size) so no record is lost below the two-file retention capacity."
    )
