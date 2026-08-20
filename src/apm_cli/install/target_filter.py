"""Consumer dependency target filtering for install integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apm_cli.core.apm_yml import parse_targets_field
from apm_cli.models.apm_package import canonical_package_targets

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile

    from ..utils.diagnostics import DiagnosticCollector


@dataclass(frozen=True)
class EffectivePackageTargets:
    """One restriction-only projection of every target authorization input."""

    targets: tuple[TargetProfile, ...]
    excluded_targets: tuple[TargetProfile, ...]
    consumer_allowed_targets: frozenset[str]
    consumer_restriction_active: bool
    package_declared_targets: tuple[str, ...]
    package_allowed_targets: frozenset[str]
    package_restriction_active: bool


def filter_targets_for_dependency(
    targets: list[TargetProfile],
    dep_target_subset: list[str] | None,
    diagnostics: DiagnosticCollector,
    package_name: str,
) -> tuple[list[TargetProfile], set[str], bool]:
    """Apply the consumer-manifest dependency target filter."""
    if not dep_target_subset:
        return targets, set(), False

    allowed_dep_targets = set(dep_target_subset)
    filtered_targets = [target for target in targets if target.name in allowed_dep_targets]
    if not filtered_targets:
        requested = ", ".join(sorted(allowed_dep_targets))
        active = ", ".join(sorted(target.name for target in targets))
        diagnostics.warn(
            f"Per-dependency targets [{requested}] do not overlap active install targets; skipping",
            package=package_name,
            detail=f"active targets: [{active}]",
        )
    return filtered_targets, allowed_dep_targets, True


def resolve_effective_package_targets(
    targets: list[TargetProfile],
    dep_target_subset: list[str] | None,
    package_source: object,
    diagnostics: DiagnosticCollector | None,
    package_name: str,
) -> EffectivePackageTargets:
    """Intersect active, consumer-authorized, and package-declared targets.

    Project-active targets are the maximum authority. Consumer dependency
    targets may narrow that set, and package targets may narrow it again.
    Package metadata never activates a target absent from either upstream set.
    An omitted declaration and the legacy ``all`` spelling add no restriction.
    """
    active_targets = tuple(targets)
    if diagnostics is None:
        consumer_allowed = set(dep_target_subset or ())
        consumer_restriction_active = bool(dep_target_subset)
        consumer_targets = (
            tuple(target for target in active_targets if target.name in consumer_allowed)
            if consumer_restriction_active
            else active_targets
        )
    else:
        filtered, consumer_allowed, consumer_restriction_active = filter_targets_for_dependency(
            list(active_targets),
            dep_target_subset,
            diagnostics,
            package_name,
        )
        consumer_targets = tuple(filtered)

    package = getattr(package_source, "package", package_source)
    try:
        package_fields = vars(package)
    except TypeError:
        package_fields = {}
    raw_target = package_fields.get("target")
    raw_targets = package_fields.get("targets")
    if raw_target is not None and raw_targets is not None:
        parse_targets_field({"target": raw_target, "targets": raw_targets})
    declared_targets = canonical_package_targets(package)
    package_restriction_active = bool(declared_targets) and "all" not in declared_targets
    package_allowed = frozenset(declared_targets) if package_restriction_active else frozenset()
    effective_targets = (
        tuple(target for target in consumer_targets if target.name in package_allowed)
        if package_restriction_active
        else consumer_targets
    )

    if (
        diagnostics is not None
        and package_restriction_active
        and consumer_targets
        and not effective_targets
    ):
        requested = ", ".join(sorted(package_allowed))
        authorized = ", ".join(sorted(target.name for target in consumer_targets))
        diagnostics.warn(
            f"Package targets [{requested}] do not overlap authorized active targets; skipping",
            package=package_name,
            detail=(
                f"authorized targets: [{authorized}]; enable a declared target "
                "or choose a compatible dependency"
            ),
        )

    effective_names = {target.name for target in effective_targets}
    return EffectivePackageTargets(
        targets=effective_targets,
        excluded_targets=tuple(
            target for target in active_targets if target.name not in effective_names
        ),
        consumer_allowed_targets=frozenset(consumer_allowed),
        consumer_restriction_active=consumer_restriction_active,
        package_declared_targets=declared_targets,
        package_allowed_targets=package_allowed,
        package_restriction_active=package_restriction_active,
    )
