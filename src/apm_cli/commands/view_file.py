"""Rendering for ``apm view --file PATH`` (read-only reverse provenance)."""

from __future__ import annotations

import sys
from pathlib import Path

from ..core.command_logger import CommandLogger
from ..deps.file_provenance import (
    FileProvenance,
    FileProvenanceError,
    FileStatus,
    OwnerKind,
    OwnerProvenance,
    RecordProvenance,
    lookup_file_provenance,
)

EXIT_UNRECORDED = 1
EXIT_INVALID_STATE = 2

_NOT_RECORDED = "not recorded"

_STATUS_TEXT = {
    FileStatus.MATCHES: "matches the recorded deployed hash",
    FileStatus.MODIFIED: "modified since deployment (differs from the recorded hash)",
    FileStatus.MISSING: "missing",
    FileStatus.UNVERIFIABLE: "unverifiable",
}

_GUIDANCE = {
    OwnerKind.PROJECT_SELF: (
        "Owned by this project. Edit the project's own source (for example under "
        ".apm/), then rerun 'apm install'."
    ),
    OwnerKind.LOCAL_BUNDLE: (
        "Deployed from a local bundle, not replayable from apm.yml. Edit the "
        "bundle's source and reinstall it instead of this copy."
    ),
    OwnerKind.DIRECT_DEPENDENCY: (
        "Deployed copy from a dependency. Edit the dependency's source instead; "
        "'apm install' can overwrite edits made here."
    ),
    OwnerKind.TRANSITIVE_DEPENDENCY: (
        "Deployed copy from a dependency. Edit the dependency's source instead; "
        "'apm install' can overwrite edits made here."
    ),
}


def _owner_label(owner: OwnerProvenance) -> str:
    if owner.kind is OwnerKind.PROJECT_SELF:
        kind = "this project (project-self)"
    elif owner.kind is OwnerKind.LOCAL_BUNDLE:
        kind = "local bundle (imperative install)"
    elif owner.kind is OwnerKind.DIRECT_DEPENDENCY:
        kind = "direct dependency"
    elif owner.kind is OwnerKind.TRANSITIVE_DEPENDENCY:
        via = f" via {owner.resolved_by}" if owner.resolved_by else ""
        kind = f"transitive dependency{via}"
    else:
        kind = "unresolved (owner has no dependency record in the lockfile)"
    return kind


def _render_owner(owner: OwnerProvenance, logger: CommandLogger) -> None:
    role = "active writer" if owner.active else "surviving owner"
    logger.tree_item(f"    Owner: {owner.key} [{role}]")
    logger.tree_item(f"      Kind: {_owner_label(owner)}")
    if owner.kind not in (OwnerKind.DIRECT_DEPENDENCY, OwnerKind.TRANSITIVE_DEPENDENCY):
        return
    package = " ".join(part for part in (owner.name, owner.version) if part)
    logger.tree_item(f"      Package: {package or _NOT_RECORDED}")
    logger.tree_item(f"      Source: {owner.source or _NOT_RECORDED}")
    if owner.local_source:
        return
    logger.tree_item(f"      Ref: {owner.ref or _NOT_RECORDED}")
    logger.tree_item(f"      Commit: {owner.commit or _NOT_RECORDED}")


def _render_record(record: RecordProvenance, logger: CommandLogger) -> None:
    logger.tree_item(f"  Target: {record.target}")
    for owner in record.owners:
        _render_owner(owner, logger)
    status = _STATUS_TEXT[record.status]
    if record.status_detail:
        status = f"{status} ({record.status_detail})"
    logger.tree_item(f"  Current file: {status}")


def render_file_provenance(
    result: FileProvenance, logger: CommandLogger, *, user_scope: bool
) -> int:
    """Render *result* and return the process exit code."""
    scope = "user" if user_scope else "project"
    if not result.recorded:
        logger.error(
            f"No recorded provenance for '{result.relative_path}' in {result.lockfile_path}."
        )
        logger.error_detail(
            "  APM has no exact-path deployment record for this file. Its origin is "
            "unknown: do not assume it is project-owned or safe to edit."
        )
        return EXIT_UNRECORDED

    logger.progress(f"{result.relative_path} ({scope} scope, from {result.lockfile_path.name})")
    for record in result.records:
        _render_record(record, logger)

    if result.legacy_records:
        logger.warning(
            "Lockfile has no canonical deployment records; owners come from legacy "
            "deployed_files views. Rerun 'apm install' to refresh them."
        )
    if any(record.status is not FileStatus.MATCHES for record in result.records):
        logger.warning(
            "Owners above are recorded provenance, not verification of the current bytes."
        )
    if not result.has_resolved_owner:
        logger.error(
            "Recorded owner is not in the lockfile dependencies; provenance is incomplete. "
            "Rerun 'apm install' to rebuild deployment records."
        )
        return EXIT_UNRECORDED

    active = result.records[0].active_owner
    guidance = _GUIDANCE.get(active.kind) if active is not None else None
    if guidance:
        logger.info(guidance)
    return 0


def view_file(path: str, *, global_: bool, logger: CommandLogger) -> None:
    """Run ``apm view --file PATH`` and exit with its status code."""
    from ..core.scope import InstallScope, get_apm_dir, get_deploy_root

    scope = InstallScope.USER if global_ else InstallScope.PROJECT
    try:
        result = lookup_file_provenance(
            path,
            lock_dir=get_apm_dir(scope),
            deploy_root=get_deploy_root(scope),
            user_scope=global_,
            cwd=Path.cwd(),
        )
    except FileProvenanceError as exc:
        logger.error(str(exc))
        sys.exit(EXIT_INVALID_STATE)
    code = render_file_provenance(result, logger, user_scope=global_)
    if code:
        sys.exit(code)
