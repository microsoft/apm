"""Policy inheritance: resolve and merge policy chains.

Supports three-level chains: enterprise hub -> org -> repo.
Each level can tighten but never relax the parent.

extends: values:
- "org"              -> same org's .github repo (repo-level override)
- "<owner>/<repo>"   -> cross-org reference (enterprise policy hub)
- "https://..."      -> direct URL
"""

from __future__ import annotations

from .schema import (
    ApmPolicy,
    AuditPolicy,
    BinDeployPolicy,
    CompilationPolicy,
    CompilationStrategyPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    ManifestPolicy,
    McpPolicy,
    McpTransportPolicy,
    PolicyCache,
    RegistrySourcePolicy,
    ScannerGovernance,
    SecurityPolicy,
    UnmanagedFilesPolicy,
)

MAX_CHAIN_DEPTH = 5

# Escalation ladders -- index = severity, higher is stricter.
_ENFORCEMENT_LEVELS = {"off": 0, "warn": 1, "block": 2}
_FETCH_FAILURE_LEVELS = {"warn": 0, "block": 1}
_RESOLUTION_LEVELS = {"project-wins": 0, "policy-wins": 1, "block": 2}
_SELF_DEFINED_LEVELS = {"allow": 0, "warn": 1, "deny": 2}
_UNMANAGED_ACTION_LEVELS = {"ignore": 0, "warn": 1, "deny": 2}
_SCRIPTS_LEVELS = {"allow": 0, "deny": 1}
_AUDIT_ON_INSTALL_LEVELS = {"off": 0, "warn": 1, "block": 2}


class PolicyInheritanceError(Exception):
    """Raised when policy inheritance chain is invalid."""

    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def merge_policies(parent: ApmPolicy, child: ApmPolicy) -> ApmPolicy:
    """Merge a child policy with its parent.

    The child can TIGHTEN but never RELAX the parent's constraints.
    """
    return ApmPolicy(
        name=child.name or parent.name,
        version=child.version or parent.version,
        extends=None,  # resolved, no longer needed
        enforcement=_merge_enforcement(parent.enforcement, child.enforcement),
        fetch_failure=_merge_fetch_failure(parent.fetch_failure, child.fetch_failure),
        cache=_merge_cache(parent.cache, child.cache),
        dependencies=_merge_dependencies(parent.dependencies, child.dependencies),
        mcp=_merge_mcp(parent.mcp, child.mcp),
        compilation=_merge_compilation(parent.compilation, child.compilation),
        manifest=_merge_manifest(parent.manifest, child.manifest),
        unmanaged_files=_merge_unmanaged_files(parent.unmanaged_files, child.unmanaged_files),
        registry_source=_merge_registry_source(parent.registry_source, child.registry_source),
        security=_merge_security(parent.security, child.security),
        bin_deploy=_merge_bin_deploy(parent.bin_deploy, child.bin_deploy),
    )


def resolve_policy_chain(policies: list[ApmPolicy]) -> ApmPolicy:
    """Merge an ordered policy list [root, ..., leaf] left-to-right.

    Raises ``PolicyInheritanceError`` if the chain exceeds
    ``MAX_CHAIN_DEPTH``.
    """
    if not policies:
        return ApmPolicy()

    chain_refs = [p.extends or p.name or f"<policy-{i}>" for i, p in enumerate(policies)]
    validate_chain_depth(chain_refs)

    result = policies[0]
    for child in policies[1:]:
        result = merge_policies(result, child)
    return result


def validate_chain_depth(chain: list[str]) -> None:
    """Raise ``PolicyInheritanceError`` if *chain* exceeds ``MAX_CHAIN_DEPTH``."""
    if len(chain) > MAX_CHAIN_DEPTH:
        raise PolicyInheritanceError(
            f"Policy chain depth {len(chain)} exceeds maximum of {MAX_CHAIN_DEPTH}"
        )


def detect_cycle(visited: list[str], next_ref: str) -> bool:
    """Return ``True`` if *next_ref* would create a cycle."""
    return next_ref in visited


# ---------------------------------------------------------------------------
# Scalar escalation helpers
# ---------------------------------------------------------------------------


def _escalate(levels: dict[str, int], parent_val: str, child_val: str) -> str:
    """Return the stricter of two values on an escalation ladder.

    Raises ``PolicyInheritanceError`` for unknown values -- validated
    policies should never reach this, but failing loudly is safer than
    silently downgrading enforcement.
    """
    if parent_val not in levels:
        raise PolicyInheritanceError(f"Unknown escalation value: {parent_val!r}")
    if child_val not in levels:
        raise PolicyInheritanceError(f"Unknown escalation value: {child_val!r}")
    p = levels[parent_val]
    c = levels[child_val]
    target = max(p, c)
    for name, rank in levels.items():
        if rank == target:
            return name
    return parent_val  # pragma: no cover -- defensive fallback


# ---------------------------------------------------------------------------
# Section merges
# ---------------------------------------------------------------------------


def _merge_enforcement(parent: str, child: str) -> str:
    return _escalate(_ENFORCEMENT_LEVELS, parent, child)


