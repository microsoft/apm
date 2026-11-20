"""Withdraw what a refused ``--frozen`` install deployed outside the lock (#4309).

``--frozen`` never rewrites ``apm.lock.yaml``, so an install that deploys a
file the lock does not record must fail. A failed command preserves durable
state (I7): before refusing, the frozen owner removes exactly the unrecorded
files THIS invocation wrote, and only the directories this invocation
created. The proof is a journal taken before the pipeline: this
installation's deployment receipts and the target directories that already
existed. A receipt that is new or changed since then proves this invocation
wrote the file; anything else (an adopted file, a file edited since, one that
could not be removed) is kept and named with the next step.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core import remediation

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from apm_cli.deps.lockfile import LockFile

# Deep enough for every deployed primitive layout below a target root.
_DIRECTORY_DEPTH = 6

_KEPT_REASONS = {
    "adopted": (
        "already on disk before this install (not written by it)",
        remediation.FROZEN_KEPT_ADOPTED,
    ),
    "edited": ("edited after this install wrote it", remediation.FROZEN_KEPT_EDITED),
    "failed": ("could not be removed", remediation.FROZEN_KEPT_UNREMOVABLE),
}


@dataclass(frozen=True)
class WriteJournal:
    """What existed before a frozen install deployed: receipts and directories."""

    receipts: Mapping[str, str]
    directories: frozenset[str]


class WithheldWrites(list):
    """Lockfile payloads a frozen install withheld, with the journal taken before it."""

    journal: WriteJournal | None = None


def _receipts(deploy_root: Path, lockfile_dir: Path, *, user_scope: bool) -> dict[str, str]:
    from apm_cli.integration.cleanup import DeploymentEvidence
    from apm_cli.utils.diagnostics import DiagnosticCollector

    return DeploymentEvidence.collect(
        deploy_root,
        user_scope=user_scope,
        lockfile=None,
        lockfile_root=lockfile_dir,
        diagnostics=DiagnosticCollector(),
        kept=(),
    ).receipt_hashes


def _directories(deploy_root: Path, roots: Iterable[str]) -> frozenset[str]:
    """Directories that exist now under each target root, to a bounded depth."""
    found = {deploy_root.resolve().as_posix()}
    for root_dir in roots:
        root = deploy_root / root_dir
        if not root.is_dir():
            continue
        base = len(root.parts)
        for current, children, _files in os.walk(root):
            found.add(Path(current).resolve().as_posix())
            if len(Path(current).parts) - base >= _DIRECTORY_DEPTH:
                children.clear()
    return frozenset(found)


def take_write_journal(lockfile_path: Path) -> WriteJournal | None:
    """Snapshot receipts and target directories before a frozen deploy."""
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.policy.ci_checks import resolve_audit_scope

    committed = LockFile.read(lockfile_path)
    if committed is None:
        return None
    deploy_root, user_scope, targets = resolve_audit_scope(lockfile_path.parent, committed)
    receipts = _receipts(deploy_root, lockfile_path.parent, user_scope=user_scope)
    return WriteJournal(
        # A receipt proves an earlier write only while its file is still on disk;
        # once the user moved it aside, rewriting it is this invocation's write.
        receipts={
            path: digest
            for path, digest in receipts.items()
            if Path(path).exists() or Path(path).is_symlink()
        },
        directories=_directories(deploy_root, {target.root_dir for target in targets}),
    )


@contextmanager
def frozen_capture(lockfile_path: Path) -> Iterator[WithheldWrites]:
    """Withhold lockfile writes (req-lk-006) and journal what existed before."""
    from apm_cli.deps.lockfile import suppress_lockfile_writes

    captured = WithheldWrites()
    captured.journal = take_write_journal(lockfile_path)
    with suppress_lockfile_writes() as withheld:
        try:
            yield captured
        finally:
            captured.extend(withheld)


@dataclass
class Withdrawal:
    """The outcome of withdrawing one refused install's unrecorded files."""

    removed: list[str] = field(default_factory=list)
    kept: dict[str, str] = field(default_factory=dict)


def _unrecorded_files(
    candidates: Iterable[LockFile], unclaimed: set[str], deploy_root: Path
) -> Iterator[tuple[str, str]]:
    """Yield ``(owner, file)`` for every file under an unrecorded claim."""
    seen: set[str] = set()
    for candidate in candidates:
        rows = [(key, dep.deployed_files) for key, dep in candidate.dependencies.items()]
        rows.append((".", candidate.local_deployed_files))
        for owner, claims in rows:
            for claim in claims:
                if claim not in unclaimed or "://" in claim:
                    continue
                physical = deploy_root / claim
                members = (
                    [p for p in sorted(physical.rglob("*")) if p.is_file() or p.is_symlink()]
                    if physical.is_dir() and not physical.is_symlink()
                    else [physical]
                )
                for member in members:
                    rel = member.relative_to(deploy_root).as_posix()
                    if rel not in seen and (member.is_file() or member.is_symlink()):
                        seen.add(rel)
                        yield owner, rel


