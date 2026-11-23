"""Carry deployment receipts to a re-anchored local owner (#4889).

Re-anchoring renames a kept local dependency's lock key (``./vendor/e`` ->
``local:vendor/e``). The bytes on disk and the writer are unchanged, so every
receipt of the old owner moves to the new one with its hash history:
``bind_committed_claims`` cannot do this, since it rebinds only an unchanged
record and forgets history on a new binding (#4428).

Before any write, :func:`reanchor_receipt_problems` proves each receipted
file of a renamed owner is bound to that owner under the current lock. A lost
or forged binding refuses the re-anchor, so no deletion evidence is ever
invented and no file is deleted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .deployment_ledger import DeploymentLedgerCodec, DeploymentMaterializationSession


def _renamed_claims(lockfile: Any, renames: Mapping[str, str]):
    """The lock's content-bearing records whose active owner is renamed."""
    for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values():
        if record.active_owner in renames and record.content_hash is not None:
            yield record


class _ReanchorSession(DeploymentMaterializationSession):
    """The materialization session, extended with an owner rename only."""

    def problems(self, lockfile: Any, renames: Mapping[str, str]) -> list[str]:
        problems = []
        for claim in _renamed_claims(lockfile, renames):
            value = DeploymentLedgerCodec.legacy_value(claim.locator)
            receipt = self._records.get(claim.locator.key)
            expected = [claim.active_owner, self._claim_signature(value)]
            if (
                receipt is None
                or receipt.owners != claim.owners
                or receipt.active_owner != claim.active_owner
                or self._bindings.get(claim.locator.key) != expected
            ):
                problems.append(value)
        return sorted(problems)

    def rename_owners(self, renames: Mapping[str, str]) -> None:
        """Move the renamed owners' receipts to the committed claims this session reads."""
        for key, record in list(self._records.items()):
            if record.active_owner not in renames:
                continue
            owners = tuple(renames.get(owner, owner) for owner in record.owners)
            renamed = replace(record, owners=owners, active_owner=renames[record.active_owner])
            value = DeploymentLedgerCodec.legacy_value(record.locator)
            self._records[key] = renamed
            # Same physical bytes, same writer: the hash history stays (#4889).
            self._bindings[key] = [renamed.active_owner, self._claim_signature(value)]
            self._dirty = True
        self.flush()


def reanchor_receipt_problems(
    project_root: Path, scope: Any, lockfile: Any, renames: Mapping[str, str]
) -> list[str]:
    """Return the renamed owners' files without an intact receipt binding."""
    renames = {old: new for old, new in renames.items() if old != new}
    if not renames:
        return []
    try:
        session = _ReanchorSession(project_root, scope, lockfile)
    except ValueError:
        return sorted(
            DeploymentLedgerCodec.legacy_value(claim.locator)
            for claim in _renamed_claims(lockfile, renames)
        ) or ["deployment receipts"]
    return session.problems(lockfile, renames)


def rename_receipt_owners(
    project_root: Path, scope: Any, committed: Any, renames: Mapping[str, str]
) -> None:
    """Rebind the renamed owners' receipts to the *committed* lock, carrying hash history.

    Receipts are read fresh, so writes this command already recorded stay.
    """
    renames = {old: new for old, new in renames.items() if old != new}
    if renames:
        _ReanchorSession(project_root, scope, committed).rename_owners(renames)
