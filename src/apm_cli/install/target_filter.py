"""Consumer dependency target filtering for install integration."""

from __future__ import annotations

import builtins
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
    allowed_dep_targets = builtins.set(dep_target_subset or [])
    if not dep_target_subset:
        return targets, allowed_dep_targets, False

    filtered_targets = [target for target in targets if target.name in allowed_dep_targets]
    if not filtered_targets:
        diagnostics.warn(
            "per-dependency targets do not overlap active install targets; skipping",
            package=package_name,
        )
    return filtered_targets, allowed_dep_targets, True
