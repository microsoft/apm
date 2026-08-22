"""Helpers for deriving scoped package install selections."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.command_logger import _ValidationOutcome
    from apm_cli.models.dependency import DependencyReference


def only_packages_from_validation(
    packages: tuple[str, ...] | None,
    outcome: _ValidationOutcome | None,
) -> list[str] | None:
    """Return canonical package specs for a positional install request."""
    if not packages:
        return None
    if outcome is None:
        return []
    seen = set()
    selected = []
    for canonical, _already_present in outcome.valid:
        if canonical not in seen:
            seen.add(canonical)
            selected.append(canonical)
    return selected


def cli_agent_subset_dep_keys(
    direct_dependencies: list[DependencyReference],
    only_packages: list[str] | None,
    *,
    agent_subset_from_cli: bool,
) -> set[str]:
    """Return direct dependency keys targeted by this invocation's ``--agent``."""
    if not agent_subset_from_cli or not only_packages:
        return set()

    from apm_cli.models.dependency import DependencyReference

    selected_identities: set[str] = set()
    for package in only_packages:
        try:
            selected_identities.add(DependencyReference.parse(package).get_identity())
        except Exception:
            selected_identities.add(package)

    return {
        dependency.get_unique_key()
        for dependency in direct_dependencies
        if dependency.get_identity() in selected_identities
    }
