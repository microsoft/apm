"""Shared cleanup helper for stale deployed files.

Used by the post-install cleanup blocks in :mod:`apm_cli.commands.install`
to remove files previously deployed for a still-present package that the
current install no longer produces (e.g. after a rename or removal inside
the package). Centralises the safety gates so both the local-package and
remote-package cleanup paths apply the same rules.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .base_integrator import BaseIntegrator


@dataclass
class CleanupResult:
    """Outcome of a stale-file cleanup pass for a single package."""

    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped_user_edit: list[str] = field(default_factory=list)
    skipped_unmanaged: list[str] = field(default_factory=list)
    deleted_targets: list[Path] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CleanupOpts:
    """Optional arguments for stale-file cleanup."""

    dep_key: str
    targets: object
    diagnostics: object
    recorded_hashes: dict[str, str] | None = None
    failed_path_retained: bool = True


@dataclass(slots=True)
class _CoworkCleanupState:
    """Cached cowork resolution state shared across one cleanup pass."""

    root_resolved: bool = False
    root_cached: Path | None = None
    skipped_count: int = 0
    resolve_errors: int = 0


def _strip_sha256(hash_value: str) -> str:
    """Normalize bare and prefixed SHA-256 hashes."""
    return hash_value[len("sha256:") :] if hash_value.startswith("sha256:") else hash_value


def _warn_failed_delete(
    diagnostics, dep_key: str, stale_path: str, exc: Exception, *, retained: bool
) -> None:
    """Record a failed-delete warning."""
    if retained:
        message = (
            f"Could not remove stale file {stale_path}: {exc}. "
            "Path retained in lockfile; will retry on next 'apm install'."
        )
    else:
        message = (
            f"Could not remove orphaned file {stale_path}: {exc}. "
            "The owning package is no longer in apm.yml -- delete the file manually."
        )
    diagnostics.warn(message, package=dep_key)


def _record_skipped_directory(
    result: CleanupResult, diagnostics, dep_key: str, stale_path: str
) -> None:
    """Record refusal to delete a directory entry."""
    result.skipped_unmanaged.append(stale_path)
    diagnostics.warn(
        (
            f"Refused to remove directory entry {stale_path}: APM only deletes individual files. "
            "If this entry was added by a malicious or corrupt lockfile, remove it manually from "
            "apm.lock.yaml."
        ),
        package=dep_key,
    )


def _verify_recorded_hash(
    stale_path: str,
    stale_target: Path,
    *,
    opts: CleanupOpts,
    recorded_hashes: dict[str, str],
    result: CleanupResult,
) -> bool:
    """Return True when the stale target still matches the recorded hash."""
    expected_hash = recorded_hashes.get(stale_path)
    if not expected_hash:
        return True
    try:
        from ..utils.content_hash import compute_file_hash

        actual_hash = compute_file_hash(stale_target)
    except Exception as hash_exc:
        result.skipped_user_edit.append(stale_path)
        opts.diagnostics.warn(
            (
                f"Skipped removing {stale_path}: could not verify file content "
                f"({hash_exc.__class__.__name__}). Inspect the file and delete it manually if no "
                "longer needed."
            ),
            package=opts.dep_key,
        )
        return False

    if _strip_sha256(actual_hash) == _strip_sha256(expected_hash):
        return True

    result.skipped_user_edit.append(stale_path)
    opts.diagnostics.warn(
        (
            f"Skipped removing {stale_path}: file has been edited since APM deployed it. "
            "Delete it manually if you no longer need it, or ignore this warning to keep your "
            "changes."
        ),
        package=opts.dep_key,
    )
    return False


def _resolve_cowork_target(
    stale_path: str,
    *,
    targets,
    result: CleanupResult,
    cowork_state: _CoworkCleanupState,
) -> Path | None:
    """Resolve a cowork lockfile path to a concrete filesystem target."""
    from .copilot_cowork_paths import from_lockfile_path, resolve_copilot_cowork_skills_dir
    from .targets import get_integration_prefixes

    if ".." in stale_path:
        result.skipped_unmanaged.append(stale_path)
        return None
    if not stale_path.startswith(get_integration_prefixes(targets=targets)):
        result.skipped_unmanaged.append(stale_path)
        return None
    try:
        if not cowork_state.root_resolved:
            cowork_state.root_cached = resolve_copilot_cowork_skills_dir()
            cowork_state.root_resolved = True
        if cowork_state.root_cached is None:
            cowork_state.skipped_count += 1
            result.failed.append(stale_path)
            return None
        return from_lockfile_path(stale_path, cowork_state.root_cached)
    except Exception:
        cowork_state.resolve_errors += 1
        result.failed.append(stale_path)
        return None


def _resolve_stale_target(
    stale_path: str,
    *,
    project_root: Path,
    targets,
    result: CleanupResult,
    cowork_state: _CoworkCleanupState,
) -> Path | None:
    """Resolve a stale path after applying path-safety gates."""
    from .copilot_cowork_paths import COWORK_URI_SCHEME

    if stale_path.startswith(COWORK_URI_SCHEME):
        return _resolve_cowork_target(
            stale_path,
            targets=targets,
            result=result,
            cowork_state=cowork_state,
        )
    if not BaseIntegrator.validate_deploy_path(stale_path, project_root, targets=targets):
        result.skipped_unmanaged.append(stale_path)
        return None
    return project_root / stale_path


def _warn_cowork_cleanup_issues(
    diagnostics, dep_key: str, cowork_state: _CoworkCleanupState
) -> None:
    """Emit one-time cowork cleanup warnings."""
    if cowork_state.skipped_count > 0:
        diagnostics.warn(
            (
                f"Cowork: skipping {cowork_state.skipped_count} stale lockfile "
                f"{'entry' if cowork_state.skipped_count == 1 else 'entries'}"
                " -- OneDrive path not detected.\n"
                "Run: apm config set copilot-cowork-skills-dir <path>  "
                "(or set APM_COPILOT_COWORK_SKILLS_DIR)\n"
                "to clean up these entries on the next install/uninstall."
            ),
            package=dep_key,
        )
    if cowork_state.resolve_errors > 0:
        diagnostics.warn(
            (
                f"Cowork: {cowork_state.resolve_errors} lockfile "
                f"{'entry' if cowork_state.resolve_errors == 1 else 'entries'}"
                " failed path resolution (containment violation or malformed path). "
                "Paths retained for manual inspection."
            ),
            package=dep_key,
        )


def remove_stale_deployed_files(
    stale_paths: Iterable[str],
    project_root: Path,
    opts: CleanupOpts | None = None,
    **legacy_kwargs,
) -> CleanupResult:
    """Remove APM-deployed files that are no longer produced by a package."""
    if opts is None:
        dep_key = legacy_kwargs.get("dep_key")
        if dep_key is None:
            msg = "dep_key is required when opts is not provided"
            raise ValueError(msg)
        opts = CleanupOpts(
            dep_key=dep_key,
            targets=legacy_kwargs.get("targets"),
            diagnostics=legacy_kwargs.get("diagnostics"),
            recorded_hashes=legacy_kwargs.get("recorded_hashes"),
            failed_path_retained=legacy_kwargs.get("failed_path_retained", True),
        )
    result = CleanupResult()
    recorded_hashes = opts.recorded_hashes or {}
    cowork_state = _CoworkCleanupState()

    for stale_path in sorted(stale_paths):
        stale_target = _resolve_stale_target(
            stale_path,
            project_root=project_root,
            targets=opts.targets,
            result=result,
            cowork_state=cowork_state,
        )
        if stale_target is None or not stale_target.exists():
            continue
        if stale_target.is_dir() and not stale_target.is_symlink():
            _record_skipped_directory(result, opts.diagnostics, opts.dep_key, stale_path)
            continue
        if not _verify_recorded_hash(
            stale_path,
            stale_target,
            opts=opts,
            recorded_hashes=recorded_hashes,
            result=result,
        ):
            continue
        try:
            stale_target.unlink()
            result.deleted.append(stale_path)
            result.deleted_targets.append(stale_target)
        except Exception as exc:
            result.failed.append(stale_path)
            _warn_failed_delete(
                opts.diagnostics,
                opts.dep_key,
                stale_path,
                exc,
                retained=opts.failed_path_retained,
            )

    _warn_cowork_cleanup_issues(opts.diagnostics, opts.dep_key, cowork_state)
    return result
