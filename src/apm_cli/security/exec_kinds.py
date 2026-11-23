"""Name the executable kinds a gate retirement removes (#4567).

Uninstall and update reuse install's gate labels when they retire a kind, and
count bin/ and canvas only where install deploys them: a skill's top-level
``bin/`` below a target skills root, and a canvas below a target extensions
root (#4816).
"""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath

from .executables import (
    EXEC_TYPE_BIN,
    EXEC_TYPE_CANVAS,
    EXEC_TYPE_HOOKS,
    EXEC_TYPE_LSP,
    EXEC_TYPE_MCP,
)

# How install's gate messages name each kind; uninstall and update reuse them
# when they retire a kind, so every command names a kind identically (#4567).
EXEC_KIND_LABELS = {
    EXEC_TYPE_HOOKS: "hooks",
    EXEC_TYPE_BIN: "bin/ executables",
    EXEC_TYPE_CANVAS: "canvas extension(s)",
    EXEC_TYPE_LSP: "LSP server(s)",
    EXEC_TYPE_MCP: "MCP server(s)",
}


def _skill_roots() -> tuple[tuple[str, ...], ...]:
    """Every static skills root a target deploys to: both scopes, both skill layouts.

    User scope honours relocated homes (``CLAUDE_CONFIG_DIR`` and friends)
    through the target-root owner, so recorded home-relative paths match.
    """
    from ..integration.skill_layout import with_every_skill_layout
    from ..integration.targets import KNOWN_TARGETS, scoped_profile_or_none

    profiles = []
    for profile in KNOWN_TARGETS.values():
        # Dynamic roots (Cowork) record URI locators and may fail to resolve.
        if not profile.supports("skills") or profile.user_root_resolver is not None:
            continue
        profiles.append(profile)
        user_profile = scoped_profile_or_none(profile, user_scope=True)
        if user_profile is not None:
            profiles.append(user_profile)
    roots = {
        profile.skills_rel_root
        for profile in with_every_skill_layout(profiles)
        if profile.supports("skills")
    }
    return tuple(_root_parts(root) for root in roots)


def _root_parts(root: str) -> tuple[str, ...]:
    """Components of a skills root as the lock records paths (POSIX, normalized).

    A user root equal to HOME scopes to ``./skills``; a Windows root carries
    backslashes. Both must match recorded ``skills/<skill>/bin/...`` paths.
    """
    return PurePosixPath(posixpath.normpath(root.replace("\\", "/"))).parts


def _canvas_roots() -> tuple[tuple[str, ...], ...]:
    """Every static canvas extensions root a target deploys to, both scopes (#4816)."""
    from ..integration.targets import KNOWN_TARGETS, scoped_profile_or_none

    roots = set()
    for profile in KNOWN_TARGETS.values():
        if profile.user_root_resolver is not None:
            continue
        for scoped in (profile, scoped_profile_or_none(profile, user_scope=True)):
            if scoped is None or not scoped.supports("canvas"):
                continue
            mapping = scoped.primitives["canvas"]
            roots.add(posixpath.join(mapping.deploy_root or scoped.root_dir, mapping.subdir))
    return tuple(_root_parts(root) for root in roots)


def _deployed_canvas_file(path: str, canvas_roots) -> bool:
    """True for ``<extensions root>/<canvas>/...``, the only place a canvas deploys."""
    parts = PurePosixPath(path).parts
    return any(parts[: len(root)] == root and len(parts) > len(root) + 1 for root in canvas_roots)


def _deployed_bin_file(path: str, skill_roots) -> bool:
    """True for ``<skills root>/<skill>/bin/...``, the only place bin/ deploys."""
    parts = PurePosixPath(path).parts
    for root in skill_roots:
        size = len(root)
        if parts[:size] == root and len(parts) > size + 2 and parts[size + 1] == "bin":
            return True
    return False


def rebuild_retired_kinds(deployed, withheld, deployed_files) -> tuple[str, ...]:
    """Kinds a gated survivor rebuild removes: *withheld* among *deployed*, in order.

    MCP servers are retired and reported by MCP admission, not the rebuild.
    bin/ counts only when *deployed_files* records a file under a skill's
    top-level ``bin/`` directory below a target skills root (the only bin/ the
    install plan withholds), since bin/ deploys only in some scopes and targets.
    A canvas counts only when *deployed_files* records a file below a target
    extensions root, since only some targets map canvas (#4816).
    """
    files = tuple(deployed_files or ())
    skill_roots, canvas_roots = _skill_roots(), _canvas_roots()
    evidence = {
        EXEC_TYPE_BIN: any(_deployed_bin_file(path, skill_roots) for path in files),
        EXEC_TYPE_CANVAS: any(_deployed_canvas_file(path, canvas_roots) for path in files),
    }
    return tuple(
        kind
        for kind in deployed
        if kind in withheld and kind != EXEC_TYPE_MCP and evidence.get(kind, True)
    )


def exec_kinds_phrase(kinds) -> str:
    """Join kind labels once each: ``hooks``, ``hooks and LSP server(s)``."""
    labels = list(dict.fromkeys(EXEC_KIND_LABELS.get(kind, kind) for kind in kinds))
    if len(labels) < 2:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def retired_kinds_warnings(hooks, *, skip) -> list[str]:
    """One warning per retired package naming the kinds its rebuild removes.

    *hooks* maps a lock key to ``(why, kinds)``. A package in *skip* (already
    reported) or with no kind the rebuild removes (MCP only: MCP admission
    reports it) gets none.
    """
    return [
        f"{key} {exec_kinds_phrase(kinds)} are no longer allowed ({why}); removing them"
        for key, (why, kinds) in sorted(hooks.items())
        if key not in skip and kinds
    ]
