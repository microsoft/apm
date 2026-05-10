"""Lockfile-aware auto-migration of legacy per-client skill paths.

When a skill's deploy target changes from a per-client directory (e.g.
``.github/skills/``, ``.cursor/skills/``) to the converged ``.agents/skills/``
directory, the lockfile still records the old paths.  This module detects those
stale legacy entries, checks for collisions with the new converged location,
and deletes the old files after the integrate phase has written the new ones.

**Write-order safety** (crash-recovery friendly):

1. Integrate phase writes new files to ``.agents/skills/…``.
2. This module deletes old per-client files.
3. Lockfile phase persists the updated ``deployed_files``.

If a crash occurs between steps 1 and 2, the next install re-detects the
legacy paths (still in the lockfile) and retries deletion.  If a crash occurs
between steps 2 and 3, the lockfile still has the old paths but the files are
gone -- the next install sees them missing and the cleanup is a no-op.

**Security model**:

- Plan creation (:func:`detect_legacy_skill_deployments`) rejects lockfile
  entries containing path-traversal segments (``..``) via
  :func:`~apm_cli.utils.path_security.validate_path_segments`.
- File deletion (:func:`execute_migration`) guards ``unlink()`` with
  :func:`~apm_cli.utils.path_security.ensure_path_within` so that even a
  poisoned plan entry cannot delete outside the project root.
- Directory deletion is routed through :func:`~apm_cli.utils.path_security.safe_rmtree`
  which also calls ``ensure_path_within``.
- Parent-directory cleanup (:func:`_cleanup_empty_parents`) is
  containment-checked at every iteration step.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    safe_rmtree,
    validate_path_segments,
)

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile

_log = logging.getLogger(__name__)

# Legacy per-client skill prefixes that have been converged into .agents/skills/.
# .claude/skills/ is excluded (Claude is not in the convergence set).
# .codex/skills/ was never a legacy path (Codex always used .agents/).
_LEGACY_SKILL_PATTERN = re.compile(r"^\.(github|cursor|opencode|gemini)/skills/([^/]+)/.+$")

# ------------------------------------------------------------------
# Shared message templates (single source of truth — H6)
# ------------------------------------------------------------------
MIGRATION_SUMMARY_TEMPLATE = (
    "Migrated {count} skill file(s) from legacy per-client paths to .agents/skills/"
)
COLLISION_HEADER_TEMPLATE = (
    "Skill path migration skipped: {count} file(s) at .agents/skills/ "
    "already exist with different content."
)
COLLISION_DETAIL_TEMPLATE = "  {dst_path} conflicts with {src_path} (from {dep_name})"
COLLISION_HINT = (
    "Remove the conflicting file(s) and re-run `apm install`, "
    "or pass --legacy-skill-paths to keep per-client paths."
)


@dataclass(frozen=True)
class MigrationPlan:
    """A single file that needs to be migrated (old path deleted)."""

    src_path: str
    """Workspace-relative POSIX path of the legacy deployed file."""

    dst_path: str
    """Expected workspace-relative POSIX path under ``.agents/skills/``."""

    dep_name: str
    """Dependency key from the lockfile that owns this file."""


@dataclass
class MigrationResult:
    """Outcome of executing a migration plan."""

    deleted: list[str] = field(default_factory=list)
    """Legacy paths successfully removed from disk."""

    failed: list[str] = field(default_factory=list)
    """Legacy paths that raised during deletion (retained for retry)."""

    skipped_no_file: list[str] = field(default_factory=list)
    """Legacy paths already absent from disk (no-op)."""

    updated_deps: set[str] = field(default_factory=set)
    """Dependency keys whose ``deployed_files`` were mutated."""


def detect_legacy_skill_deployments(
    lockfile: LockFile,
    project_root: Path,
) -> list[MigrationPlan]:
    """Scan the lockfile for deployed_files that match legacy per-client skill paths.

    Returns a list of :class:`MigrationPlan` entries, one per file that should
    be migrated.  The caller decides whether to act on the plan.

    The *project_root* parameter is accepted for forward-compatibility (e.g.
    on-disk collision detection) but is unused in the initial implementation.
    """
    plans: list[MigrationPlan] = []

    for dep_key, dep in lockfile.dependencies.items():
        for rel_path in dep.deployed_files:
            m = _LEGACY_SKILL_PATTERN.match(rel_path)
            if not m:
                continue

            # B1: reject path-traversal in lockfile-recorded paths.
            try:
                validate_path_segments(rel_path, context="legacy skill path")
            except PathTraversalError:
                _log.warning("Skipping legacy path with traversal segments: %s", rel_path)
                continue

            # Build the converged destination: .agents/skills/<name>/<rest>
            # e.g. ".github/skills/my-skill/SKILL.md" -> ".agents/skills/my-skill/SKILL.md"
            client_prefix = f".{m.group(1)}/skills/"
            suffix = rel_path[len(client_prefix) :]

            # B1: also validate the computed destination suffix.
            try:
                validate_path_segments(suffix, context="migrated skill suffix")
            except PathTraversalError:
                _log.warning("Skipping legacy path whose suffix has traversal: %s", rel_path)
                continue

            dst = f".agents/skills/{suffix}"
            plans.append(MigrationPlan(src_path=rel_path, dst_path=dst, dep_name=dep_key))

    return plans


def check_collisions(
    plans: list[MigrationPlan],
    project_root: Path,
) -> list[str]:
    """Check whether any migration destination already exists AND differs.

    Returns a list of human-readable collision descriptions.  An empty list
    means no collisions -- safe to proceed.

    If the destination file already exists with identical content, that is NOT
    a collision (the integrate phase already wrote it).
    """
    collisions: list[str] = []
    for plan in plans:
        src_abs = project_root / plan.src_path
        dst_abs = project_root / plan.dst_path
        if not dst_abs.exists():
            # Destination doesn't exist yet -- the integrate phase will create
            # it, or the file has already been cleaned up.  Not a collision.
            continue
        if not src_abs.exists():
            # Source is already gone -- nothing to collide with.
            continue
        # Both exist -- compare content.
        try:
            if src_abs.read_bytes() == dst_abs.read_bytes():
                continue  # Identical content: not a real collision.
        except OSError:
            pass  # Fall through to report collision on I/O error.
        collisions.append(f"{plan.src_path} -> {plan.dst_path} (dep: {plan.dep_name})")
    return collisions


def execute_migration(
    plans: list[MigrationPlan],
    lockfile: LockFile,
    project_root: Path,
) -> MigrationResult:
    """Delete legacy files and update lockfile ``deployed_files`` in place.

    For each plan entry:

    1. Delete the legacy file from disk via :func:`safe_rmtree`.
    2. Remove the old path from ``deployed_files`` on the owning dependency.
    3. Add the new ``.agents/skills/`` path if not already present.
    4. Mirror changes in ``local_deployed_files`` / ``local_deployed_file_hashes``
       when the path originates from the synthesized self-entry (local bundles).

    The lockfile object is mutated in place; the caller is responsible for
    persisting it to disk afterwards.

    Returns a :class:`MigrationResult` summarizing what happened.
    """
    result = MigrationResult()

    for plan in plans:
        abs_path = project_root / plan.src_path

        if not abs_path.exists():
            result.skipped_no_file.append(plan.src_path)
            # Still update lockfile even if file is gone -- the path entry
            # is stale and should point to the new location.
            _update_lockfile_entry(lockfile, plan, result)
            continue

        try:
            # B1: containment guard before any disk mutation.
            ensure_path_within(abs_path, project_root)
            if abs_path.is_file():
                abs_path.unlink()
            else:
                safe_rmtree(abs_path, project_root)
            result.deleted.append(plan.src_path)
            _cleanup_empty_parents(abs_path, project_root)
        except (OSError, PathTraversalError):
            result.failed.append(plan.src_path)
            continue  # Don't update lockfile for failed deletions.

        _update_lockfile_entry(lockfile, plan, result)

    return result


def _update_lockfile_entry(
    lockfile: LockFile,
    plan: MigrationPlan,
    result: MigrationResult,
) -> None:
    """Swap src_path -> dst_path in the dependency's deployed_files.

    Also mirrors the change into ``lockfile.local_deployed_files`` /
    ``local_deployed_file_hashes`` when the path appears there (local
    bundles use the flat fields as source of truth for serialization).
    """
    dep = lockfile.dependencies.get(plan.dep_name)
    if dep is None:
        return

    files = dep.deployed_files
    if plan.src_path in files:
        files.remove(plan.src_path)
    if plan.dst_path not in files:
        files.append(plan.dst_path)

    # Mirror the swap in deployed_file_hashes if present.
    hashes = dep.deployed_file_hashes
    if plan.src_path in hashes:
        hash_val = hashes.pop(plan.src_path)
        if plan.dst_path not in hashes:
            hashes[plan.dst_path] = hash_val

    # Also update the flat local_deployed_files / local_deployed_file_hashes
    # that the lockfile serializer uses as source of truth.
    if plan.src_path in lockfile.local_deployed_files:
        lockfile.local_deployed_files.remove(plan.src_path)
        if plan.dst_path not in lockfile.local_deployed_files:
            lockfile.local_deployed_files.append(plan.dst_path)
    if plan.src_path in lockfile.local_deployed_file_hashes:
        h = lockfile.local_deployed_file_hashes.pop(plan.src_path)
        if plan.dst_path not in lockfile.local_deployed_file_hashes:
            lockfile.local_deployed_file_hashes[plan.dst_path] = h

    result.updated_deps.add(plan.dep_name)


def _cleanup_empty_parents(deleted_path: Path, base_dir: Path) -> None:
    """Remove empty parent directories up to but not including *base_dir*."""
    parent = deleted_path.parent
    base_resolved = base_dir.resolve()
    while parent.resolve() != base_resolved:
        try:
            # H8: containment guard before rmdir.
            ensure_path_within(parent, base_dir)
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
            else:
                break
        except (OSError, PathTraversalError):
            break
        parent = parent.parent
