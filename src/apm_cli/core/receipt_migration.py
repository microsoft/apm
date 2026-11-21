"""Migrate user-scope root receipts written before the user lockfile existed (#3772).

Until 0.38, ``apm install -g`` deployed user-root primitives (``~/.apm/agents``)
and receipted each write, but wrote no user lockfile. Those receipts are bound
to no lock claim, so a later install cannot trust them, and it treats APM's own
earlier output as user content. The first upgraded install adopts such a
receipt as the self-entry claim it would have had: only at user scope, only
for the root owner ``.``, only for a path the lock does not claim, and only
while the file still holds the receipted bytes. A hand-edited file never
qualifies.
"""

from __future__ import annotations

from typing import Any

_ROOT_OWNER = "."


class LocklessRootReceiptMigration:
    """Mixin for :class:`DeploymentMaterializationSession`; it uses the session's state."""

    def adopt_lockless_root_receipts(self: Any) -> list[str]:
        """Bind qualifying lockless root receipts to their claim; return their lock values."""
        from pathlib import Path

        if self.scope.value != "user":
            return []
        adopted: list[str] = []
        for key, record in self._records.items():
            value = record.locator.value
            physical = self._destinations.get(key)
            if (
                record.active_owner != _ROOT_OWNER
                or self._claims.get(value)
                or physical is None
                or not self._holds(Path(physical), record)
            ):
                continue
            self._claims[value] = [record]
            self._bindings[key] = [_ROOT_OWNER, self._claim_signature(value)]
            self._dirty = True
            adopted.append(value)
        return sorted(adopted)
