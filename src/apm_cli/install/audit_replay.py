"""Shared cold-cache scratch replay owner for ``apm audit --ci``.

This module owns the one-shot orchestration that turns a checkout with a
lockfile but no live ``apm_modules/`` tree into a prepared, lock-pinned scratch
replay. ``commands/audit.py`` creates the replay once, then both
``config-consistency`` and ``drift`` consume the same materialized state.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
from dataclasses import dataclass
from pathlib import Path

from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.install.drift import (
    CheckLogger,
    ReplayConfig,
    _make_scratch_root,
    _read_apm_yml_target,
    run_replay,
)
from apm_cli.install.plan import lockfile_satisfies_manifest
from apm_cli.integration.targets import TargetProfile, resolve_targets
from apm_cli.models.apm_package import APMPackage


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
    lockfile_path = get_lockfile_path(project_root)
    if not lockfile_path.exists():
        raise CiAuditReplayError(
            f"lockfile not found at {lockfile_path}; run 'apm install' to generate it"
        )

    manifest = APMPackage.from_apm_yml(project_root / "apm.yml")
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        raise CiAuditReplayError(f"lockfile at {lockfile_path} is empty or unreadable")

    manifest_deps = list(manifest.get_apm_dependencies())
    manifest_deps.extend(manifest.get_dev_apm_dependencies())
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
    )
    stderr_context = (
        contextlib.nullcontext() if verbose else contextlib.redirect_stderr(io.StringIO())
    )
    try:
        with contextlib.redirect_stdout(io.StringIO()), stderr_context:
            run_replay(config, CheckLogger(verbose=verbose))
    except Exception as exc:
        raise CiAuditReplayError(str(exc)) from exc

    explicit_target = _read_apm_yml_target(project_root)
    return PreparedCiAuditReplay(
        scratch_root=scratch_root,
        modules_root=modules_root,
        lockfile_path=lockfile_path,
        tracked_files=_git_tracked_files(project_root),
        targets=tuple(resolve_targets(scratch_root, explicit_target=explicit_target)),
    )
