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
from dataclasses import dataclass
from pathlib import Path

from apm_cli.deps.lockfile import LockFile, LockfileFormatError, get_lockfile_path
from apm_cli.install.drift import (
    CheckLogger,
    ReplayConfig,
    _make_scratch_root,
    run_replay,
)
from apm_cli.install.plan import lockfile_satisfies_manifest, lockfile_satisfies_replacements
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
    try:
        completed = subprocess.run(
            ("git", "-C", str(project_root), "ls-files", "-z", "--full-name"),
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
) -> PreparedCiAuditReplay:
    """Prepare one lock-pinned scratch replay for ``apm audit --ci`` consumers."""
    project_root = project_root.resolve()
    from apm_cli.core.scope import get_workspace_deploy_root

    user_scope = get_workspace_deploy_root(project_root) != project_root
    lockfile_path = get_lockfile_path(project_root)
    if not lockfile_path.exists():
        raise CiAuditReplayError(
            f"lockfile not found at {lockfile_path}; run 'apm install' to generate it"
        )

    manifest = APMPackage.from_apm_yml(project_root / "apm.yml")
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        raise CiAuditReplayError(f"lockfile at {lockfile_path} is empty or unreadable")

    unchanged, replacement_reasons = lockfile_satisfies_replacements(
        lockfile, manifest.replace_rules
    )
    if not unchanged:
        raise CiAuditReplayError(
            "Replacement rules differ from apm.lock.yaml. "
            + " ".join(replacement_reasons)
            + " Run 'apm install' to record the selected sources."
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


def resolve_ci_audit_materialization(
    project_root: Path,
    *,
    verbose: bool = False,
) -> CiAuditMaterialization:
    """Decide how ``apm audit --ci`` reaches installed package materialization.

    A live ``apm_modules/`` tree is used as-is. A project checkout without one
    gets a lock-pinned scratch replay. The user workspace (``$APM_HOME``) is
    never cold-replayed: it needs ``apm_modules/`` only when the lockfile
    records content installed there (packages or local bundle staging,
    ``LockFile.records_modules_content``). A lockfile holding only
    MCP/LSP servers or the workspace's own content needs none, whichever of
    ``apm install -g`` or ``apm update -g`` wrote it (#3781).
    """
    from apm_cli.constants import APM_MODULES_DIR, APM_YML_FILENAME
    from apm_cli.core.scope import get_workspace_deploy_root

    lockfile_path = get_lockfile_path(project_root)
    if (
        not (project_root / APM_YML_FILENAME).exists()
        or (project_root / APM_MODULES_DIR).exists()
        or not lockfile_path.exists()
    ):
        return CiAuditMaterialization()
    if get_workspace_deploy_root(project_root) != project_root.resolve():
        try:
            lockfile = LockFile.read(lockfile_path)
        except (LockfileFormatError, OSError):
            lockfile = None
        if lockfile is not None and not lockfile.records_modules_content():
            return CiAuditMaterialization()
        return CiAuditMaterialization(
            error=(
                "installed package materialization is missing at "
                f"{project_root / APM_MODULES_DIR}; run 'apm install --global' "
                "to restore it"
            )
        )
    try:
        return CiAuditMaterialization(
            prepared_replay=prepare_ci_audit_replay(project_root, verbose=verbose)
        )
    except CiAuditReplayError as exc:
        return CiAuditMaterialization(error=str(exc))