def _merge_fetch_failure(parent: str, child: str) -> str:
    """Escalate fetch_failure on the warn < block ladder (tighten, never relax)."""
    return _escalate(_FETCH_FAILURE_LEVELS, parent, child)


def _merge_registry_source(
    parent: RegistrySourcePolicy, child: RegistrySourcePolicy
) -> RegistrySourcePolicy:
    """Merge registry-source policy: required registries union, restrict-only.

    ``require`` is union-merged like other require lists -- a child can add
    mandated registries but never drop a parent's. ``allow_non_registry`` is
    AND-merged so once any ancestor blocks non-registry sources (``False``),
    a child cannot relax it back to ``True``.
    """
    return RegistrySourcePolicy(
        require=_union(parent.require, child.require),
        allow_non_registry=parent.allow_non_registry and child.allow_non_registry,
    )


def _merge_bin_deploy(parent: BinDeployPolicy, child: BinDeployPolicy) -> BinDeployPolicy:
    """Merge bin/ deployment policy: restrict-only.

    ``deny_all`` OR-merges so any ancestor's kill-switch (``True``) sticks;
    ``deny`` is union-merged so a child can add packages but never drop a
    parent's denied entries.
    """
    return BinDeployPolicy(
        deny_all=parent.deny_all or child.deny_all,
        deny=_union(parent.deny, child.deny),
    )


def _merge_cache(parent: PolicyCache, child: PolicyCache) -> PolicyCache:
    return PolicyCache(ttl=min(parent.ttl, child.ttl))


def _merge_dependencies(parent: DependencyPolicy, child: DependencyPolicy) -> DependencyPolicy:
    return DependencyPolicy(
        deny=_merge_list_field(parent.deny, child.deny),
        allow=_intersect_allow(parent.allow, child.allow),
        require=_merge_list_field(parent.require, child.require),
        require_resolution=_escalate(
            _RESOLUTION_LEVELS, parent.require_resolution, child.require_resolution
        ),
        max_depth=min(parent.max_depth, child.max_depth),
        # Strict-wins: once a parent (org) policy enables the pin
        # requirement, a child cannot relax it. This matches
        # ``allow``/``deny``/``require`` semantics where the child can
        # only narrow, never broaden.
        require_pinned_constraint=parent.require_pinned_constraint
        or child.require_pinned_constraint,
    )


def _merge_mcp(parent: McpPolicy, child: McpPolicy) -> McpPolicy:
    return McpPolicy(
        deny=_union(parent.deny, child.deny),
        allow=_intersect_allow(parent.allow, child.allow),
        transport=McpTransportPolicy(
            allow=_intersect_allow(parent.transport.allow, child.transport.allow),
        ),
        self_defined=_escalate(_SELF_DEFINED_LEVELS, parent.self_defined, child.self_defined),
        trust_transitive=parent.trust_transitive and child.trust_transitive,
    )


def _merge_compilation(parent: CompilationPolicy, child: CompilationPolicy) -> CompilationPolicy:
    return CompilationPolicy(
        target=CompilationTargetPolicy(
            allow=_intersect_allow(parent.target.allow, child.target.allow),
            enforce=parent.target.enforce or child.target.enforce,
        ),
        strategy=CompilationStrategyPolicy(
            enforce=parent.strategy.enforce or child.strategy.enforce,
        ),
        source_attribution=parent.source_attribution or child.source_attribution,
    )


def _merge_manifest(parent: ManifestPolicy, child: ManifestPolicy) -> ManifestPolicy:
    child_ct_allow = _extract_ct_allow(child.content_types)
    parent_ct_allow = _extract_ct_allow(parent.content_types)
    merged_ct_allow = _intersect_allow(parent_ct_allow, child_ct_allow)

    # Preserve content_types structure only if at least one side defined it.
    merged_content_types: dict | None = None
    if parent.content_types is not None or child.content_types is not None:
        merged_content_types = {
            "allow": list(merged_ct_allow) if merged_ct_allow is not None else []
        }

    return ManifestPolicy(
        required_fields=_union(parent.required_fields, child.required_fields),
        scripts=_escalate(_SCRIPTS_LEVELS, parent.scripts, child.scripts),
        content_types=merged_content_types,
    )


def _coerce_unmanaged_action_for_escalate(value: str | None) -> str:
    """Treat ``None`` as the weakest rung when comparing two concrete opinions."""
    return "ignore" if value is None else value


def _coerce_unmanaged_directories_for_union(value: tuple[str, ...] | None) -> tuple[str, ...]:
    return () if value is None else value


def _merge_unmanaged_files(
    parent: UnmanagedFilesPolicy, child: UnmanagedFilesPolicy
) -> UnmanagedFilesPolicy:
    """Merge unmanaged-files policy; omitted child block is transparent (#1198)."""
    if child.action is None and child.directories is None:
        return parent

    if child.action is None:
        eff_action_raw = parent.action
    else:
        eff_action_raw = _escalate(
            _UNMANAGED_ACTION_LEVELS,
            _coerce_unmanaged_action_for_escalate(parent.action),
            child.action,
        )

    if child.directories is None:
        eff_dirs = parent.directories
    else:
        eff_dirs = _union(
            _coerce_unmanaged_directories_for_union(parent.directories),
            child.directories,
        )

    eff_action = eff_action_raw if eff_action_raw is not None else "ignore"
    eff_dirs_out: tuple[str, ...] = () if eff_dirs is None else eff_dirs

    return UnmanagedFilesPolicy(action=eff_action, directories=eff_dirs_out)


