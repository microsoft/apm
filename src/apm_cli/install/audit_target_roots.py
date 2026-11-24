"""Scratch projection helpers for audit target deployment roots."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from apm_cli.constants import APM_MODULES_DIR
from apm_cli.core.scope import InstallScope, get_deploy_root, get_modules_dir
from apm_cli.integration.targets import KNOWN_TARGETS, TargetProfile
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

_EXTERNAL_REPLAY_ROOT = ".apm-audit-targets"


def replay_deployment_frame(
    scratch_root: Path, project_root: Path, *, user_scope: bool
) -> tuple[Path, tuple[Path, Path]]:
    """Return live ``apm_modules`` and the ``(scratch, live deploy root)`` link frame.

    Replay renders dependency asset links in the live coordinates install used,
    so they match wherever ``apm_modules`` sits relative to the deploy root (an
    absolute ``APM_HOME`` outside ``HOME``, #3901), independent of scratch depth
    or drive. The frame only shapes link text; nothing is written through it.
    User scope keeps the unresolved scope paths install used: resolving them
    (as get_workspace_deploy_root does) changes link text under symlinked roots.
    """
    if user_scope:
        scope = InstallScope.USER
        return get_modules_dir(scope), (scratch_root, get_deploy_root(scope))
    return project_root / APM_MODULES_DIR, (scratch_root, project_root)


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
        # A lock can mix skill layouts (a parked native replacement keeps its
        # legacy claims, #5092): walk the converged skills root as well. Only a
        # user-scope profile whose own override places skills has none (#5162).
        converged = KNOWN_TARGETS.get(getattr(t, "name", None))
        if converged is None or (
            "skills" in (converged.user_primitive_overrides or {})
            and root == converged.user_root_dir
        ):
            continue
        skills = converged.primitives.get("skills")
        if skills is not None and skills.deploy_root and "skills" in primitives:
            roots.add(skills.deploy_root.split("/", 1)[0])
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


def replay_targets(
    converged: list[TargetProfile], *, lock=None, recorded_paths=()
) -> list[TargetProfile]:
    """Return replay profiles in the skill layout *recorded_paths* record.

    *lock* recovers the layout from every claim it records (scratch roots and
    governed walks); a dependency passes its own claims so a lock that mixes
    layouts replays each dependency where install deployed it (#5092).
    """
    from apm_cli.integration.skill_layout import recorded_deployed_paths, resolve_skill_layout

    if lock is not None:
        recorded_paths = recorded_deployed_paths(lock)
    return [
        replay_target(target)
        for target in resolve_skill_layout(converged, recorded_paths=recorded_paths)
    ]


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
