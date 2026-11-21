"""Value object for a selected-owner target contraction (#3656).

:func:`apm_cli.install.manifest_reconcile.reconcile_target_deployed_files`
fills it while it removes the selected dependencies' files, so a refused
uninstall can still write an ``apm.lock.yaml`` that claims only the files
left on disk.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile


@dataclass
class SelectedCleanupOutcome:
    """What a selected-owner removal left on disk, for a truthful lockfile on failure.

    ``claims`` maps each selected dependency to the files and hashes it still
    owns after cleanup, including preserved rows that never entered cleanup;
    ``retained_paths`` lists the cleaned files that no non-selected package
    claims, which block the removal.
    """

    claims: dict[str, tuple[list[str], dict[str, str]]] = field(default_factory=dict)
    retained_paths: set[str] = field(default_factory=set)

    def record(
        self,
        dep_key: str,
        dependency: LockedDependency,
        files: list[str],
        hashes: dict[str, str],
        *,
        survivors: AbstractSet[str],
        preserved: AbstractSet[str],
    ) -> None:
        kept = [path for path in dependency.deployed_files if path in preserved]
        recorded = dependency.deployed_file_hashes
        self.claims[dep_key] = (
            [*files, *kept],
            {**{path: recorded[path] for path in kept if path in recorded}, **hashes},
        )
        self.retained_paths.update(
            path for path in files if path not in survivors and "://" not in path
        )

    def apply_to(self, lockfile: LockFile) -> None:
        """Make the selected dependencies claim only the files still on disk."""
        from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

        DeploymentLedgerCodec.replace_legacy_owners(lockfile, self.claims)
