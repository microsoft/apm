"""Consumer dependency target filtering for install integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apm_cli.core.apm_yml import parse_targets_field
from apm_cli.models.apm_package import canonical_package_targets

if TYPE_CHECKING:
    from apm_cli.core.command_logger import InstallLogger
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


def log_package_target_restriction(
    logger: InstallLogger | None,
    target_selection: EffectivePackageTargets,
) -> None:
    """Name declared and effective targets when a package narrows them."""
    if logger is None or not target_selection.package_restriction_active:
        return
    declared = (
        ", ".join(target_selection.package_declared_targets)
        if target_selection.package_declared_targets
        else "unrestricted"
    )
    effective = ", ".join(target.name for target in target_selection.targets) or "none"
    logger.verbose_detail(
        f"Package target restriction: [{declared}]; effective targets: [{effective}]"
    )


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


# A package declaring this target ships portable, cross-client skills.
CROSS_CLIENT_SKILLS_TARGET = "agent-skills"


def package_allows_target(target: TargetProfile, package_allowed: frozenset[str]) -> bool:
    """Return whether a package's declared targets admit *target*.

    A declaration of ``agent-skills`` describes cross-client skills, so it also
    admits every skills-only target (derived from the profile, never by name):
    only the skills primitive deploys there, which is exactly what the package
    declared.
    """
    if target.name in package_allowed:
        return True
    return CROSS_CLIENT_SKILLS_TARGET in package_allowed and target.skills_only


def _no_target_hint(declared: list[str], requested: str) -> str:
    """Actionable hint for a package left with no deployable requested target."""
    names = ", ".join(sorted(declared))
    hint = f"Install with --target {sorted(declared)[0]}, or ask the package author to add {requested} to its targets (declared: {names})."
    if CROSS_CLIENT_SKILLS_TARGET in declared:
        hint += f" Skills-only targets accept packages that declare {CROSS_CLIENT_SKILLS_TARGET}."
    return hint


def resolve_effective_package_targets(
    targets: list[TargetProfile],
    dep_target_subset: list[str] | None,
    package_source: object,
    diagnostics: DiagnosticCollector | None,
    package_name: str,
    explicit_targets: bool = False,
) -> EffectivePackageTargets:
    """Intersect active, consumer-authorized, and package-declared targets.

    Project-active targets are the maximum authority. Consumer dependency
    targets may narrow that set, and package targets may narrow it again.
    Package metadata never activates a target absent from either upstream set.
    An omitted declaration and the legacy ``all`` spelling add no restriction.

    When the package declaration leaves nothing to deploy to, the outcome
    depends on how the targets were chosen: an explicit ``--target`` request
    (*explicit_targets*) is an error, because the user asked for a deployment
    that cannot happen and a silent exit 0 hides it. Auto-detected or
    configured targets stay a warning, since the package legitimately serves
    only some of the project's tools.
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
        tuple(
            target for target in consumer_targets if package_allows_target(target, package_allowed)
        )
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
        if explicit_targets:
            diagnostics.error(
                f"Package declares targets [{requested}] but you requested [{authorized}]; "
                "nothing was deployed",
                package=package_name,
                detail=_no_target_hint(declared_targets, consumer_targets[0].name),
            )
        else:
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
