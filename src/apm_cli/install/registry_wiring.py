"""Install-phase glue for the dedicated package registry (REST), not HTTP client logic.

``deps.registry`` owns fetching and verifying registry packages. This module
owns how the install pipeline reads ``InstallContext.registry_resolver`` and
lockfile rows to populate ``InstalledPackage.registry_resolution`` -- i.e.
orchestration only, kept out of ``sources.py`` so that file stays strategy-shaped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def get_effective_default_registry(data: dict) -> str | None:
    """Return the effective default registry name for CLI shorthand routing.

    Checks the project-level ``registries.default`` key from *data* (the raw
    ``apm.yml`` dict) first, then falls back to the user-level default
    configured via ``apm config set``.  Returns ``None`` when no default is
    configured at either level.
    """
    try:
        from ..deps.registry.feature_gate import is_package_registry_enabled

        if not is_package_registry_enabled():
            return None
    except Exception:
        return None
    raw_regs = data.get("registries")
    if isinstance(raw_regs, dict):
        project_default = raw_regs.get("default")
        # Only honor a project default that names a configured registry --
        # mirrors _parse_registries_block, which rejects an unconfigured
        # default at load time. Otherwise the CLI would bypass the GitHub
        # probe and write a dep the next manifest load cannot resolve.
        if project_default and project_default in {k for k in raw_regs if k != "default"}:
            return str(project_default)
    try:
        from ..deps.registry.config_loader import resolve_effective_registries

        # resolve_effective_registries is called with an empty project map so
        # only the user-level default is consulted here. Project-level registry
        # names are intentionally excluded: they are validated above against the
        # project registries block, and the user config has no knowledge of
        # project-only registry names.
        _, user_default = resolve_effective_registries({}, None)
        return user_default
    except Exception:
        return None


def should_skip_github_probe_for_dep(dep_ref: Any, default_registry: str | None) -> bool:
    """Return True when dep would be routed to the default registry.

    Delegates the routing predicate to ``routes_unscoped_to_registry`` so this
    gate cannot drift from ``_route_unscoped_to_default_registry``.  No-version
    shorthands also bypass the probe (they are rejected by
    ``validate_registry_ref`` before apm.yml is written).
    """
    if default_registry is None:
        return False
    from ..models.apm_package import routes_unscoped_to_registry

    return routes_unscoped_to_registry(dep_ref)


def validate_registry_ref(dep_ref: Any) -> tuple[bool, str]:
    """Return (True, "") if dep_ref.reference is a usable registry version selector.

    Returns (False, reason) only when no reference is given or when the ref
    looks like a malformed semver expression (e.g. ``^1.0`` missing the patch).
    Non-semver literals (branch names, opaque labels, v-prefixed tags) are
    allowed -- the resolver matches them exactly against the server's published
    version list (registry HTTP API spec, section 1.3).  Call only after
    should_skip_github_probe_for_dep returns True.
    """
    from ..models.dependency.identity import InvalidSemverRangeError

    ref = getattr(dep_ref, "reference", None)
    if not ref:
        repo = getattr(dep_ref, "repo_url", None) or ""
        hint = f"'{repo}#1.0.0'" if repo else "owner/repo#1.0.0"
        return False, (
            f"version selector required: no '#<version>' found but this dep "
            f"would route to the default registry. "
            f"Add a version selector (e.g. {hint}) "
            f"or use the git: URL form in apm.yml to force the GitHub path"
        )
    try:
        _ = dep_ref.ref_kind
    except InvalidSemverRangeError as exc:
        return False, str(exc)
    return True, ""


def get_registry_resolver(ctx: Any) -> Any:
    """Return ``ctx.registry_resolver`` when set (resolve phase may leave it ``None``)."""
    return getattr(ctx, "registry_resolver", None)


def resolver_last_registry_resolution(ctx: Any, dep_key: str) -> Any | None:
    """Per-dep snapshot from the in-process resolver (``last_resolutions``), if any."""
    resolver = get_registry_resolver(ctx)
    if resolver is None:
        return None
    return resolver.last_resolutions.get(dep_key)


def registry_resolution_for_cached_registry_dep(
    ctx: InstallContext,
    dep_ref: Any,
    dep_key: str,
    dep_locked_chk: Any,
) -> Any | None:
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
