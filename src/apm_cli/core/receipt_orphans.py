"""Receipt-bound cleanup of deployed files no lock entry claims (#3188).

The lockfile stays the declarative contract. When a teammate's commit drops
a package from both ``apm.yml`` and ``apm.lock.yaml``, no lock entry names
the files it deployed here any more, but this installation's own deployment
receipts still do. Under req-lk-024 (amendment 0.1.77) such a file may be
deleted only when the receipt binds to this project root and installation,
the file still holds the receipted bytes, no current or freshly deployed
owner claims the path, and a complete, successful, non-narrowed install or
prune runs. An edited file is kept and named. Audit reports the same
selection read-only.

This module is the single selector for install, prune and audit; deletion
routes through ``remove_stale_deployed_files`` and receipt retirement
through ``DeploymentMaterializationSession``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec, DeploymentMaterializationSession
from apm_cli.core.deployment_state import LocatorKind
from apm_cli.utils.paths import portable_relpath

if TYPE_CHECKING:
    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.integration.cleanup import CleanupResult


@dataclass(frozen=True)
class ReceiptOrphan:
    """A receipted file no current lock entry or fresh deployment claims."""

    path: str
    """Deployment-root-relative POSIX path."""

    held: bool
    """Whether the file still holds exactly the receipted bytes."""


def _spelling(path: Path) -> str:
    # Case- and normalization-insensitive filesystems spell one entry many
    # ways; any spelling of a claimed path keeps the file.
    return unicodedata.normalize("NFC", path.as_posix()).casefold()


def _claimed_values(lockfiles: Iterable[LockFile | None], deployed: Iterable[str]) -> set[str]:
    values = set(deployed)
    for lockfile in lockfiles:
        if lockfile is None:
            continue
        values.update(DeploymentLedgerCodec.legacy_deployed_file_claims(lockfile))
        values.update(
            DeploymentLedgerCodec.legacy_value(record.locator)
            for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values()
            if record.locator.kind != LocatorKind.URI
        )
    return {value.replace("\\", "/").rstrip("/") for value in values if "://" not in value}


def _claimed_spellings(root: Path, values: Iterable[str]) -> frozenset[str]:
    spellings: set[str] = set()
    for value in values:
        path = Path(value)
        spellings.add(_spelling((path if path.is_absolute() else root / path).resolve()))
    return frozenset(spellings)


def _is_claimed(path: Path, claimed: frozenset[str]) -> bool:
    return any(_spelling(entry) in claimed for entry in (path, *path.parents))


def _bound_session(
    deploy_root: Path, scope: InstallScope
) -> DeploymentMaterializationSession | None:
    """This installation's receipts, or ``None`` when none bind (moved or invalid)."""
    try:
        return DeploymentMaterializationSession(deploy_root, scope, None)
    except (OSError, ValueError):
        return None


def _select(
    session: DeploymentMaterializationSession,
    lockfiles: Iterable[LockFile | None],
    deployed: Iterable[str],
) -> tuple[ReceiptOrphan, ...]:
    root = session.root
    claimed = _claimed_spellings(root, _claimed_values(lockfiles, deployed))
    orphans: list[ReceiptOrphan] = []
    for physical in sorted(session.receipt_hashes()):
        path = Path(physical)
        if (
            not path.is_relative_to(root)
            or _is_claimed(path, claimed)
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        orphans.append(ReceiptOrphan(portable_relpath(path, root), session.receipt_held(path)))
    return tuple(orphans)


def receipt_orphans(
    deploy_root: Path,
    scope: InstallScope,
    lockfile: LockFile | None,
    *,
    prior: LockFile | None = None,
    deployed: Iterable[str] = (),
) -> tuple[ReceiptOrphan, ...]:
    """Read-only: receipted files under *deploy_root* nothing current claims.

    *lockfile* is the current (committed or staged) lock; *prior* is the lock
    the operation started from, whose own cleanup decides its claims; and
    *deployed* names lock values this operation itself deployed. Without a
    lockfile nothing is selected: an absent lock is no evidence that a file
    is unclaimed.
    """
    if lockfile is None:
        return ()
    session = _bound_session(deploy_root, scope)
    if session is None:
        return ()
    return _select(session, (lockfile, prior), deployed)


def remove_receipt_orphans(
    deploy_root: Path,
    scope: InstallScope,
    lockfile: LockFile | None,
    *,
    diagnostics,
    prior: LockFile | None = None,
    deployed: Iterable[str] = (),
) -> CleanupResult:
    """Delete receipted files nothing current claims and that hold their receipted bytes.

    Callers invoke this only after a complete, successful, non-narrowed
    install or prune; arguments are those of :func:`receipt_orphans`. An
    edited file is kept with its receipt and named in one diagnostic;
    deleted files retire their receipt rows, and only directories left
    empty are removed.
    """
    from apm_cli.integration.base_integrator import BaseIntegrator
    from apm_cli.integration.cleanup import (
        CleanupResult,
        DeploymentEvidence,
        remove_stale_deployed_files,
    )

    user_scope = scope.value == "user"
    session = _bound_session(deploy_root, scope) if lockfile is not None else None
    if session is None:
        return CleanupResult(user_scope=user_scope)
    orphans = _select(session, (lockfile, prior), deployed)
    if not orphans:
        return CleanupResult(user_scope=user_scope)
    root = session.root
    result = remove_stale_deployed_files(
        [orphan.path for orphan in orphans],
        root,
        dep_key="",
        targets=None,
        diagnostics=diagnostics,
        # Receipts only: no lock entry names these files, so there is no
        # locked render, and a receipt mismatch keeps the file as edited.
        evidence=DeploymentEvidence(
            deploy_root=root,
            receipts=session.receipt_hashes(),
            render=None,
            user_scope=user_scope,
            kept=(),
        ),
        failed_path_retained=False,
        user_scope=user_scope,
    )
    BaseIntegrator.cleanup_empty_parents(result.deleted_targets, stop_at=root)
    return result