def withdraw_unrecorded_deployments(
    candidates: Iterable[LockFile],
    unclaimed: set[str],
    *,
    committed_paths: Iterable[str],
    deploy_root: Path,
    user_scope: bool,
    journal: WriteJournal | None,
    receipts_after: Mapping[str, str],
) -> Withdrawal:
    """Remove the unrecorded files this invocation wrote; name every file kept."""
    from apm_cli.integration.base_integrator import BaseIntegrator
    from apm_cli.integration.cleanup import DeploymentEvidence, remove_stale_deployed_files
    from apm_cli.utils.diagnostics import DiagnosticCollector

    before = journal.receipts if journal is not None else receipts_after
    fresh = {path: digest for path, digest in receipts_after.items() if before.get(path) != digest}
    protected = {(deploy_root / path).resolve().as_posix() for path in committed_paths}
    outcome = Withdrawal()
    by_owner: dict[str, list[str]] = {}
    for owner, rel in _unrecorded_files(candidates, unclaimed, deploy_root):
        resolved = (deploy_root / rel).resolve().as_posix()
        if resolved in protected:
            continue  # the committed lock records it: never this refusal's to undo
        if resolved not in fresh:
            outcome.kept[rel] = "adopted"
        else:
            by_owner.setdefault(owner, []).append(rel)
    # Only this invocation's receipts authorize; the cleanup owner retires them.
    evidence = DeploymentEvidence(
        deploy_root=deploy_root,
        receipts=fresh,
        render=None,
        user_scope=user_scope,
        kept=(),
    )
    deleted: list[Path] = []
    for owner, paths in by_owner.items():
        result = remove_stale_deployed_files(
            paths,
            deploy_root,
            dep_key=owner,
            targets=None,
            diagnostics=DiagnosticCollector(),
            evidence=evidence,
            failed_path_retained=False,
            user_scope=user_scope,
        )
        outcome.removed.extend(result.deleted)
        deleted.extend(result.deleted_targets)
        outcome.kept.update(dict.fromkeys(result.skipped_user_edit, "edited"))
        outcome.kept.update(dict.fromkeys(result.failed, "failed"))
        outcome.kept.update(
            dict.fromkeys((p for p in result.skipped_unmanaged if p not in outcome.kept), "adopted")
        )
    _remove_created_directories(deleted, deploy_root, journal, BaseIntegrator)
    return outcome


def _remove_created_directories(
    deleted: Sequence[Path], deploy_root: Path, journal: WriteJournal | None, integrator
) -> None:
    """Remove emptied directories this invocation created, never older ones."""
    existing = journal.directories if journal is not None else frozenset()
    for path in deleted:
        stop = path.parent
        while stop != deploy_root and stop.resolve().as_posix() not in existing:
            if stop.parent == stop:
                break
            stop = stop.parent
        if journal is None:
            stop = path.parent  # no journal: no directory is provably this invocation's
        integrator.cleanup_empty_parents([path], stop)


def kept_lines(kept: Mapping[str, str]) -> list[str]:
    """Name each kept file with its reason and the runnable next step."""
    return [
        f"  - {remediation.kept_file_message(path, *_KEPT_REASONS[reason])}"
        for path, reason in sorted(kept.items())
    ]


def refusal_tip(kept: Mapping[str, str]) -> str:
    """The refusal's tip: what was withdrawn, and how to record the deployment."""
    removed = "the other unrecorded files were" if kept else "the unrecorded files were"
    return (
        f"Tip: {removed} removed again and apm.lock.yaml is unchanged. "
        f"{remediation.FROZEN_LOCKFILE_WITHHELD.sentence()}"
    )


def refuse_unrecorded_deployments(
    withheld: Iterable[str], unclaimed: set[str], committed: LockFile, lockfile_dir: Path
) -> Exception:
    """Withdraw this invocation's unrecorded files and return the ``--frozen`` refusal."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.errors import FrozenInstallError
    from apm_cli.install.service import _outermost
    from apm_cli.policy.ci_checks import resolve_audit_scope

    deploy_root, user_scope, _targets = resolve_audit_scope(lockfile_dir, committed)
    outcome = withdraw_unrecorded_deployments(
        [LockFile.from_yaml(payload) for payload in withheld],
        unclaimed,
        committed_paths=DeploymentLedgerCodec.legacy_deployed_file_claims(committed),
        deploy_root=deploy_root,
        user_scope=user_scope,
        journal=getattr(withheld, "journal", None),
        receipts_after=_receipts(deploy_root, lockfile_dir, user_scope=user_scope),
    )
    reasons = [
        f"  - {path} is deployed by this install but not recorded in apm.lock.yaml"
        for path in _outermost(unclaimed)
    ]
    reasons += kept_lines(outcome.kept)
    return FrozenInstallError(
        "--frozen: apm.lock.yaml does not record everything this install deploys.",
        reasons=reasons,
        tip=refusal_tip(outcome.kept),
    )
