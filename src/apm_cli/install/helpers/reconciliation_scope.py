"""Catalog-backed target scope for manifest reconciliation."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector


def _profiles_by_name(
    targets: Iterable[TargetProfile] | None,
) -> dict[str, TargetProfile]:
    """Return profiles keyed by catalog target name."""
    by_name: dict[str, TargetProfile] = {}
    for target in targets or ():
        name = getattr(target, "name", None)
        if isinstance(name, str) and name not in by_name:
            by_name[name] = target
    return by_name


def _is_manifest_target(name: str) -> bool:
    """Return whether *name* can be declared in ``apm.yml`` ``targets:``.

    Every other profile in the declared universe is supplemental, included
    only so its rows are not dropped as ghosts.
    """
    from apm_cli.core.apm_yml import CANONICAL_TARGETS

    return name in CANONICAL_TARGETS


def _has_gated_resolver(profile: TargetProfile) -> bool:
    """Return whether an inactive supplemental target must not resolve."""
    return profile.requires_flag is not None and profile.user_root_resolver is not None


def _record_inactive_resolver_skip(
    profile: TargetProfile,
    diagnostics: DiagnosticCollector | None,
) -> None:
    """Record the intentional skip of an inactive experimental resolver."""
    if diagnostics is None:
        return
    from apm_cli.utils.diagnostics import CATEGORY_INFO

    message = (
        f"Skipped inactive experimental resolver for target '{profile.name}' "
        "during lockfile reconciliation."
    )
    if any(
        diagnostic.message == message
        for diagnostic in diagnostics.by_category().get(CATEGORY_INFO, ())
    ):
        return
    flag = profile.requires_flag or profile.name
    diagnostics.info(
        message,
        detail=(
            f"To include it, enable '{flag.replace('_', '-')}' and select "
            f"target '{profile.name}' for this install."
        ),
    )


def _scoped_known_targets_for_reconciliation(
    *,
    user_scope: bool,
    active_targets: Iterable[TargetProfile],
    declared_targets: Iterable[TargetProfile] | None,
) -> dict[str, TargetProfile]:
    """Return known targets without running inactive experimental resolvers."""
    from apm_cli.integration.targets import KNOWN_TARGETS

    active_by_name = _profiles_by_name(active_targets)
    declared_by_name = _profiles_by_name(declared_targets)
    scoped_known_targets: dict[str, TargetProfile] = {}
    for name, profile in KNOWN_TARGETS.items():
        resolved = active_by_name.get(name) or declared_by_name.get(name)
        if resolved is not None:
            scoped_known_targets[name] = resolved
            continue
        if _has_gated_resolver(profile):
            scoped_known_targets[name] = profile
            continue
        scoped = profile.for_scope(user_scope=user_scope)
        if scoped is not None:
            scoped_known_targets[name] = scoped
    return scoped_known_targets
