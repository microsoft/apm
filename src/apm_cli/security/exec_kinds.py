"""Name the executable kinds a gate retirement removes (#4567).

Uninstall and update reuse install's gate labels when they retire a kind, and
count bin/ only where install deploys it: a skill's top-level ``bin/`` below a
target skills root.
"""

from __future__ import annotations

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
    from pathlib import PurePosixPath

    from ..integration.skill_layout import with_every_skill_layout
    from ..integration.targets import KNOWN_TARGETS, scoped_profile_or_none

    profiles = []
    for profile in KNOWN_TARGETS.values():
        profiles.append(profile)
        user_profile = scoped_profile_or_none(profile, user_scope=True)
        if user_profile is not None:
            profiles.append(user_profile)
    roots = {
        profile.skills_rel_root
        for profile in with_every_skill_layout(profiles)
        if profile.supports("skills") and profile.user_root_resolver is None
    }
    # PurePosixPath, as for recorded paths: CLAUDE_CONFIG_DIR=$HOME gives "./skills".
    return tuple(PurePosixPath(root).parts for root in roots)


def _deployed_bin_file(path: str, skill_roots) -> bool:
    """True for ``<skills root>/<skill>/bin/...``, the only place bin/ deploys."""
    from pathlib import PurePosixPath

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
    """
    skill_roots = _skill_roots()
    bin_deployed = any(_deployed_bin_file(path, skill_roots) for path in deployed_files or ())
    return tuple(
        kind
        for kind in deployed
        if kind in withheld and kind != EXEC_TYPE_MCP and (kind != EXEC_TYPE_BIN or bin_deployed)
    )


def exec_kinds_phrase(kinds) -> str:
    """Join kind labels once each: ``hooks``, ``hooks and LSP server(s)``."""
    labels = list(dict.fromkeys(EXEC_KIND_LABELS.get(kind, kind) for kind in kinds))
    if len(labels) < 2:
        return "".join(labels)
    return f"{', '.join(labels[:-1])} and {labels[-1]}"
