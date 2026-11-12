"""Local edits to APM-managed files: detection and the export hint.

Single owner of the ``apm export-patch`` hint wording shown by
``apm audit`` (drift text output) and ``apm install`` (before it re-syncs
managed files), and of the read-only install-time check that counts
locally edited managed files.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile


def local_edit_hint(count: int, *, installing: bool = False) -> str:
    """Return the one-line hint pointing at ``apm export-patch``."""
    noun = f"{count} local edit(s) to managed files"
    if installing:
        return (
            f"{noun} may be overwritten by this install; run 'apm export-patch' "
            "before installing to export them as upstream patches"
        )
    return (
        f"{noun}; run 'apm export-patch' to export them as upstream patches "
        "before the next install overwrites them"
    )


def count_local_edits(
    project_root: Path,
    lockfile: LockFile | None,
    dependency_keys: Collection[str],
) -> int:
    """Count managed files of remote *dependency_keys* edited since deployment.

    Read-only. A file counts when it still exists in the project and its
    content hash differs from the hash recorded at deployment. Local-path
    packages and project-local content are excluded: their sources already
    live on disk, so there is nothing to export upstream. Files without a
    recorded hash, symlinks, and paths escaping the project are ignored.
    """
    if lockfile is None or not dependency_keys:
        return 0

    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.utils.content_hash import compute_file_hash

    remote_keys = [
        key
        for key in dependency_keys
        if (dep := lockfile.dependencies.get(key)) is not None
        and dep.source != "local"
        and not dep.local_path
    ]
    if not remote_keys:
        return 0

    snapshot = DeploymentLedgerCodec.cleanup_snapshot(lockfile, remote_keys)
    root = project_root.resolve()
    count = 0
    for rel_path, recorded in sorted(snapshot.hashes.items()):
        if rel_path.endswith("/"):
            continue
        path = project_root / rel_path
        try:
            if path.is_symlink() or not path.is_file():
                continue
            if not path.resolve().is_relative_to(root):
                continue
            if compute_file_hash(path) != recorded:
                count += 1
        except OSError:
            continue
    return count
