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
from .deployment_state import LocatorKind


def _renamed_claims(lockfile: Any, renames: Mapping[str, str]):
    """The lock's file records any renamed owner claims, shared ones included.

    A missing lock hash never exempts a file: only a receipt proves who wrote it.
    """
    for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values():
        if record.locator.kind != LocatorKind.URI and any(
            owner in renames for owner in record.owners
        ):
            yield record


class _ReanchorSession(DeploymentMaterializationSession):
    """The materialization session, extended with an owner rename only."""

    def problems(self, lockfile: Any, renames: Mapping[str, str]) -> list[str]:
        """Claims whose receipt the rename would carry but cannot prove.

        A receipt records its actual writer, so it is authenticated by that
        writer and the claim's canonical signature, never by the claim's full
        owner set. A renamed writer's claim must hold its receipt.
        """
        problems = []
        # A receipt's writer must already own something in this lock: the
        # rename must never turn an untrusted writer into a trusted one.
        valid_writers = DeploymentLedgerCodec.valid_owner_keys(lockfile)
        for claim in _renamed_claims(lockfile, renames):
            value = DeploymentLedgerCodec.legacy_value(claim.locator)
            receipt = self._records.get(claim.locator.key)
            if receipt is None:
                # Only a genuine directory claim needs no receipt; a null lock
                # hash never makes a file claim exempt.
                if claim.active_owner in renames and (
                    claim.content_hash is not None or not self._is_directory(value)
                ):
                    problems.append(value)
                continue
            if receipt.active_owner not in valid_writers:
                problems.append(value)
                continue
            expected = [receipt.active_owner, self._claim_signature(value)]
            if (
                claim.active_owner in renames and receipt.active_owner != claim.active_owner
            ) or self._bindings.get(claim.locator.key) != expected:
                problems.append(value)
        return sorted(problems)

    def carried_problems(self, lockfile: Any, carried: Mapping[str, str]) -> list[str]:
        """Carried files whose receipt does not prove the deployed bytes (#5048).

        A path moves to a new writer only when its receipt is bound to the
        current claim by a trusted writer and records exactly the bytes on disk.
        """
        from apm_cli.utils.content_hash import compute_file_hash

        claims = {
            DeploymentLedgerCodec.legacy_value(record.locator).rstrip("/"): record
            for record in DeploymentLedgerCodec.from_lockfile(lockfile).records.values()
        }
        valid_writers = DeploymentLedgerCodec.valid_owner_keys(lockfile)
        problems = []
        for value in sorted(carried):
            claim = claims.get(value)
            if claim is not None and self._is_directory(value):
                continue
            receipt = claim and self._records.get(claim.locator.key)
            path = Path(value) if Path(value).is_absolute() else self.root / value
            try:
                proven = (
                    receipt is not None
                    and receipt.active_owner in valid_writers
                    and self._bindings.get(claim.locator.key)
                    == [receipt.active_owner, self._claim_signature(value)]
                    and path.is_file()
                    and not path.is_symlink()
                    and receipt.content_hash == compute_file_hash(path)
                )
            except OSError:
                proven = False
            if not proven:
                problems.append(value)
        return problems

    def _is_directory(self, value: str) -> bool:
        path = Path(value) if Path(value).is_absolute() else self.root / value
        return path.is_dir() and not path.is_symlink()

    def rename_owners(
        self, renames: Mapping[str, str], carried: Mapping[str, str] | None = None
    ) -> None:
        """Move receipts to the committed claims this session reads, writer and history kept.

        A *carried* path (#5048) moves to the writer a fresh install records:
        its bytes were proven equal to that writer's render, so history stays.
        """
        carried = carried or {}
        new_owners = set(renames.values())
        for key, record in list(self._records.items()):
            value = DeploymentLedgerCodec.legacy_value(record.locator)
            # Only receipts the validated owner transition covers move: the
            # committed claim must name a new owner, or the path is one the
            # preflight proved for carrying. A stale receipt is untouched.
            if value.rstrip("/") not in carried and not any(
                owner in new_owners
                for claim in self._claims.get(value, ())
                for owner in claim.owners
            ):
                continue
            owners = tuple(renames.get(owner, owner) for owner in record.owners)
            active = renames.get(record.active_owner, record.active_owner)
            if writer := carried.get(value.rstrip("/")):
                owners, active = (writer,), writer
            renamed = replace(record, owners=owners, active_owner=active)
            self._records[key] = renamed
            # Same physical bytes, same writer: the hash history stays (#4889).
            self._bindings[key] = [renamed.active_owner, self._claim_signature(value)]
            self._dirty = True
        self.flush()


def reanchor_receipt_problems(
    project_root: Path,
    scope: Any,
    lockfile: Any,
    renames: Mapping[str, str],
    carried: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the renamed owners' and carried (#5048) files without a proving receipt."""
    renames = {old: new for old, new in renames.items() if old != new}
    carried = carried or {}
    if not renames:
        return []
    try:
        session = _ReanchorSession(project_root, scope, lockfile)
    except ValueError:
        return sorted(
            {
                *(
                    DeploymentLedgerCodec.legacy_value(claim.locator)
                    for claim in _renamed_claims(lockfile, renames)
                ),
                *carried,
            }
        ) or ["deployment receipts"]
    return sorted(
        {*session.problems(lockfile, renames), *session.carried_problems(lockfile, carried)}
    )


def rename_receipt_owners(
    project_root: Path,
    scope: Any,
    committed: Any,
    renames: Mapping[str, str],
    carried: Mapping[str, str] | None = None,
) -> None:
    """Rebind the renamed owners' receipts to the *committed* lock, carrying hash history.

    Receipts are read fresh, so writes this command already recorded stay.
    """
    renames = {old: new for old, new in renames.items() if old != new}
    if renames:
        _ReanchorSession(project_root, scope, committed).rename_owners(renames, carried)
