"""Consumer dependency target filtering for install integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..utils.diagnostics import DiagnosticCollector


def filter_targets_for_dependency(
    targets: Any,
    dep_target_subset: list[str] | None,
    diagnostics: DiagnosticCollector,
    package_name: str,
) -> tuple[Any, set[str], bool]:
    """Apply the consumer-manifest dependency target filter."""
    if not dep_target_subset:
        return targets, set(), False

    allowed_dep_targets = set(dep_target_subset)
    filtered_targets = [target for target in targets if target.name in allowed_dep_targets]
    if not filtered_targets:
        requested = ", ".join(sorted(allowed_dep_targets))
        active = ", ".join(sorted(target.name for target in targets))
        diagnostics.warn(
            f"per-dependency targets [{requested}] do not overlap active install targets; skipping",
            package=package_name,
            detail=f"active targets: [{active}]",
        )
    return filtered_targets, allowed_dep_targets, True
