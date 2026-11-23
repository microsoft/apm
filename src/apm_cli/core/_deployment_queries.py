"""Read-only projections of recorded deployment evidence for the ledger codec."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, ValuesView

    from apm_cli.core.deployment_state import DeploymentRecord
    from apm_cli.deps.lockfile import LockFile


def recorded_rows(lockfile: LockFile) -> ValuesView[DeploymentRecord] | None:
    """Return recorded rows, or None if the only possible evidence is legacy inference."""
    ledger = lockfile.deployment_ledger
    if not lockfile._deployments_present or ledger is None:
        return None
    return ledger.records.values()


def group_owner_records(
    records: Iterable[DeploymentRecord],
) -> dict[str, tuple[DeploymentRecord, ...]]:
    """Index already scope-filtered deployment rows by recorded owner."""
    grouped: dict[str, list[DeploymentRecord]] = {}
    for record in records:
        for owner in dict.fromkeys(record.owners):
            grouped.setdefault(owner, []).append(record)
    return {owner: tuple(rows) for owner, rows in grouped.items()}
