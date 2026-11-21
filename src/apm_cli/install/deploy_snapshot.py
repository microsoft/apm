"""Audit-gated snapshot of the paths an install writes before its last gate (#4085).

The install-time content audit (``phases/audit.py``) must scan the exact bytes
integrators deployed, so its blocking verdict lands after deployment. When
that audit is on, :class:`~apm_cli.install.transaction.InstallTransaction`
captures every path the integrate, cleanup, lockfile and post-deps-local
phases can write, before the first of them runs. A failure before the audit
passes restores those paths byte for byte and removes the ones that did not
exist; a pass discards the copy. When the audit is off nothing is captured.
:func:`remove_empty_directories` also lets the transaction remove target roots
a failed install created and left empty, at no copy cost (#4159).

Only the transaction may call :func:`capture_audit_gated_snapshot`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from apm_cli.utils.file_ops import robust_rmtree

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext

_Kind = Literal["absent", "file", "dir", "symlink"]


@dataclass(frozen=True)
class _Unit:
    """One captured path and how to put it back."""

    path: Path
    kind: _Kind
    copy: Path | None
    link: str | None
    absent_ancestors: tuple[Path, ...]


def _absent_ancestors(path: Path) -> tuple[Path, ...]:
    """Return the missing parents of *path*, deepest first."""
    missing = []
    parent = path.parent
    while parent != parent.parent and not os.path.lexists(parent):
        missing.append(parent)
        parent = parent.parent
    return tuple(missing)


def remove_empty_directories(directories: Iterable[Path]) -> None:
    """Remove each directory that is empty, deepest first; keep anything else."""
    for directory in sorted(set(directories), key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):
            if directory.is_dir() and not directory.is_symlink():
                directory.rmdir()


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        robust_rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


class DeployTargetSnapshot:
    """Byte-exact copy of a fixed set of deploy paths, kept outside them."""

    def __init__(self, store: Path, units: tuple[_Unit, ...]) -> None:
        self.store = store
        self.units = units

    @classmethod
    def capture(cls, paths: list[Path], store_parent: Path) -> DeployTargetSnapshot:
        """Copy every path in *paths* (and record the absent ones) under a new store."""
        store_parent.mkdir(parents=True, exist_ok=True)
        store = Path(tempfile.mkdtemp(prefix="deploy-", dir=store_parent))
        units: list[_Unit] = []
        try:
            for index, path in enumerate(paths):
                copy = store / str(index)
                ancestors = _absent_ancestors(path)
                if path.is_symlink():
                    units.append(_Unit(path, "symlink", None, os.readlink(path), ancestors))
                elif path.is_dir():
                    shutil.copytree(path, copy, symlinks=True)
                    units.append(_Unit(path, "dir", copy, None, ancestors))
                elif path.is_file():
                    shutil.copy2(path, copy)
                    units.append(_Unit(path, "file", copy, None, ancestors))
                elif not os.path.lexists(path):
                    units.append(_Unit(path, "absent", None, None, ancestors))
        except BaseException:
            shutil.rmtree(store, ignore_errors=True)
            raise
        return cls(store, tuple(units))

    def restore(self) -> list[tuple[Path, str]]:
        """Put every captured path back; return the paths that could not be restored."""
        issues: list[tuple[Path, str]] = []
        for unit in self.units:
            try:
                self._restore_unit(unit)
            except OSError as exc:
                issues.append((unit.path, f"could not restore: {exc}"))
        remove_empty_directories(a for unit in self.units for a in unit.absent_ancestors)
        if issues:
            issues.append((self.store, "pre-install copies retained here for recovery"))
        else:
            self.discard()
        return issues

    @staticmethod
    def _restore_unit(unit: _Unit) -> None:
        path, copy = unit.path, unit.copy
        if unit.kind == "file" and copy is not None and path.is_file() and not path.is_symlink():
            if path.read_bytes() == copy.read_bytes():
                return
        if unit.kind == "symlink" and path.is_symlink() and os.readlink(path) == unit.link:
            return
        _remove(path)
        if unit.kind == "absent":
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if unit.link is not None:
            os.symlink(unit.link, path)
        elif unit.kind == "dir" and copy is not None:
            shutil.copytree(copy, path, symlinks=True)
        elif copy is not None:
            shutil.copy2(copy, path)

    def discard(self) -> None:
        """Delete the stored copies and the empty store parent."""
        shutil.rmtree(self.store, ignore_errors=True)
        with contextlib.suppress(OSError):
            self.store.parent.rmdir()


def _target_paths(ctx: InstallContext, base: Path, user_scope: bool) -> list[Path]:
    """Return every path the active targets' integrators and hook reconcilers write."""
    from apm_cli.integration.hook_integrator import _APM_HOOKS_SIDECAR, _MERGE_HOOK_TARGETS
    from apm_cli.integration.targets import KNOWN_TARGETS, scoped_profile_or_none

    paths: list[Path] = []
    for target in ctx.targets or ():
        if target.resolved_deploy_root is not None:
            # File-backed dynamic roots decode their locators; the others
            # (copilot-app) store rows in an application database, not files.
            if target.external_locator_decoder is not None:
                paths.append(target.resolved_deploy_root)
            continue
        for primitive, mapping in target.primitives.items():
            if primitive == "skills":
                paths.append(target.skills_deploy_path(base))
            elif mapping.deploy_root:
                paths.append(base / mapping.deploy_root / mapping.subdir)
            elif mapping.subdir:
                paths.append(target.deploy_path(base, mapping.subdir))
        if "hooks" in target.primitives:
            paths.append(target.deploy_path(base, "hooks"))
    # Merge-hook configs: active targets merge into them, and dropped targets
    # are reconciled by the lockfile phase.
    for name, config in _MERGE_HOOK_TARGETS.items():
        profile = KNOWN_TARGETS.get(name)
        scoped = scoped_profile_or_none(profile, user_scope=user_scope) if profile else None
        if scoped is None or scoped.resolved_deploy_root is not None:
            continue
        target_dir = scoped.deploy_path(base)
        paths.extend((target_dir / config.config_filename, target_dir / _APM_HOOKS_SIDECAR))
    return paths