def _merge_security(parent: SecurityPolicy, child: SecurityPolicy) -> SecurityPolicy:
    """Merge the security section: audit tightens but never relaxes.

    ``on_install`` escalates on the off < warn < block ladder with
    None-transparency (``None`` means "no opinion"; the other side flows
    through).  ``external`` (required scanners) is union-merged like other
    require lists -- a child can add scanners but not drop a parent's.
    """
    p, c = parent.audit, child.audit
    if p.on_install is None:
        on_install = c.on_install
    elif c.on_install is None:
        on_install = p.on_install
    else:
        on_install = _escalate(_AUDIT_ON_INSTALL_LEVELS, p.on_install, c.on_install)
    return SecurityPolicy(
        audit=AuditPolicy(
            on_install=on_install,
            external=_merge_list_field(p.external, c.external),
            scanners=_merge_scanners(p.scanners, c.scanners),
        ),
    )


def _merge_scanners(
    parent: tuple[tuple[str, ScannerGovernance], ...] | None,
    child: tuple[tuple[str, ScannerGovernance], ...] | None,
) -> tuple[tuple[str, ScannerGovernance], ...] | None:
    """Merge per-scanner governance: union of names; ``allow_args`` AND-merged.

    Restrict-only -- any ancestor forbidding args (``allow_args=False``) wins,
    so a child can tighten but never relax a parent's kill-switch. ``None``
    means "no opinion" and flows through transparently.
    """
    if parent is None and child is None:
        return None
    p_map = dict(parent or ())
    c_map = dict(child or ())
    merged: list[tuple[str, ScannerGovernance]] = []
    seen: set[str] = set()
    for name in list(p_map) + [n for n in c_map if n not in p_map]:
        if name in seen:
            continue
        seen.add(name)
        merged.append((name, _merge_governance(p_map.get(name), c_map.get(name))))
    return tuple(merged)


def _merge_governance(
    parent: ScannerGovernance | None,
    child: ScannerGovernance | None,
) -> ScannerGovernance:
    """AND-merge two governance blocks (``allow_args=False`` always wins).

    ``None`` is "no opinion" and flows through transparently; ``False``
    (forbid args) beats ``True`` so an org floor can never be relaxed.
    """
    p_allow = parent.allow_args if parent is not None else None
    c_allow = child.allow_args if child is not None else None
    if p_allow is False or c_allow is False:
        allow_args: bool | None = False
    elif p_allow is None and c_allow is None:
        allow_args = None
    else:
        allow_args = True
    return ScannerGovernance(allow_args=allow_args)


# ---------------------------------------------------------------------------
# List helpers
# ---------------------------------------------------------------------------


def _merge_list_field(
    parent: tuple[str, ...] | None,
    child: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """Merge a deny/require list field with None-transparency and union.

    * ``child is None``  -- no opinion; parent flows through (transparent).
    * ``child`` is empty -- explicit empty override; clears parent entries,
      returning ``()``.  Child can use ``[]`` in YAML to clear an inherited
      deny/require list.
    * both truthy        -- union; child entries are added to parent entries
      (deduped, parent order preserved).

    Always returns a ``tuple`` or ``None``; never a bare list.
    """
    if child is None:
        # Transparent: parent flows through.  Normalise to tuple if non-None
        # so callers always receive a uniform type regardless of how parent
        # was constructed in tests (list vs tuple).
        return _union((), parent) if parent is not None else None
    if not child:
        return ()  # explicit empty: override parent
    if parent is None or not parent:
        return _union((), child)  # parent has nothing; child wins
    return _union(parent, child)  # both have values: union


def _union(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    """Deduplicated union preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in (*a, *b):
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _intersect_allow(
    parent: tuple[str, ...] | None,
    child: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """Intersect two allow-lists (tighten-only).

    * ``None`` means "no opinion" (transparent in merge).
    * ``()`` means "explicitly allow nothing".
    * ``(...)`` means "allow only matching patterns".

    Rules:
    * Parent ``None`` -> child decides.
    * Child ``None`` -> parent decides.
    * Both non-None -> intersection (order follows parent).
    """
    if parent is None:
        return child
    if child is None:
        return parent
    child_set = set(child)
    return tuple(item for item in parent if item in child_set)


def _extract_ct_allow(content_types: dict | None) -> tuple[str, ...] | None:
    """Extract allow list from a content_types dict, preserving None semantics."""
    if content_types is None:
        return None
    allow_val = content_types.get("allow")
    if allow_val is None:
        return None
    if isinstance(allow_val, (list, tuple)):
        return tuple(allow_val)
    return None
