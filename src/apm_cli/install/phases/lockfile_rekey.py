"""Re-key pre-#5418 absolute local identities in an existing lockfile, once.

The write path of the #5418 migration. Install and update run it on the lock
they read before resolution; the partial-install merge runs it on its re-read
of the saved lock, so both merge rows under one key.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile


def rekey_legacy_local_identities(
    lockfile: LockFile | None,
    project_root: Path,
    modules_dir: Path | None = None,
    journal: Callable[[tuple], None] | None = None,
    before_rename: Callable[[LockFile, dict[str, str]], None] | None = None,
) -> dict[str, str]:
    """Re-key pre-#5418 absolute local identities in place; return old -> new keys.

    The write path of the migration (plain ``apm install`` / ``apm update``):
    each row :func:`apm_cli.deps.local_identity.legacy_local_renames` names
    takes its portable key and ``anchored_local_path``, children's
    ``declaring_parent``, the ledger owners and matching MCP omission
    evidence follow the rename map. With *modules_dir* and *journal*, its
    ``apm_modules/_local/<slot>`` copy moves to the new slot and *journal*
    receives the moves (the install transaction puts them back on rollback
    or a declined update). The owner stays the same row; nothing is deleted.
    A new key another row already holds refuses before any change.
    *before_rename* sees the lock, still under its legacy keys, and the
    rename map once nothing can refuse any more (the install proves the
    legacy receipts there).
    """
    if lockfile is None or not lockfile.dependencies:
        return {}
    from apm_cli.core.deployment_reanchor import (
        move_materializations,
        rekey_requests,
        rekey_rows,
        rename_ledger_owners,
    )
    from apm_cli.core.remediation import LOCAL_IDENTITY_DUPLICATE
    from apm_cli.deps.local_identity import legacy_local_renames
    from apm_cli.integration.mcp_config_view import rebind_rekeyed_omissions
    from apm_cli.utils.diagnostics import printable_ascii_text

    found = legacy_local_renames(lockfile.dependencies, project_root)
    if not found:
        return {}
    renames = {old: f"local:{anchor}" for old, anchor in found.items()}
    owners: dict[str, str] = {}
    for old, new in renames.items():
        if new in lockfile.dependencies or new in owners:
            label, legacy = printable_ascii_text(new), printable_ascii_text(old)
            raise RuntimeError(
                f"apm.lock.yaml records local package {label} twice: as {legacy} and as "
                f"{label}. " + LOCAL_IDENTITY_DUPLICATE.sentence()
            )
        owners[new] = old
    if before_rename is not None:
        before_rename(lockfile, renames)
    moves = []
    rekeyed = []
    for old, anchor in found.items():
        row = lockfile.dependencies[old]
        current = row.to_dependency_ref().get_install_path(modules_dir) if modules_dir else None
        legacy = dataclasses.replace(row)
        row.anchored_local_path = anchor
        rekeyed.append((legacy, row, project_root / anchor))
        if current is not None and current.is_dir():
            target = row.to_dependency_ref().get_install_path(modules_dir)
            if not target.exists():
                moves.append((current, target))
    if moves and journal is not None:
        journal(move_materializations(tuple(moves), modules_dir))
    rename_ledger_owners(lockfile, renames)
    rebind_rekeyed_omissions(lockfile, rekeyed)
    rekey_rows(lockfile, renames)
    for row in lockfile.dependencies.values():
        rekey_requests(row.replacement, renames)
    return renames


def legacy_local_keys(data: bytes, project_root: Path) -> set[str] | None:
    """The pre-#5418 absolute local keys the lockfile *data* records; ``None`` if unreadable."""
    from apm_cli.deps.local_identity import legacy_local_renames
    from apm_cli.deps.lockfile import LockFile

    try:
        lock = LockFile.from_yaml(data.decode("utf-8"))
    except (ValueError, KeyError, TypeError):
        return None
    return set(legacy_local_renames(lock.dependencies, project_root))


def with_portable_local_keys(lockfile: LockFile | None, project_root: Path) -> LockFile | None:
    """*lockfile* with its legacy absolute local identities re-keyed (no apm_modules move)."""
    rekey_legacy_local_identities(lockfile, project_root)
    return lockfile


class LegacyReceiptCarry:
    """Carry deployment receipts across the #5418 re-key, as uninstall does (#4889).

    :meth:`prove` runs before the rows change: only when every renamed owner's
    receipt is proven does the carry happen. :meth:`carry_after_commit`, once
    the lock is staged, rebinds them to the committed lock with their hash
    history, before this run binds its own writes; a lock that never commits
    leaves every receipt as it was.
    """

    def __init__(self, project_root: Path, scope: Any) -> None:
        self.project_root, self.scope = project_root, scope
        self.renames: dict[str, str] = {}

    def prove(self, legacy: LockFile, renames: dict[str, str]) -> None:
        from apm_cli.core.deployment_reanchor import reanchor_receipt_problems

        if not reanchor_receipt_problems(self.project_root, self.scope, legacy, renames):
            self.renames = dict(renames)

    def carry_after_commit(self, apm_dir: Path) -> None:
        if not self.renames:
            return
        from apm_cli.deps.lockfile_write_authority import after_lockfile_commit

        after_lockfile_commit(lambda: self._carry(apm_dir))

    def _carry(self, apm_dir: Path) -> None:
        from apm_cli.core.deployment_reanchor import rename_receipt_owners
        from apm_cli.deps.lockfile import LockFile, get_lockfile_path

        committed = LockFile.read(get_lockfile_path(apm_dir))
        rename_receipt_owners(self.project_root, self.scope, committed, self.renames)
