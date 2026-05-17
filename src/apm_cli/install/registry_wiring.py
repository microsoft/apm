"""Install-phase glue for the dedicated package registry (REST), not HTTP client logic.

``deps.registry`` owns fetching and verifying registry packages. This module
owns how the install pipeline reads ``InstallContext.registry_resolver`` and
lockfile rows to populate ``InstalledPackage.registry_resolution`` — i.e.
orchestration only, kept out of ``sources.py`` so that file stays strategy-shaped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def get_registry_resolver(ctx: Any) -> Any:
    """Return ``ctx.registry_resolver`` when set (resolve phase may leave it ``None``)."""
    return getattr(ctx, "registry_resolver", None)


def resolver_last_registry_resolution(ctx: Any, dep_key: str) -> Optional[Any]:
    """Per-dep snapshot from the in-process resolver (``last_resolutions``), if any."""
    resolver = get_registry_resolver(ctx)
    if resolver is None:
        return None
    return resolver.last_resolutions.get(dep_key)


def registry_resolution_for_cached_registry_dep(
    ctx: "InstallContext",
    dep_ref: Any,
    dep_key: str,
    dep_locked_chk: Any,
) -> Optional[Any]:
    """Build ``RegistryResolution`` for a registry dep on the cached install path.

    Two sources, so the lockfile keeps ``resolved_url``, ``resolved_hash``, and
    ``version`` when the package is reused from disk instead of re-downloaded:

    1. The resolver's ``last_resolutions`` map — filled when the BFS callback in
       ``resolve.py`` just downloaded this dep during dependency-graph resolution
       (e.g. first install with no prior lockfile). This path was the bug fix.
    2. The existing lockfile row — used on re-install when the tree is already on
       disk and was verified earlier (original phase-7 wiring).

    If neither applies, a registry dep on the cached path would degrade to a
    v1-shaped lockfile entry (missing registry resolution fields).
    """
    if dep_ref.source != "registry":
        return None
    hit = resolver_last_registry_resolution(ctx, dep_key)
    if hit is not None:
        return hit
    if dep_locked_chk and dep_locked_chk.resolved_url:
        from apm_cli.deps.registry.resolver import RegistryResolution

        return RegistryResolution(
            resolved_url=dep_locked_chk.resolved_url,
            resolved_hash=dep_locked_chk.resolved_hash or "",
            version=dep_locked_chk.version or (dep_ref.reference or ""),
        )
    return None
