"""Helpers for deriving scoped package install selections."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.command_logger import _ValidationOutcome


def existing_dependency_identities(current_dependencies: list[object]) -> set[str]:
    """Return canonical identities for every parseable manifest dependency."""
    from apm_cli.models.apm_package import DependencyReference

    identities: set[str] = set()
    for entry in current_dependencies:
        try:
            if isinstance(entry, str):
                reference = DependencyReference.parse(entry)
            elif isinstance(entry, dict):
                reference = DependencyReference.parse_from_dict(entry)
            else:
                continue
            identities.add(reference.get_identity())
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return identities


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
