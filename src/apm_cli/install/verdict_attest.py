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


def attested_gates(project_root: Path, lock: Any, fetch: Any, disposition: Any = None) -> bool:
    """The gates that re-judge every run, for a state an attested record proves.

    A successful full install attested the state it committed; the record's
    HMAC over every live input and the no-follow re-hash of every deployed file
    and package copy hold (A7), so planning and staging would find nothing.
    Scripts, executables and policy still re-judge this run, and a ``--frozen``
    run re-proves every declared edge from the lock alone (its own refusal):
    a plain install's attestation may serve it (#4770).
    """
    from apm_cli.install import noop_diff
    from apm_cli.install.diff_apply import (
        Decline,
        _refuse_lifecycle_scripts,
        _refuse_unproven_edges,
        _refuse_unsilent_executables,
    )

    if getattr(disposition, "replay", False):
        # W3d AF2/AF3: try_apply already ran the frozen preflight (refuse_frozen_drift)
        # for every frozen fast run; the lock-only edge walk re-judges the hit's edges.
        _refuse_unproven_edges(project_root, lock, [])
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
    if not vr._RUN_FROZEN or vr.run_disposition() == "plain":
        # Provenance never upgrades (W3d A5): this run's output record stays attested.
        # A frozen hit records nothing (AF5): the plain attestation keeps serving.
        vr.note_verified_noop(vr.ATTESTED)
    return True


def refuse_frozen_drift(project_root: Path) -> None:
    """The frozen preflight owner itself (InstallService.enforce_frozen), live.

    Its notices are taped, not printed: the render replays them at the full run's
    print point (``replay_frozen_notices``), so one judgment serves both (#5491).
    """
    from apm_cli.core.scope import InstallScope, manifest_file
    from apm_cli.install.diff_apply import Decline
    from apm_cli.install.errors import FrozenInstallError
    from apm_cli.install.request import InstallRequest
    from apm_cli.install.service import InstallService
    from apm_cli.integration.mcp_config_view import PreflightNoticeTape
    from apm_cli.models.apm_package import APMPackage

    _FROZEN_NOTICES.pop(str(project_root), None)
    manifest = APMPackage.from_apm_yml(manifest_file(project_root))
    tape = PreflightNoticeTape()
    try:
        request = InstallRequest.frozen_preflight(
            manifest, scope=InstallScope.PROJECT, logger=tape, speculative=True
        )
        InstallService.enforce_frozen(request, allow_pending_mcp=True)  # a bare install
    except FrozenInstallError:
        raise Decline("frozen preflight") from None  # the full run renders the refusal
    _FROZEN_NOTICES[str(project_root)] = tape.calls


_FROZEN_NOTICES: dict[str, list[tuple[str, str]]] = {}


def take_frozen_notices(project_root: Path) -> list[tuple[str, str]] | None:
    """This run's taped frozen preflight notices (consumed), or None."""
    return _FROZEN_NOTICES.pop(str(project_root), None)


def replay_frozen_notices(calls: list[tuple[str, str]], logger: Any) -> None:
    """Print taped frozen preflight notices on *logger*, in order.

    A taped trust notice goes through its owner's dedupe now, as the preflight's
    own print would have (mcp_config_view._progress_trust_once).
    """
    from apm_cli.integration import mcp_config_view

    for name, message in calls:
        if name == "trust_once":
            mcp_config_view._progress_trust_once(logger, message)
        else:
            getattr(logger, name)(message)


# Every frozen-only owner, classified for a frozen hit served by an attestation
# (W3d AF2). "live": re-judged on the hit; "implied": the MAC'd inputs (manifest,
# lock, governed trees, approvals, config, policy cache) plus the no-follow
# re-hash of every deployed file and package copy (A7) fix its answer, or the
# fast path declines the shape. tests/integration/test_verdict_record.py keeps
# this closed: a new frozen owner fails until it is classified here.
FROZEN_OWNERS: dict[str, tuple[str, str]] = {
    "service:enforce_frozen": ("live", "the preflight owner runs before any fast decision"),
    "service:_refuse_frozen_dropped_targets": ("live", "inside enforce_frozen"),
    "service:_verify_frozen_local_catalogs": ("live", "inside enforce_frozen"),
    "service:_verify_frozen_local_content": ("live", "inside enforce_frozen"),
    "service:_frozen_mcp_reasons": ("live", "inside enforce_frozen (hydrated MCP)"),
    "service:enforce_frozen_services": ("live", "run_services(frozen_disposition)"),
    "diff_apply:_refuse_unproven_edges": ("live", "lock-only edge walk on the hit"),
    "service:frozen_disposition": ("live", "keyed in the MAC (AF1)"),
    "service:frozen_replay_lockfile": ("implied", "lock bytes are MAC'd"),
    "service:frozen_writes": ("implied", "the hit commits an empty diff"),
    "service:_enforce_frozen_no_rewrite": ("implied", "the hit never writes the lock"),
    "service:enforce_frozen_restored": ("implied", "package copies re-hashed (A7)"),
    "service:_enforce_frozen_deployed_hashes": (
        "live",
        "A7 re-hash; build id MAC'd; native plugins decline",
    ),
    "service:enforce_frozen_selections": ("implied", "manifest, lock and targets MAC'd"),
    "service:enforce_frozen_hook_pins": ("implied", "hook bytes A7; approvals MAC'd"),
    "service:reject_frozen_mutation": ("implied", "only bare install argv is served"),
    "service:reject_missing_frozen_root": ("implied", "only bare install argv is served"),
    "frozen_mcp_drift": ("live", "inside enforce_frozen (_frozen_mcp_reasons)"),
    "frozen_selection": ("implied", "manifest, lock and targets MAC'd"),
    "frozen_withdrawal": ("implied", "the hit deploys nothing"),
    "frozen_retirement": ("implied", "exec gate live; deployed bytes re-hashed (A7)"),
    "local_content_verdict": ("live", "inside enforce_frozen; local rows decline"),
    "plan:manifest_requires_lockfile": ("live", "inside enforce_frozen"),
}
