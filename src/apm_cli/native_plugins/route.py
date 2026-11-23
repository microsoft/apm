"""The native plugin route decision: native or loose, per dependency (#4995).

One owner for whether a dependency may take the native plugin route on any
harness. Every native planner (Claude Code, Codex) asks it before any trust or
fingerprint work; a ``False`` answer projects the package loose, exactly as
before native plugins existed.
"""

from __future__ import annotations

from typing import Any


def _declared_native(dep: Any, manifest: Any) -> bool | None:
    """The ``native:`` key on *manifest*'s own declarations of *dep*, if any.

    Declarations that disagree resolve to ``False`` whatever their order.
    """
    getter = getattr(manifest, "get_all_apm_dependencies", None)
    if dep is None or getter is None:
        return None
    key = dep.get_unique_key()
    values = {
        getattr(declared, "native", None)
        for declared in getter()
        if declared.get_unique_key() == key
    } - {None}
    return None if not values else all(values)


def effective_native_route(dep: Any, manifest: Any) -> bool:
    """Whether dependency *dep* may take the native plugin route.

    The root *manifest*'s own declaration of *dep* decides through its
    ``native:`` key; otherwise the manifest's top-level ``native_plugins:``
    default; absent both, the route stays automatic (``True``). A key binds
    only its own declaration, so a transitive dependency follows the project
    default and a parent's key never reaches the packages it depends on.
    """
    declared = _declared_native(dep, manifest)
    if declared is not None:
        return declared
    return getattr(manifest, "native_plugins", None) is not False


def frozen_route_change_reasons(lockfile: Any, manifest: Any) -> list[str]:
    """Name each locked native plugin that *manifest* now keeps off the native route.

    ``--frozen`` cannot move a plugin between routes: that retires its native
    registration and rewrites the lock, so the frozen install refuses first.
    """
    reasons = []
    for key, locked in sorted(getattr(lockfile, "dependencies", {}).items()):
        if getattr(locked, "native_plugin", None) is None:
            continue
        if not effective_native_route(locked.to_dependency_ref(), manifest):
            reasons.append(
                f"  - {key} is locked on the native plugin route, but apm.yml now sets "
                "it loose (native: false)"
            )
    return reasons
