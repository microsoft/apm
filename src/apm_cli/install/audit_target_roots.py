"""Scratch projection helpers for audit target deployment roots."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from apm_cli.core.scope import InstallScope, get_deploy_root, get_modules_dir
from apm_cli.integration.targets import TargetProfile
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

_EXTERNAL_REPLAY_ROOT = ".apm-audit-targets"


def replay_modules_root(scratch_root: Path, *, user_scope: bool) -> Path:
    """Project the canonical scope's deployment source layout into scratch."""
    scope = InstallScope.USER if user_scope else InstallScope.PROJECT
    return scratch_root / get_modules_dir(scope).relative_to(get_deploy_root(scope))


def governed_root_dirs(targets: list[TargetProfile] | None) -> set[str]:
    """Return the set of top-level managed directory names to walk.

    Includes each target's top-level ``root_dir`` (plus ``.apm``) AND every
    per-primitive ``deploy_root`` override (e.g. the ``copilot`` target routing
    ``skills`` to ``.agents``). Walking the deploy roots is what lets the drift
    differ compare committed skill bundles under ``.agents/skills/`` against the
    replay, closing the gap where deployed skill content could silently diverge
    from source (issue #1716). The replay reproduces the deploy-time link
    rewrite faithfully, so byte-identical skills do not surface as false drift.
    Only the first path segment is kept so nested deploy roots collapse to a
    single walk root.
    """
    roots: set[str] = {".apm"}
    for t in targets or []:
        root = getattr(t, "root_dir", None)
        if root:
            roots.add(str(root).split("/", 1)[0])
        primitives = getattr(t, "primitives", None) or {}
        for mapping in primitives.values():
            deploy_root = getattr(mapping, "deploy_root", None)
            if deploy_root:
                roots.add(str(deploy_root).split("/", 1)[0])
    return roots


def canvas_deploy_prefixes(targets: list[TargetProfile] | None) -> set[str]:
    """Return ``root/subdir/`` prefixes for every target carrying a canvas mapping.

    Used to exclude canvas extension deploy paths from drift comparison
    (the replay deliberately does not re-integrate canvases).
    """
    prefixes: set[str] = set()
    for target in targets or []:
        mapping = getattr(target, "primitives", {}).get("canvas")
        if mapping is None:
            continue
        effective_root = mapping.deploy_root or target.root_dir
        if mapping.subdir:
            prefixes.add(f"{effective_root}/{mapping.subdir}/")
        else:
            prefixes.add(f"{effective_root}/")
    return prefixes


def replay_target(target: TargetProfile) -> TargetProfile:
    """Return a scratch-contained profile for replay-only integration."""
    if target.managed_deploy_root is None:
        return target
    return replace(
        target,
        root_dir=f"{_EXTERNAL_REPLAY_ROOT}/{target.name}",
        resolved_deploy_root=None,
    )


def external_replay_root(scratch_root: Path, target: TargetProfile) -> Path:
    """Return the scratch projection root for an external target."""
    return scratch_root / _EXTERNAL_REPLAY_ROOT / target.name


def external_target_relative_roots(target: TargetProfile) -> set[str]:
    """Return bounded paths governed below an external target root."""
    roots: set[str] = set()
    for mapping in target.primitives.values():
        if mapping.deploy_root:
            deploy_root = Path(mapping.deploy_root)
            if not deploy_root.is_absolute():
                roots.add(deploy_root.parts[0])
                continue
        if mapping.subdir:
            roots.add(Path(mapping.subdir).parts[0])
        elif mapping.extension:
            roots.add(mapping.extension.lstrip("/"))
    roots.update(Path(path).parts[0] for path in target.generated_files if Path(path).parts)
    return roots


def claims_for_root(
    claims: dict[str, str],
    root: Path,
    *,
    absolute_only: bool,
    targets: tuple[TargetProfile, ...] = (),
) -> dict[str, str]:
    """Rebase lock claims governed by *root* into comparison-relative paths."""
    root = root.resolve()
    rebased: dict[str, str] = {}
    for path, owner in claims.items():
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                validated = ensure_path_within(candidate, root)
                relative = validated.relative_to(root)
            except (PathTraversalError, ValueError):
                continue
            rebased[relative.as_posix()] = owner
        elif absolute_only:
            for target in targets:
                try:
                    decoded = target.decode_external_locator(path, root)
                except (PathTraversalError, ValueError):
                    continue
                if decoded is None:
                    continue
                validated = ensure_path_within(decoded, root)
                rebased[validated.relative_to(root).as_posix()] = owner
                break
        elif not absolute_only:
            rebased[path] = owner
    return rebased
