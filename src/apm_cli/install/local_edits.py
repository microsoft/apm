"""Local edits to APM-managed files: detection and the export hint.

Single owner of the ``apm export-patch`` hint wording shown by
``apm audit`` (drift text output) and ``apm install`` (before it re-syncs
managed files), of which owners count as exportable upstream, and of the
read-only check that tells a local edit (on-disk bytes differ from the
locked hash) from a rendering change (bytes still match the lock but this
APM version renders them differently).
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


def rendering_change_hint(count: int) -> str:
    """Return the hint for drift whose on-disk bytes still match apm.lock.yaml."""
    return (
        f"{count} file(s) still match apm.lock.yaml but this APM version renders them "
        "differently; run 'apm install' and commit the result to adopt the new rendering"
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


def _differs_from_recorded(path: Path, resolved_root: Path, recorded: str) -> bool | None:
    """Return whether *path* differs from its *recorded* hash.

    Tri-state: ``True`` (edited), ``False`` (matches the lock) or ``None``
    when the file is missing, a symlink, escapes *resolved_root* or cannot be
    read. Callers must compare with ``is True`` / ``is False``, not truthiness.
    """
    from apm_cli.utils.content_hash import compute_file_hash
    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within_resolved

    try:
        if path.is_symlink() or not path.is_file():
            return None
        ensure_path_within_resolved(path, resolved_root)
        return compute_file_hash(path) != recorded
    except (OSError, PathTraversalError):
        return None


def unedited_modified_paths(
    findings: Iterable[DriftFinding], lockfile: LockFile | None, project_root: Path | None
) -> frozenset[str]:
    """Return ``modified`` finding paths whose on-disk bytes still match the lock.

    Such drift is a rendering change, not a local edit, so it is never
    offered to ``apm export-patch``.
    """
    modified = [finding for finding in findings if finding.kind == "modified"]
    keys = {finding.package for finding in modified if finding.package}
    if lockfile is None or project_root is None or not keys:
        return frozenset()

    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    recorded = DeploymentLedgerCodec.cleanup_snapshot(lockfile, keys).hashes
    resolved_root = project_root.resolve()
    return frozenset(
        finding.path
        for finding in modified
        if finding.path in recorded
        and _differs_from_recorded(
            project_root / finding.path, resolved_root, recorded[finding.path]
        )
        is False
    )


def count_exportable_findings(
    findings: Iterable[DriftFinding],
    lockfile: LockFile | None = None,
    *,
    unedited: Collection[str] = (),
) -> int:
    """Count ``modified`` drift findings that are upstream-package local edits."""
    return sum(
        1
        for finding in findings
        if finding.kind == "modified"
        and finding.path not in unedited
        and is_upstream_package(finding.package, lockfile)
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

    remote_keys = [key for key in dependency_keys if key and is_upstream_package(key, lockfile)]
    if not remote_keys:
        return 0

    snapshot = DeploymentLedgerCodec.cleanup_snapshot(lockfile, remote_keys)
    resolved_root = project_root.resolve()
    count = 0
    for rel_path, recorded in snapshot.hashes.items():
        if rel_path.endswith("/"):
            continue
        if _differs_from_recorded(project_root / rel_path, resolved_root, recorded):
            count += 1
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
