"""Shared cleanup helper for stale deployed files.

Used by the post-install cleanup blocks in :mod:`apm_cli.commands.install`
to remove files previously deployed for a still-present package that the
current install no longer produces (e.g. after a rename or removal inside
the package). Centralises the safety gates so both the local-package and
remote-package cleanup paths apply the same rules.

Safety gates, in order:

1. **Path validation** -- :meth:`BaseIntegrator.validate_deploy_path` rejects
   path traversal and any path not under a known integration prefix.
2. **Directory handling** -- APM-managed primitives are file-keyed
   (``SKILL.md`` for skills, individual ``.prompt.md`` / ``.instructions.md``
   files elsewhere). Directory entries that match a known skill directory
   pattern (``<prefix>/skills/<name>``) are deferred to a second pass so
   individual files are removed first; if the directory is then empty (or
   only contains APM-tracked files with matching hashes) it is safely
   removed. Non-skill directory entries are still rejected as untrusted.
3. **Provenance check** -- when the previous lockfile recorded a content
   hash for the file, the on-disk content must still match. If the user
   edited the file after APM deployed it the hash will differ and the
   deletion is skipped with a warning. Files without a recorded hash
   (legacy lockfiles) fall through and are deleted, preserving prior
   behaviour.

The helper records cleanup diagnostics via *diagnostics* (collect-then-
render) and returns a :class:`CleanupResult` summarizing deleted, failed,
and skipped paths. Callers remain responsible for any informational,
progress, or warning logging based on that result -- the helper itself
takes no logger.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .base_integrator import BaseIntegrator


@dataclass
class CleanupResult:
    """Outcome of a stale-file cleanup pass for a single package."""

    deleted: list[str] = field(default_factory=list)
    """Workspace-relative paths actually removed from disk."""

    failed: list[str] = field(default_factory=list)
    """Paths that raised during ``unlink``/``rmtree`` and should be
    retained in ``deployed_files`` for retry on the next install."""

    skipped_user_edit: list[str] = field(default_factory=list)
    """Paths skipped because the on-disk content no longer matches the
    hash APM recorded at deploy time -- treated as user-edited."""

    skipped_unmanaged: list[str] = field(default_factory=list)
    """Paths refused by the safety gates (validation failure, directory
    entry, etc.). Not retained in ``deployed_files``."""

    deleted_targets: list[Path] = field(default_factory=list)
    """Absolute paths of deleted entries -- input to
    :meth:`BaseIntegrator.cleanup_empty_parents`."""


def _is_skill_directory_entry(rel_path: str) -> bool:
    """Return True when *rel_path* matches a skill directory pattern.

    Skill directories are deployed under ``<prefix>/skills/<name>`` where
    ``<prefix>`` is a target root or deploy_root (e.g. ``.agents``,
    ``.github``, ``.claude``, ``.cursor``).  The path must have exactly
    one component after ``skills/`` (the skill name) to qualify -- deeper
    entries or ``skills/`` itself do not match.
    """
    parts = Path(rel_path).parts
    # Minimum: prefix, "skills", name -> 3 parts (e.g. ".agents/skills/my-skill")
    if len(parts) < 3:
        return False
    # The second-to-last component must be "skills" and the last is the
    # skill name.  We require exactly one component after skills/ so that
    # we only match the top-level skill directory, not subdirectories
    # within a skill bundle.
    try:
        skills_idx = parts.index("skills")
    except ValueError:
        return False
    # Exactly one component after "skills" (the skill name)
    return skills_idx == len(parts) - 2 and skills_idx >= 1


def _safe_remove_skill_directory(
    skill_dir: Path,
    rel_path: str,
    recorded_hashes: dict[str, str],
    all_stale: set[str],
    result: CleanupResult,
    diagnostics,
    dep_key: str,
    failed_path_retained: bool,
) -> None:
    """Attempt safe removal of an APM-deployed skill directory.

    Called in the deferred second pass after individual files have been
    deleted.  The directory is removed when:

    - It no longer exists (already cleaned or never created).
    - It is empty (all files already deleted in the first pass).
    - All remaining files are tracked in *recorded_hashes* with matching
      content hashes (APM-owned, unmodified).

    When the directory contains user-created or user-edited files, removal
    is skipped with a diagnostic listing the blocking entries.
    """
    if not skill_dir.exists():
        return

    if not skill_dir.is_dir() or skill_dir.is_symlink():
        return

    # Check if directory is empty -- safe to rmdir.
    try:
        remaining = list(skill_dir.iterdir())
    except OSError:
        result.failed.append(rel_path)
        return

    if not remaining:
        try:
            skill_dir.rmdir()
            result.deleted.append(rel_path)
            result.deleted_targets.append(skill_dir)
        except OSError as exc:
            result.failed.append(rel_path)
            _emit_dir_failure(diagnostics, rel_path, exc, dep_key, failed_path_retained)
        return

    # Non-empty: verify every remaining file is APM-tracked with
    # matching hashes.  Recurse into subdirectories (skills can
    # contain scripts/, assets/, references/ subdirectories).
    blocking: list[str] = []
    project_root = skill_dir
    # Walk up to reconstruct project_root from the rel_path
    rel_parts = Path(rel_path).parts
    project_root = skill_dir
    for _ in rel_parts:
        project_root = project_root.parent

    for child in skill_dir.rglob("*"):
        # Symlinks are treated as unmanaged -- APM does not deploy
        # symlinks, so any symlink is user-created content.
        if child.is_symlink():
            try:
                child_rel = child.relative_to(project_root).as_posix()
            except ValueError:
                child_rel = str(child.name)
            blocking.append(child_rel)
            continue
        if child.is_dir():
            continue
        try:
            child_rel = child.relative_to(project_root).as_posix()
        except ValueError:
            blocking.append(str(child.name))
            continue
        expected_hash = recorded_hashes.get(child_rel)
        if expected_hash is None:
            # File not tracked by APM -- could be user-created.
            # Check if it was in the stale set (already processed
            # and potentially deleted). If not in stale set at all,
            # it is definitely user content.
            if child_rel not in all_stale:
                blocking.append(child_rel)
                continue
            # Was in stale set but file still exists -- might have
            # been skipped (user-edit, failed unlink). Block removal.
            blocking.append(child_rel)
            continue
        # Verify hash matches.
        try:
            from ..utils.content_hash import compute_file_hash

            actual_hash = compute_file_hash(child)
        except Exception:
            blocking.append(child_rel)
            continue
        _strip = _strip_sha256_prefix
        if _strip(actual_hash) != _strip(expected_hash):
            blocking.append(child_rel)

    if blocking:
        # Show up to 5 blocking paths so the user knows which files
        # prevented cleanup (reviewer feedback on PR #1767).
        preview = blocking[:5]
        suffix = f" (and {len(blocking) - 5} more)" if len(blocking) > 5 else ""
        file_list = ", ".join(preview) + suffix
        diagnostics.warn(
            (
                f"Skipped removing skill directory {rel_path}: "
                f"{len(blocking)} file(s) not owned by APM or "
                f"modified since deployment: {file_list}. "
                "Remove manually if no longer needed."
            ),
            package=dep_key,
        )
        result.skipped_unmanaged.append(rel_path)
        return

    # All remaining files are APM-tracked with matching hashes -- safe
    # to remove the entire directory tree.
    try:
        shutil.rmtree(skill_dir)
        result.deleted.append(rel_path)
        result.deleted_targets.append(skill_dir)
    except OSError as exc:
        result.failed.append(rel_path)
        _emit_dir_failure(diagnostics, rel_path, exc, dep_key, failed_path_retained)


def _emit_dir_failure(diagnostics, rel_path, exc, dep_key, failed_path_retained):
    """Emit a diagnostic for a failed directory removal."""
    if failed_path_retained:
        diagnostics.warn(
            (
                f"Could not remove skill directory {rel_path}: {exc}. "
                "Path retained in lockfile; will retry on next "
                "'apm install'."
            ),
            package=dep_key,
        )
    else:
        diagnostics.warn(
            (
                f"Could not remove skill directory {rel_path}: {exc}. "
                "The owning package is no longer in apm.yml -- "
                "remove the directory manually."
            ),
            package=dep_key,
        )


def _strip_sha256_prefix(h: str) -> str:
    """Strip the ``sha256:`` prefix for hash comparison."""
    return h[len("sha256:") :] if h.startswith("sha256:") else h


def remove_stale_deployed_files(
    stale_paths: Iterable[str],
    project_root: Path,
    *,
    dep_key: str,
    targets,
    diagnostics,
    recorded_hashes: dict[str, str] | None = None,
    failed_path_retained: bool = True,
) -> CleanupResult:
    """Remove APM-deployed files that are no longer produced by *dep_key*.

    Args:
        stale_paths: Workspace-relative paths flagged as stale by
            :func:`apm_cli.drift.detect_stale_files` (intra-package
            renames/removals) or :func:`apm_cli.drift.detect_orphans`
            (whole package removed from the manifest).
        project_root: Project root the deletion is scoped within.
        dep_key: Unique key of the package these paths belong to (used
            for diagnostic attribution).
        targets: Resolved target profiles for this install (passed
            through to :meth:`BaseIntegrator.validate_deploy_path`).
        diagnostics: ``DiagnosticCollector`` -- recoverable warnings
            (user-edit skip, unlink failure, refused directory entry)
            are pushed here.
        recorded_hashes: Mapping from rel-path to ``"sha256:<hex>"`` as
            stored on the previous ``LockedDependency``. ``None`` (or
            empty) disables the per-file provenance check entirely --
            preserved for backward compat with pre-hash lockfiles.
        failed_path_retained: When ``True`` (default, intra-package
            stale cleanup) the failure diagnostic tells the user APM
            will retry on the next install -- the caller is expected
            to re-insert ``result.failed`` into the new
            ``deployed_files``. When ``False`` (orphan cleanup) the
            owning package is being removed from the lockfile so a
            failed path cannot be retained; the diagnostic instructs
            the user to remove the file manually instead.

    Returns:
        :class:`CleanupResult` describing what happened. The caller is
        responsible for any post-deletion bookkeeping (extending the
        new ``deployed_files`` list with ``failed`` so they are retried,
        invoking :meth:`BaseIntegrator.cleanup_empty_parents` on
        ``deleted_targets``, calling
        :meth:`InstallLogger.cleanup_skipped_user_edit` for each entry
        in ``skipped_user_edit`` so the inline yellow warning renders,
        and reporting ``deleted`` count to the user via
        :meth:`InstallLogger.stale_cleanup` /
        :meth:`InstallLogger.orphan_cleanup`).
    """
    result = CleanupResult()
    recorded_hashes = recorded_hashes or {}

    # Materialise stale_paths so we can iterate twice: once for the main
    # file-deletion loop and once as a lookup set for the deferred
    # directory pass.
    _stale_list = sorted(stale_paths)
    _stale_set: set[str] = set(_stale_list)

    # Skill directory entries are deferred to a second pass so individual
    # files are removed first. After files are deleted the directory is
    # either empty (safe rmdir) or contains only verified APM-tracked
    # files (safe rmtree).
    _deferred_dirs: list[tuple[str, Path]] = []

    # Lazy-resolve cowork root at most once per invocation (same
    # pattern as sync_remove_files in base_integrator.py -- PR #926 P4).
    _cowork_root_resolved: bool = False
    _cowork_root_cached: Path | None = None
    _cowork_orphans_skipped: int = 0
    _cowork_resolve_errors: int = 0

    for stale_path in _stale_list:
        # -- Cowork:// paths ---------------------------------------
        # Handled BEFORE validate_deploy_path because that method
        # hard-rejects cowork:// when the OneDrive root is unavailable
        # (returning False ⇒ skipped_unmanaged).  For cleanup we want
        # the gentler "retain in *failed* for retry" behaviour, so we
        # do equivalent security checks (no traversal, known prefix,
        # containment via from_lockfile_path) ourselves.
        from .copilot_cowork_paths import COWORK_URI_SCHEME

        if stale_path.startswith(COWORK_URI_SCHEME):
            # Basic security: reject path-traversal components.
            if ".." in stale_path:
                result.skipped_unmanaged.append(stale_path)
                continue
            # Verify the path starts with a known integration prefix.
            from .targets import get_integration_prefixes

            if not stale_path.startswith(get_integration_prefixes(targets=targets)):
                result.skipped_unmanaged.append(stale_path)
                continue
            # Resolve the cowork:// URI to a real filesystem path.
            try:
                if not _cowork_root_resolved:
                    from .copilot_cowork_paths import (
                        resolve_copilot_cowork_skills_dir,
                    )

                    _cowork_root_cached = resolve_copilot_cowork_skills_dir()
                    _cowork_root_resolved = True
                if _cowork_root_cached is None:
                    # OneDrive unavailable -- retain lockfile entry so a
                    # later install with a configured root can clean up.
                    _cowork_orphans_skipped += 1
                    result.failed.append(stale_path)
                    continue
                from .copilot_cowork_paths import from_lockfile_path

                stale_target = from_lockfile_path(stale_path, _cowork_root_cached)
            except Exception:
                # Containment violation or malformed path -- retain in
                # lockfile for manual inspection.
                _cowork_resolve_errors += 1
                result.failed.append(stale_path)
                continue
        else:
            # ── Non-cowork paths ─────────────────────────────────────
            # Gate 1: path validation (traversal, allowed prefix, in-tree).
            if not BaseIntegrator.validate_deploy_path(stale_path, project_root, targets=targets):
                result.skipped_unmanaged.append(stale_path)
                continue
            stale_target = project_root / stale_path

        if not stale_target.exists():
            # File already gone -- treat as cleaned (no-op success).
            continue

        # Gate 2: directory handling. APM-managed primitives are
        # file-keyed; a directory entry under an integration prefix is
        # normally treated as untrusted. However, skill directories are
        # legitimately tracked by APM (services.py records the skill dir
        # entry alongside its contained files). Skill directories are
        # deferred to a second pass so individual files are deleted first;
        # non-skill directory entries are still rejected immediately.
        if stale_target.is_dir() and not stale_target.is_symlink():
            if _is_skill_directory_entry(stale_path):
                _deferred_dirs.append((stale_path, stale_target))
            else:
                result.skipped_unmanaged.append(stale_path)
                diagnostics.warn(
                    (
                        f"Refused to remove directory entry {stale_path}: APM "
                        "only deletes individual files. If this entry was added "
                        "by a malicious or corrupt lockfile, remove it manually "
                        "from apm.lock.yaml."
                    ),
                    package=dep_key,
                )
            continue

        # Gate 3: provenance check. If APM recorded a content hash for
        # this file at deploy time and it no longer matches, the user
        # has edited the file -- skip deletion and warn so they can
        # decide what to do. Fails CLOSED on hash-read errors: if APM
        # cannot prove the file is unmodified (PermissionError, race,
        # etc.) we keep it rather than risk destroying user work.
        expected_hash = recorded_hashes.get(stale_path)
        if expected_hash:
            try:
                from ..utils.content_hash import compute_file_hash

                actual_hash = compute_file_hash(stale_target)
            except Exception as _hash_exc:
                result.skipped_user_edit.append(stale_path)
                diagnostics.warn(
                    (
                        f"Skipped removing {stale_path}: could not verify "
                        f"file content ({_hash_exc.__class__.__name__}). "
                        "Inspect the file and delete it manually if no "
                        "longer needed."
                    ),
                    package=dep_key,
                )
                continue

            # Defensive normalization: ``recorded_hashes`` may carry either
            # the canonical ``sha256:<hex>`` (regular install pipeline) or
            # bare ``<hex>`` (legacy local-bundle installs prior to the
            # 0.12.0 fix).  ``compute_file_hash`` always returns the
            # prefixed form, so strip the prefix from both sides before
            # comparing to avoid false "user-edited" classifications.
            if _strip_sha256_prefix(actual_hash) != _strip_sha256_prefix(expected_hash):
                result.skipped_user_edit.append(stale_path)
                diagnostics.warn(
                    (
                        f"Skipped removing {stale_path}: file has been "
                        "edited since APM deployed it. Delete it manually "
                        "if you no longer need it, or ignore this warning "
                        "to keep your changes."
                    ),
                    package=dep_key,
                )
                continue

        # All gates passed -- safe to delete.
        try:
            stale_target.unlink()
            result.deleted.append(stale_path)
            result.deleted_targets.append(stale_target)
        except Exception as exc:
            result.failed.append(stale_path)
            if failed_path_retained:
                diagnostics.warn(
                    (
                        f"Could not remove stale file {stale_path}: {exc}. "
                        "Path retained in lockfile; will retry on next "
                        "'apm install'."
                    ),
                    package=dep_key,
                )
            else:
                diagnostics.warn(
                    (
                        f"Could not remove orphaned file {stale_path}: {exc}. "
                        "The owning package is no longer in apm.yml -- "
                        "delete the file manually."
                    ),
                    package=dep_key,
                )

    # -- Second pass: deferred skill directories -------------------
    # Individual files have been deleted above. Now attempt safe removal
    # of skill directories that APM itself created.
    for _dir_path, _dir_target in _deferred_dirs:
        _safe_remove_skill_directory(
            _dir_target,
            _dir_path,
            recorded_hashes,
            _stale_set,
            result,
            diagnostics,
            dep_key,
            failed_path_retained,
        )

    # One-time warnings for cowork edge cases (mirrors sync_remove_files).
    if _cowork_orphans_skipped > 0:
        diagnostics.warn(
            (
                f"Cowork: skipping {_cowork_orphans_skipped} stale lockfile "
                f"{'entry' if _cowork_orphans_skipped == 1 else 'entries'}"
                " -- OneDrive path not detected.\n"
                "Run: apm config set copilot-cowork-skills-dir <path>  "
                "(or set APM_COPILOT_COWORK_SKILLS_DIR)\n"
                "to clean up these entries on the next install/uninstall."
            ),
            package=dep_key,
        )
    if _cowork_resolve_errors > 0:
        diagnostics.warn(
            (
                f"Cowork: {_cowork_resolve_errors} lockfile "
                f"{'entry' if _cowork_resolve_errors == 1 else 'entries'}"
                " failed path resolution (containment violation or "
                "malformed path). Paths retained for manual inspection."
            ),
            package=dep_key,
        )

    return result
