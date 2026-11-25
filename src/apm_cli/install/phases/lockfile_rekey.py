"""Re-key pre-#5418 absolute local identities in an existing lockfile, once.

The write path of the #5418 migration. Install and update run it on the lock
they read before resolution; the partial-install merge runs it on its re-read
of the saved lock, so both merge rows under one key.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile


def rekey_legacy_local_identities(
    lockfile: LockFile | None,
    project_root: Path,
    modules_dir: Path | None = None,
    journal: Callable[[tuple], None] | None = None,
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
    """
    if lockfile is None or not lockfile.dependencies:
        return {}
    from apm_cli.core.deployment_reanchor import move_materializations, rename_ledger_owners
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
    rows = list(lockfile.dependencies.items())
    lockfile.dependencies.clear()
    for key, row in rows:
        if row.declaring_parent in renames:
            row.declaring_parent = renames[row.declaring_parent]
        lockfile.dependencies[renames.get(key, key)] = row
    return renames


def with_portable_local_keys(lockfile: LockFile | None, project_root: Path) -> LockFile | None:
    """*lockfile* with its legacy absolute local identities re-keyed (no apm_modules move)."""
    rekey_legacy_local_identities(lockfile, project_root)
    return lockfile
