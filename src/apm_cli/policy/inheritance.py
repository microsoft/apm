"""Policy inheritance: resolve and merge policy chains.

Supports three-level chains: enterprise hub -> org -> repo.
Each level can tighten but never relax the parent.

extends: values:
- "org"              -> same org's .github repo (repo-level override)
- "<owner>/<repo>"   -> cross-org reference (enterprise policy hub)
- "https://..."      -> direct URL
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple  # noqa: F401, UP035

from .schema import (
    ApmPolicy,
    CompilationPolicy,
    CompilationStrategyPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    ManifestPolicy,
    McpPolicy,
    McpTransportPolicy,
    PolicyCache,
    UnmanagedFilesPolicy,
)

MAX_CHAIN_DEPTH = 5

# Escalation ladders -- index = severity, higher is stricter.
_ENFORCEMENT_LEVELS = {"off": 0, "warn": 1, "block": 2}
_RESOLUTION_LEVELS = {"project-wins": 0, "policy-wins": 1, "block": 2}
_SELF_DEFINED_LEVELS = {"allow": 0, "warn": 1, "deny": 2}
_UNMANAGED_ACTION_LEVELS = {"ignore": 0, "warn": 1, "deny": 2}
_SCRIPTS_LEVELS = {"allow": 0, "deny": 1}


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
        cache=_merge_cache(parent.cache, child.cache),
        dependencies=_merge_dependencies(parent.dependencies, child.dependencies),
        mcp=_merge_mcp(parent.mcp, child.mcp),
        compilation=_merge_compilation(parent.compilation, child.compilation),
        manifest=_merge_manifest(parent.manifest, child.manifest),
        unmanaged_files=_merge_unmanaged_files(parent.unmanaged_files, child.unmanaged_files),
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


def _merge_cache(parent: PolicyCache, child: PolicyCache) -> PolicyCache:
    return PolicyCache(ttl=min(parent.ttl, child.ttl))


def _merge_dependencies(parent: DependencyPolicy, child: DependencyPolicy) -> DependencyPolicy:
    return DependencyPolicy(
        deny=_union(parent.deny, child.deny),
        allow=_intersect_allow(parent.allow, child.allow),
        require=_union(parent.require, child.require),
        require_resolution=_escalate(
            _RESOLUTION_LEVELS, parent.require_resolution, child.require_resolution
        ),
        max_depth=min(parent.max_depth, child.max_depth),
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


def _merge_unmanaged_files(
    parent: UnmanagedFilesPolicy, child: UnmanagedFilesPolicy
) -> UnmanagedFilesPolicy:
    return UnmanagedFilesPolicy(
        action=_escalate(_UNMANAGED_ACTION_LEVELS, parent.action, child.action),
        directories=_union(parent.directories, child.directories),
    )


# ---------------------------------------------------------------------------
# List helpers
# ---------------------------------------------------------------------------


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
