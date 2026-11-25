"""When a full install attests the state it committed (W3d attested-record ruling A2).

Kept out of ``commands/install.py`` (file length budget) and out of
``install/verdict_record.py``, whose imports stay light for the replay.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from apm_cli.install import verdict_record as vr


def attest_clean_install(result: Any, *, dry_run: bool) -> Any:
    """A full install that converged with nothing kept, skipped, refused or deferred
    attests its committed state, so the next unchanged install skips the proof."""
    from apm_cli.models.results import InstallDisposition

    # Every warning or error line the run printed, collector summaries included,
    # is judged by the record itself (W3d A2 allowlist, verdict_record._unsettled_line).
    if (
        not dry_run
        and result.exit_code == 0
        and getattr(result, "disposition", None) is InstallDisposition.SUCCESS
    ):
        note_attested_install()
    return result


def note_run_targets(line: str) -> None:
    """The target line the full install announced (W3d A1)."""
    if vr._ACTIVE:
        vr._RUN_TARGETS[:] = [line]


def note_attested_install() -> None:
    """A successful, convergence-clean full install committed its state (W3d A2).

    The caller holds the lifecycle lock; the record is written after the CLI
    returns, under the lock again, only over the lock bytes noted here (A6).
    """
    if vr._ACTIVE and vr._RUN_TARGETS:
        vr._ATTESTED_INSTALL[:] = list(vr._RUN_TARGETS)
        vr._note_proven_lock()


def attested_state(argv: list[str], root: Path) -> bool:
    """Whether an attested record proves this run's state (the engine's lock held).

    The same HMAC over every live input and the same no-follow re-hash of every
    deployed file and package copy as a vr.replay (W3d A7), keyed at this run's
    entry state; any doubt is False and the engine proves the run itself.
    """
    if not vr._served(argv) or not vr._hits_allowed() or not vr._RUN_ENTRY:
        return False
    try:
        if not os.path.lexists(vr._record_path(root)):
            vr._trace("attested miss (no record)")
            return False
        vr._ENTRY_ENV, vr._ENTRY_PRESENTATION = vr._RUN_ENTRY[0]
        vr._verified_stdout(argv, root, attested=True)
    except Exception as exc:
        vr._trace(f"attested miss ({exc or type(exc).__name__})")
        return False
    finally:
        vr._ENTRY_ENV, vr._ENTRY_PRESENTATION = None, None
    vr._trace("attested hit")
    return True


def attested_gates(project_root: Path, lock: Any, fetch: Any) -> bool:
    """The gates that re-judge every run, for a state an attested record proves.

    A successful full install attested the state it committed; the record's
    HMAC over every live input and the no-follow re-hash of every deployed file
    and package copy hold (A7), so planning and staging would find nothing.
    Scripts, executables and policy still re-judge this run.
    """
    from apm_cli.install import noop_diff
    from apm_cli.install.diff_apply import (
        Decline,
        _refuse_lifecycle_scripts,
        _refuse_unsilent_executables,
    )

    _refuse_lifecycle_scripts(project_root)
    _refuse_unsilent_executables(lock)
    from apm_cli.install.fast_output import quiet

    with quiet():
        if not noop_diff._policy_checks_pass(project_root, fetch):
            raise Decline("policy checks")
    return True


def release_attested(held: Any) -> bool:
    """Print a committed attested no-op; a failure propagates, never the full pipeline."""
    from apm_cli.install.diff_apply import _trace

    held.release()
    _trace("no-op (attested record)")
    # Provenance never upgrades (W3d A5): this run's output record stays attested.
    vr.note_verified_noop(vr.ATTESTED)
    return True
