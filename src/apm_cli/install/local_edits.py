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
    from apm_cli.core.remediation import Remediation
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext
    from apm_cli.install.drift import DriftFinding

_SELF_KEY = "."


def install_keeps_edit(path: str) -> bool:
    """Whether a plain ``apm install`` keeps a local edit to managed *path* (RA-009).

    Every kind is kept: the skill integrator refuses to overwrite a modified
    skill file, and a file-primitive integrator replaces an existing managed
    file only with deployment evidence, which an edited file never has; it
    keeps the file, names it and exits 1 (#4360).
    """
    del path
    return True


def local_edit_hint(count: int, *, overwritten: int | None = None) -> str:
    """Return the audit hint for *count* exportable edits.

    Only *overwritten* of them (default: all) are lost to the next plain
    ``apm install``; edited skills are kept (RA-009) but can still be
    exported upstream.
    """
    from apm_cli.core.remediation import LOCAL_EDITS_EXPORT

    overwritten = count if overwritten is None else overwritten
    if overwritten == count:
        return (
            f"{count} local edit(s) to managed files that the next 'apm install' overwrites; "
            f"first {LOCAL_EDITS_EXPORT.clause()}"
        )
    if overwritten:
        return (
            f"{count} local edit(s) to managed files ({overwritten} of them the next "
            f"'apm install' overwrites); first {LOCAL_EDITS_EXPORT.clause()}"
        )
    return f"{count} local edit(s) to managed files; {LOCAL_EDITS_EXPORT.clause()}"


def overwrite_notice(paths: Collection[str]) -> str:
    """Return the warning for edited files this install is about to overwrite."""
    shown = ", ".join(sorted(paths)[:5])
    more = f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""
    from apm_cli.core.remediation import LOCAL_EDITS_EXPORT

    return (
        f"This install overwrites {len(paths)} locally edited managed file(s): {shown}{more}. "
        f"To keep such edits, first {LOCAL_EDITS_EXPORT.clause()}"
    )


def edited_file_remediations(
    *, bundle: bool = False, user_scope: bool = False, exportable: bool = True
) -> tuple[Remediation, ...]:
    """Return the interchangeable remediations for edited managed files.

    Never destructive (RA-044): the same choice as the Kept line, move the
    file aside and reinstall to restore it, or export the edit with
    ``apm export-patch`` (project scope only: it reads the project lock; and
    only when an edit is *exportable*, i.e. in an upstream package).
    Files a local bundle deployed are only restored by reinstalling that
    bundle (#3994). Render the tuple with :func:`remediation.either`.
    """
    from apm_cli.core import remediation

    if bundle:
        return (remediation.RESTORE_BUNDLE_EDITED.bind(subject="the edited bundle files"),)
    restore = remediation.RESTORE_EDITED.bind(subject="the edited files")
    if user_scope:
        return (restore.with_command("apm install -g"),)
    return (restore, remediation.LOCAL_EDITS_EXPORT) if exportable else (restore,)


def rendering_change_hint(count: int) -> str:
    """Return the hint for drift whose on-disk bytes still match apm.lock.yaml."""
    from apm_cli.core.remediation import RENDERING_CHANGED

    return (
        f"{count} file(s) still match apm.lock.yaml but this APM version renders them "
        f"differently; {RENDERING_CHANGED.clause()}"
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
    """Count ``modified`` upstream-package local edits ``apm export-patch`` exports."""
    return sum(1 for _ in exportable_edit_paths(findings, lockfile, unedited=unedited))


def exportable_edit_paths(
    findings: Iterable[DriftFinding],
    lockfile: LockFile | None = None,
    *,
    unedited: Collection[str] = (),
) -> list[str]:
    """Return the ``modified`` upstream-package local edits ``apm export-patch`` exports."""
    return [
        finding.path
        for finding in findings
        if finding.kind == "modified"
        and not finding.owned_entries_only
        and finding.path not in unedited
        and is_upstream_package(finding.package, lockfile)
    ]


def local_edit_paths(
    project_root: Path,
    lockfile: LockFile | None,
    dependency_keys: Collection[str],
) -> list[str]:
    """Return managed files of upstream *dependency_keys* edited since deployment.

    Read-only. A file counts when it still exists in the project and its
    content hash differs from the hash recorded at deployment. Files
    without a recorded hash, symlinks, and paths escaping the project are
    ignored.
    """
    if lockfile is None or not dependency_keys:
        return []

    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    remote_keys = [key for key in dependency_keys if key and is_upstream_package(key, lockfile)]
    if not remote_keys:
        return []

    snapshot = DeploymentLedgerCodec.cleanup_snapshot(lockfile, remote_keys)
    resolved_root = project_root.resolve()
    return sorted(
        rel_path
        for rel_path, recorded in snapshot.hashes.items()
        if not rel_path.endswith("/")
        and _differs_from_recorded(project_root / rel_path, resolved_root, recorded)
    )


def count_local_edits(
    project_root: Path,
    lockfile: LockFile | None,
    dependency_keys: Collection[str],
) -> int:
    """Count managed files of upstream *dependency_keys* edited since deployment."""
    return len(local_edit_paths(project_root, lockfile, dependency_keys))


def hint_local_edits_before_install(ctx: InstallContext, lockfile: LockFile | None) -> None:
    """Warn, naming the files, before a project install overwrites local edits.

    Losing an edit is never info-level (RA-009). Edited skills are kept by
    the install and reported by the skill integrator, so they are not
    listed; when only kept edits exist, one info line offers the export. Skipped for dry runs, lockfile-only resolution and user-scope
    installs, which do not overwrite project files. Never blocks the install.
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
    edited = local_edit_paths(ctx.project_root, lockfile, keys)
    overwritten = [path for path in edited if not install_keeps_edit(path)]
    if overwritten:
        ctx.logger.warning(overwrite_notice(overwritten))
    elif edited:
        ctx.logger.info(local_edit_hint(len(edited), overwritten=0))


def unedited_apm_output(
    deploy_root: Path,
    user_scope: bool,
    lockfile: LockFile,
    targets: Collection,
    drifted: Iterable,
) -> frozenset[str]:
    """Return drifted paths whose bytes are exactly what this installation last wrote.

    A trusted local receipt proves such a file is stale APM output (for
    example after a branch switch), not a local edit, so a frozen install
    may replace it. Without a trusted receipt every mismatch counts as an
    edit.
    """
    from apm_cli.core.deployment_ledger import trusted_receipt_baselines
    from apm_cli.core.scope import InstallScope

    actual = {deploy_root / drift.path: (drift.path, drift.actual) for drift in drifted}
    baselines = trusted_receipt_baselines(
        deploy_root,
        InstallScope.USER if user_scope else InstallScope.PROJECT,
        lockfile,
        list(actual),
        list(targets),
    )
    return frozenset(
        relative for path, (relative, observed) in actual.items() if baselines.get(path) == observed
    )
