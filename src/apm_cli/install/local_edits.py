"""Local edits to APM-managed files: detection and the export hint.

Single owner of the ``apm export-patch`` hint wording shown by
``apm audit`` (drift text output) and ``apm install`` (before it re-syncs
managed files), of which owners count as exportable upstream, and of the
read-only install-time check that counts locally edited managed files.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext
    from apm_cli.install.drift import DriftFinding

_SELF_KEY = "."


def local_edit_hint(count: int, *, installing: bool = False) -> str:
    """Return the one-line hint pointing at ``apm export-patch``."""
    noun = f"{count} local edit(s) to managed files"
    if installing:
        return (
            f"{noun} may be overwritten by this install; next time, run "
            "'apm export-patch' first to export them as upstream patches"
        )
    return (
        f"{noun}; run 'apm export-patch' to export them as upstream patches "
        "before the next install overwrites them"
    )


def is_upstream_package(key: str, lockfile: LockFile | None) -> bool:
    """Return True when edits owned by *key* can be exported upstream.

    Project-local content and local-path packages are excluded: their
    sources already live on disk. An empty key (owner not recorded on the
    finding) stays eligible because ``apm export-patch`` resolves it from
    tracked directory prefixes.
    """
    if key == _SELF_KEY:
        return False
    if lockfile is None or not key:
        return True
    dep = lockfile.get_dependency(key)
    return dep is not None and dep.source != "local" and not dep.local_path


def count_exportable_findings(
    findings: Iterable[DriftFinding], lockfile: LockFile | None = None
) -> int:
    """Count ``modified`` drift findings owned by an upstream package."""
    return sum(
        1
        for finding in findings
        if finding.kind == "modified" and is_upstream_package(finding.package, lockfile)
    )


def count_local_edits(
    project_root: Path,
    lockfile: LockFile | None,
    dependency_keys: Collection[str],
) -> int:
    """Count managed files of upstream *dependency_keys* edited since deployment.

    Read-only. A file counts when it still exists in the project and its
    content hash differs from the hash recorded at deployment. Files
    without a recorded hash, symlinks, and paths escaping the project are
    ignored.
    """
    if lockfile is None or not dependency_keys:
        return 0

    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.utils.content_hash import compute_file_hash
    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

    remote_keys = [key for key in dependency_keys if key and is_upstream_package(key, lockfile)]
    if not remote_keys:
        return 0

    snapshot = DeploymentLedgerCodec.cleanup_snapshot(lockfile, remote_keys)
    count = 0
    for rel_path, recorded in snapshot.hashes.items():
        if rel_path.endswith("/"):
            continue
        path = project_root / rel_path
        try:
            if path.is_symlink() or not path.is_file():
                continue
            ensure_path_within(path, project_root)
            if compute_file_hash(path) != recorded:
                count += 1
        except (OSError, PathTraversalError):
            continue
    return count


def hint_local_edits_before_install(ctx: InstallContext, lockfile: LockFile | None) -> None:
    """Show the export-patch hint once before a project install re-syncs files.

    Skipped for dry runs, lockfile-only resolution and user-scope installs,
    which do not overwrite project files. Never blocks the install.
    """
    from apm_cli.core.scope import InstallScope

    if (
        ctx.logger is None
        or not lockfile
        or ctx.lockfile_only
        or ctx.dry_run
        or ctx.scope != InstallScope.PROJECT
    ):
        return
    keys = {dep.get_unique_key() for dep in ctx.deps_to_install}
    edited = count_local_edits(ctx.project_root, lockfile, keys)
    if edited:
        ctx.logger.info(local_edit_hint(edited, installing=True))