def _claimed_paths(ctx: InstallContext, base: Path) -> list[Path]:
    """Return the previous install's deployed claims that cleanup may remove."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

    if ctx.existing_lockfile is None:
        return []
    paths = []
    for claim in DeploymentLedgerCodec.legacy_deployed_file_claims(ctx.existing_lockfile):
        text = str(claim)
        if not text or "://" in text:
            continue
        with contextlib.suppress(PathTraversalError, ValueError):
            ensure_path_within(base / text, base)
            paths.append(base / text)
    return paths


def audit_gated_paths(ctx: InstallContext, store_parent: Path) -> list[Path]:
    """Return the disjoint paths an install can write before its audit passes.

    A path that contains the project root, ``apm_modules`` (owned by the
    resolution journal) or the snapshot store is never captured.
    """
    from apm_cli.core.deployment_ledger import materialization_evidence_paths
    from apm_cli.core.scope import InstallScope

    base = Path(os.path.abspath(ctx.project_root))
    user_scope = ctx.scope is InstallScope.USER
    state_path, identity_path = materialization_evidence_paths(base, ctx.scope)
    candidates = [
        *_target_paths(ctx, base, user_scope),
        *_claimed_paths(ctx, base),
        state_path,
        identity_path.parent,
    ]
    if not user_scope:
        candidates.append(base / ".gitignore")
    # A symlinked deploy path is restored as a link; its target holds the bytes.
    candidates += [Path(os.path.realpath(p)) for p in candidates]
    owned = [Path(os.path.abspath(p)) for p in (ctx.apm_modules_dir, store_parent) if p]
    chosen: list[Path] = []
    for path in sorted({Path(os.path.abspath(p)) for p in candidates}, key=lambda p: len(p.parts)):
        if (
            base.is_relative_to(path)
            or any(p.is_relative_to(path) or path.is_relative_to(p) for p in owned)
            or any(path.is_relative_to(kept) for kept in chosen)
        ):
            continue
        chosen.append(path)
    return chosen


def capture_audit_gated_snapshot(ctx: InstallContext) -> DeployTargetSnapshot | None:
    """Capture the deploy paths when the install-time audit can block, else ``None``."""
    if ctx.dry_run or ctx.lockfile_only:
        return None
    from apm_cli.core.install_audit import decide_for_install

    if decide_for_install(ctx).mode == "off":
        return None
    from apm_cli.core.scope import get_apm_home

    store_parent = get_apm_home() / "install-snapshots"
    return DeployTargetSnapshot.capture(audit_gated_paths(ctx, store_parent), store_parent)
