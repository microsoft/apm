"""A writer's own receipts for files a restored older lock no longer claims (#5399).

Taking an older ``apm.lock.yaml`` (for example when resolving a merge
conflict) drops the claim for a file a later install deployed, so the
session's :meth:`baseline` refuses it and install would report its own file
as unmanaged. This installation's receipt still proves which writer put
which bytes there. Only a receipt that the lock does not claim, whose bound
writer is the deploying owner and whose bytes the file still holds, counts.
A hand-made file has no such receipt and keeps the unmanaged handling. A
frozen replay verifies and reclaims nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .deployment_ledger import DeploymentLedgerCodec

_INDEX = "_unclaimed_receipt_keys"


def _unclaimed_keys(session: Any) -> dict[str, frozenset[str]]:
    """Receipt keys the lock does not claim, by bound writer: one pass per session."""
    index = session.__dict__.get(_INDEX)
    if index is None:
        grouped: dict[str, set[str]] = {}
        for key, record in session._records.items():
            # Bound to this writer under a lock claim; a legacy unbound receipt
            # (written with no claim) stays the user-scope migration's (#3772).
            binding = session._bindings.get(key) or [None, "[]"]
            if binding[0] != record.active_owner or binding[1] == "[]":
                continue
            if not session._claims.get(DeploymentLedgerCodec.legacy_value(record.locator)):
                grouped.setdefault(record.active_owner, set()).add(key)
        index = {owner: frozenset(keys) for owner, keys in grouped.items()}
        session.__dict__[_INDEX] = index
    return index


def has_unclaimed_receipts(session: Any, owner: str | None) -> bool:
    """Whether *owner* holds any receipt the current lock does not claim."""
    if session is None or owner is None or session.verify_only:
        return False
    return bool(_unclaimed_keys(session).get(owner))


def own_unclaimed_receipt(session: Any, path: Path, owner: str) -> str | None:
    """*owner*'s receipted hash for unclaimed *path* while the file holds those bytes."""
    if not has_unclaimed_receipts(session, owner):
        return None
    key = session._physical_keys.get(path.resolve().as_posix())
    if key is None or key not in _unclaimed_keys(session)[owner]:
        return None
    record = session._records[key]
    return record.content_hash if session._holds(path, record) else None
