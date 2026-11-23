"""Shared cold-cache scratch replay owner for ``apm audit --ci``.

This module owns the one-shot orchestration that turns a checkout with a
lockfile but no live ``apm_modules/`` tree into a prepared, lock-pinned scratch
replay, and decides when a workspace needs that materialization at all
(``resolve_ci_audit_materialization``). ``commands/audit.py`` asks it once, then
``skill-subset-consistency``, ``config-consistency`` and ``drift`` consume
the same materialized state.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.core.remediation import (
    LOCKFILE_MISSING,
    REPLACEMENT_SOURCES_UNRECORDED,
    USER_PACKAGE_COPIES_STALE,
    USER_PACKAGE_MATERIALIZATION_MISSING,
)
from apm_cli.core.scope import manifest_file
from apm_cli.deps.lockfile import (
    LockFile,
    LockfileFormatError,
    UnsupportedLockfileVersionError,
    get_lockfile_path,
)
from apm_cli.install.drift import (
    CheckLogger,
    ReplayConfig,
    _make_scratch_root,
    run_replay,
)
from apm_cli.install.plan import lockfile_satisfies_manifest, lockfile_satisfies_replacements
from apm_cli.install.source_helpers import (
    locked_content_mismatch,
    locked_content_mismatch_line,
    observed_package_hash,
)
from apm_cli.integration.targets import TargetProfile, resolve_audit_targets
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.replacement import project_manifest_dependencies


@dataclass(frozen=True)
class PreparedCiAuditReplay:
    """Scratch replay prepared once for ``apm audit --ci`` consumers."""

    scratch_root: Path
    modules_root: Path
    lockfile_path: Path
    tracked_files: frozenset[str] | None
    targets: tuple[TargetProfile, ...]


class CiAuditReplayError(RuntimeError):
    """Raised when ``apm audit --ci`` cannot materialize its scratch replay."""


@dataclass(frozen=True)
class CiAuditMaterialization:
    """Where ``apm audit --ci`` consumers find installed package content.

    Both fields ``None`` means the consumers read the live ``apm_modules/``
    tree, or that nothing in the lockfile materializes there.
    """

    prepared_replay: PreparedCiAuditReplay | None = None
    error: str | None = None


def _git_tracked_files(project_root: Path) -> frozenset[str] | None:
    """Return tracked repository paths, or ``None`` outside a git worktree."""
    from apm_cli.utils.git_env import get_git_executable

    try:
        completed = subprocess.run(
            (get_git_executable(), "-C", str(project_root), "ls-files", "-z", "--full-name"),
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    try:
        return frozenset(path for path in completed.stdout.decode("utf-8").split("\0") if path)
    except UnicodeDecodeError:
        return None


def prepare_ci_audit_replay(
    project_root: Path,
    *,
    verbose: bool = False,
    policy_provider: Callable[[], Any] | None = None,
    policy_fetch: Callable[[], Any] | None = None,
) -> PreparedCiAuditReplay:
    """Prepare one lock-pinned scratch replay for ``apm audit --ci`` consumers."""
    project_root = project_root.resolve()
    from apm_cli.core.scope import get_workspace_deploy_root

    user_scope = get_workspace_deploy_root(project_root) != project_root
    lockfile_path = get_lockfile_path(project_root)
    if not lockfile_path.exists():
        raise CiAuditReplayError(
            f"lockfile not found at {lockfile_path}; {LOCKFILE_MISSING.clause()}"
        )

    from apm_cli.models.manifest_io import InvalidManifestError

    try:
        manifest = APMPackage.from_apm_yml(manifest_file(project_root))
    except InvalidManifestError as exc:
        # Reported as a failed check, so JSON/SARIF output still renders.
        raise CiAuditReplayError(str(exc)) from exc
    try:
        lockfile = LockFile.read(lockfile_path)
    except UnsupportedLockfileVersionError as exc:
        # Reported as a failed check that names both remedies (#4233).
        raise CiAuditReplayError(str(exc)) from exc
    if lockfile is None:
        raise CiAuditReplayError(f"lockfile at {lockfile_path} is empty or unreadable")

    unchanged, replacement_reasons = lockfile_satisfies_replacements(
        lockfile, manifest.replace_rules
    )
    if not unchanged:
        raise CiAuditReplayError(
            "Replacement rules differ from apm.lock.yaml. "
            + " ".join(replacement_reasons)
            + f" {REPLACEMENT_SOURCES_UNRECORDED.sentence()}"
        )
    manifest_deps = project_manifest_dependencies(manifest)
    satisfied, reasons = lockfile_satisfies_manifest(lockfile, manifest_deps)
    if not satisfied:
        raise CiAuditReplayError("apm.lock.yaml is out of sync with apm.yml. " + " ".join(reasons))

    scratch_root = _make_scratch_root(project_root)
    modules_root = scratch_root / "apm_modules"
    config = ReplayConfig(
        project_root=project_root,
        lockfile_path=lockfile_path,
        cache_only=False,
        scratch_root=scratch_root,
        modules_root=modules_root,
        user_scope=user_scope,
        policy_provider=policy_provider,
        policy_fetch=policy_fetch,
    )
    stderr_context = (
        contextlib.nullcontext() if verbose else contextlib.redirect_stderr(io.StringIO())
    )
    try:
        with contextlib.redirect_stdout(io.StringIO()), stderr_context:
            run_replay(config, CheckLogger(verbose=verbose))
    except Exception as exc:
        raise CiAuditReplayError(str(exc)) from exc

    return PreparedCiAuditReplay(
        scratch_root=scratch_root,
        modules_root=modules_root,
        lockfile_path=lockfile_path,
        tracked_files=_git_tracked_files(project_root),
        targets=tuple(
            resolve_audit_targets(
                project_root,
                user_scope=user_scope,
                lockfile=lockfile,
            )
        ),
    )


def _live_copies_stale(modules_dir: Path, lockfile_path: Path) -> tuple[bool, tuple[str, ...]]:
    """Whether any live ``apm_modules`` copy contradicts its lock row (#3964).

    A copy is stale when the copy-validity owner says so or when its bytes fail
    install's locked content-hash verdict (#4187). The second value names each
    content-hash contradiction with the shared entry/expected/observed line.
    """
    from apm_cli.drift import locked_copy_is_stale

    try:
        lockfile = LockFile.read(lockfile_path)
    except (LockfileFormatError, OSError):
        return False, ()
    if lockfile is None:
        return False, ()
    stale = False
    mismatches: list[str] = []
    for key, dep in lockfile.dependencies.items():
        if key == "." or getattr(dep, "source", None) == "local":
            continue
        try:
            path = dep.to_dependency_ref().get_install_path(modules_dir)
        except (ValueError, OSError):
            continue
        if locked_copy_is_stale(dep, path):
            stale = True
        if path.is_dir() and locked_content_mismatch(key, dep, path, lockfile=lockfile):
            stale = True
            mismatches.append(
                locked_content_mismatch_line(
                    key, dep.content_hash, observed_package_hash(dep, path)
                )
            )
    return stale, tuple(mismatches)


def resolve_ci_audit_materialization(
    project_root: Path,
    *,
    verbose: bool = False,
    policy_provider: Callable[[], Any] | None = None,
    policy_fetch: Callable[[], Any] | None = None,
) -> CiAuditMaterialization:
    """Decide how ``apm audit --ci`` reaches installed package materialization.

    A live ``apm_modules/`` tree is used as-is while every locked copy agrees
    with ``apm.lock.yaml``. A project checkout without one, or whose copies
    the copy-validity owner (``drift.locked_copy_is_stale``) contradicts, gets
    a lock-pinned scratch replay, so audit derives expected state from the
    lock as install does and never reads a stale copy (#3964). The user workspace (``$APM_HOME``) is
    never cold-replayed: it needs ``apm_modules/`` only when the lockfile
    records content installed there (packages or local bundle staging,
    ``LockFile.records_modules_content``). A lockfile holding only
    MCP/LSP servers or the workspace's own content needs none, whichever of
    ``apm install -g`` or ``apm update -g`` wrote it (#3781).
    """
    from apm_cli.constants import APM_MODULES_DIR
    from apm_cli.core.scope import get_workspace_deploy_root

    lockfile_path = get_lockfile_path(project_root)
    if not manifest_file(project_root).exists() or not lockfile_path.exists():
        return CiAuditMaterialization()
    warm = (project_root / APM_MODULES_DIR).exists()
    mismatches: tuple[str, ...] = ()
    if warm:
        stale, mismatches = _live_copies_stale(project_root / APM_MODULES_DIR, lockfile_path)
        if not stale:
            return CiAuditMaterialization()
    if get_workspace_deploy_root(project_root) != project_root.resolve():
        if warm:
            from apm_cli.core.cache_drift import package_cache_drift

            _warn_live_mismatches(mismatches)

            return CiAuditMaterialization(
                error=package_cache_drift(
                    f"Package copies in {project_root / APM_MODULES_DIR}",
                    USER_PACKAGE_COPIES_STALE.clause(),
                )
            )
        try:
            lockfile = LockFile.read(lockfile_path)
        except (LockfileFormatError, OSError):
            lockfile = None
        if lockfile is not None and not lockfile.records_modules_content():
            return CiAuditMaterialization()
        return CiAuditMaterialization(
            error=(
                "installed package materialization is missing at "
                f"{project_root / APM_MODULES_DIR}; "
                f"{USER_PACKAGE_MATERIALIZATION_MISSING.clause()}"
            )
        )
    try:
        prepared = prepare_ci_audit_replay(
            project_root,
            verbose=verbose,
            policy_provider=policy_provider,
            policy_fetch=policy_fetch,
        )
    except CiAuditReplayError as exc:
        # One finding, said once: the replay error already names a mismatch it hit.
        error = str(exc)
        return CiAuditMaterialization(
            error=" ".join((*(line for line in mismatches if line not in error), error))
        )
    _warn_live_mismatches(mismatches)
    return CiAuditMaterialization(prepared_replay=prepared)


def _warn_live_mismatches(mismatches: tuple[str, ...]) -> None:
    """Warn about live copies that contradict the lock while audit verifies elsewhere."""
    from apm_cli.utils.console import _rich_warning

    for line in mismatches:
        _rich_warning(f"{line} Verifying from the locked source.", symbol="warning", err=True)
