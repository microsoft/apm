"""Unmanaged-files classification and projected-write judging (#4956)."""

from __future__ import annotations

from collections.abc import Iterable


def _unmanaged_deny_conflict(
    rel_path: str,
    dependency_deny: tuple[str, ...] | None,
    mcp_deny: tuple[str, ...] | None,
) -> str | None:
    """Return the deny pattern an unmanaged file conflicts with, or ``None``.

    Surfaces APM's OWN deny policy as a human-resolve conflict: the dependency
    side is defaults-inclusive (``dependencies.effective_deny``) and the MCP
    side is the raw ``mcp.deny`` -- mirroring the deny-list checks exactly.
    Routes through the same ``first_matching_pattern`` matcher the deny-list
    checks use -- never a second matcher.
    """
    from .matcher import first_matching_pattern

    name = rel_path.rsplit("/", 1)[-1]
    for patterns in (dependency_deny, mcp_deny):
        hit = first_matching_pattern(rel_path, patterns)
        if hit is None:
            # Fall back to the basename so a deny glob written against a bare
            # filename (e.g. 'mcp.json') still surfaces the conflict.
            hit = first_matching_pattern(name, patterns)
        if hit is not None:
            return hit
    return None


def _format_unmanaged_detail(
    rel_path: str,
    primitive_type: str | None,
    deny_hit: str | None,
) -> str:
    """Render one enriched, ASCII-only finding line for an unmanaged file."""
    label = f"{rel_path} [type: {primitive_type}]" if primitive_type else rel_path
    reasons = ["not tracked in apm.lock.yaml"]
    if deny_hit:
        reasons.append(f"matches deny rule ({deny_hit})")
    return f"{label} -- {'; '.join(reasons)}"


def _unclaimed_projected_writes(
    projected_writes: Iterable[str],
    dirs: Iterable[str],
    exclude: Iterable[str],
    deployed: set[str],
    deployed_prefixes: tuple[str, ...],
    flagged: set[str],
) -> list[str]:
    """Projected config writes in a governed directory that nothing claims or excludes."""
    from .matcher import first_matching_pattern

    governed = tuple(d.strip("/") for d in dirs)
    unclaimed = []
    for rel in sorted({path.rstrip("/") for path in projected_writes}):
        if rel in flagged or rel in deployed or rel.startswith(deployed_prefixes):
            continue
        if not any(rel == d or rel.startswith(d + "/") for d in governed):
            continue
        if first_matching_pattern(rel, exclude) is None:
            unclaimed.append(rel)
    return unclaimed
