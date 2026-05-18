"""Target resolution functions for multi-tool integration.

Extracted from ``target_runtime`` to keep that module under 400 LOC.
``active_targets``, ``active_targets_user_scope``, and ``resolve_targets``
are re-exported from ``target_runtime`` for backward compatibility; new
code should import them from here directly.
"""

from __future__ import annotations

from ._known_targets import KNOWN_TARGETS, RUNTIME_TO_CANONICAL_TARGET
from .targets import _flag_gated

__all__ = [
    "active_targets",
    "active_targets_user_scope",
    "resolve_targets",
]


def active_targets_user_scope(
    explicit_target: str | list[str] | None = None,
) -> list:
    """Return ``TargetProfile`` instances for user-scope deployment.

    Mirrors ``active_targets()`` but operates against ``~/`` and filters
    out targets that do not support user scope.

    Resolution order:

    1. **Explicit target** (``--target``): returns the matching profile(s)
       that support user scope.  ``"all"`` returns every user-capable
       target.  Validity is enforced upstream by
       :func:`apm_cli.core.target_detection.parse_target_field`; this
       function does not silently fall back when given unknown tokens.
    2. **Directory detection**: profiles whose ``effective_root(user_scope=True)``
       directory exists under ``~/``.
    3. **Fallback**: ``[copilot]`` -- same default as project scope.
    """
    from pathlib import Path

    home = Path.home()

    # --- explicit target ---
    if explicit_target:
        # See module docstring on the parse_target_field gate-keeping contract.
        raw = [explicit_target] if isinstance(explicit_target, str) else list(explicit_target)
        profiles: list = []
        seen: set = set()
        for t in raw:
            canonical = RUNTIME_TO_CANONICAL_TARGET.get(t, t)
            if canonical == "all":
                from apm_cli.core.target_detection import EXPLICIT_ONLY_TARGETS

                return [
                    p
                    for p in KNOWN_TARGETS.values()
                    if p.user_supported and _flag_gated(p) and p.name not in EXPLICIT_ONLY_TARGETS
                ]
            profile = KNOWN_TARGETS.get(canonical)
            if (
                profile
                and profile.user_supported
                and _flag_gated(profile)
                and profile.name not in seen
            ):
                seen.add(profile.name)
                profiles.append(profile)
        return profiles

    # --- auto-detect by directory presence at ~/ ---
    # Targets with detect_by_dir=False (cowork) are never auto-detected.
    detected = [
        p
        for p in KNOWN_TARGETS.values()
        if p.user_supported
        and p.detect_by_dir
        and _flag_gated(p)
        and (home / p.effective_root(user_scope=True)).is_dir()
    ]
    if detected:
        return detected

    # --- fallback: copilot is the universal default ---
    return [KNOWN_TARGETS["copilot"]]


def active_targets(
    project_root,
    explicit_target: str | list[str] | None = None,
) -> list:
    """Return the list of ``TargetProfile`` instances that should be
    deployed into *project_root*.

    Resolution order:

    1. **Explicit target** (``--target`` flag or ``apm.yml target:``):
       returns the matching profile(s).  ``"all"`` returns every known
       target.  Validity is enforced upstream by
       :func:`apm_cli.core.target_detection.parse_target_field`; unknown
       tokens never reach here, so this branch never silently falls back
       to ``[copilot]``.
    2. **Directory detection**: profiles whose ``root_dir`` already
       exists under *project_root*.
    3. **Fallback**: when nothing is detected, returns ``[copilot]``
       so greenfield projects get a default skills root.

    Args:
        project_root: The workspace root ``Path``.
        explicit_target: Canonical target name, list of canonical names,
            or ``"all"``/``None``.  ``None`` means auto-detect.
    """
    from pathlib import Path

    root = Path(project_root)

    # --- explicit target ---
    if explicit_target:
        # See module docstring on the parse_target_field gate-keeping contract.
        raw = [explicit_target] if isinstance(explicit_target, str) else list(explicit_target)
        profiles: list = []
        seen: set = set()
        for t in raw:
            canonical = RUNTIME_TO_CANONICAL_TARGET.get(t, t)
            if canonical == "all":
                # Exclude explicit-only targets (agent-skills) -- they must
                # be requested individually.
                # Exclude experimental targets (copilot-cowork) -- they must
                # be opted into explicitly via `--target copilot-cowork`,
                # matching the documented contract on EXPERIMENTAL_TARGETS in
                # core/target_detection.py. Including cowork in `all` for
                # project scope hits the unconditional project-scope gate in
                # phases/targets.py and aborts the entire install (#1185 b).
                from apm_cli.core.target_detection import (
                    EXPERIMENTAL_TARGETS,
                    EXPLICIT_ONLY_TARGETS,
                )

                return [
                    p
                    for p in KNOWN_TARGETS.values()
                    if p.name not in EXPLICIT_ONLY_TARGETS and p.name not in EXPERIMENTAL_TARGETS
                ]
            profile = KNOWN_TARGETS.get(canonical)
            if profile and _flag_gated(profile) and profile.name not in seen:
                seen.add(profile.name)
                profiles.append(profile)
        return profiles

    # --- auto-detect by directory presence ---
    # Targets with detect_by_dir=False (cowork) are never auto-detected.
    detected = [
        p
        for p in KNOWN_TARGETS.values()
        if p.detect_by_dir and _flag_gated(p) and (root / p.root_dir).is_dir()
    ]
    if detected:
        return detected

    # --- fallback: copilot is the universal default ---
    return [KNOWN_TARGETS["copilot"]]


def resolve_targets(
    project_root,
    user_scope: bool = False,
    explicit_target: str | list[str] | None = None,
) -> list:
    """Return scope-resolved ``TargetProfile`` instances.

    This is the **single entry point** for obtaining deployment targets.
    It combines target detection (or explicit selection), scope resolution
    (``for_scope``), and primitive filtering into one call.

    Callers receive profiles where ``root_dir`` is already correct for
    the requested scope -- no ``effective_root()`` calls needed.

    Args:
        project_root: Workspace root (``Path.cwd()`` or ``Path.home()``).
        user_scope: When ``True``, resolve for user-level deployment.
        explicit_target: Canonical target name, list of canonical names,
            or ``"all"``.  ``None`` means auto-detect.
    """
    if user_scope:
        raw = active_targets_user_scope(explicit_target)
    else:
        raw = active_targets(project_root, explicit_target)

    resolved = []
    for t in raw:
        scoped = t.for_scope(user_scope=user_scope)
        if scoped is not None:
            resolved.append(scoped)
    return resolved
