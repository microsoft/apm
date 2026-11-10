"""Helpers for deriving scoped package install selections."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from apm_cli.models.dependency.reference import DependencyReference

if TYPE_CHECKING:
    from apm_cli.core.command_logger import _ValidationOutcome
    from apm_cli.deps.dependency_graph import DependencyNode, DependencyTree


def _selected_dependency_reference(
    package: str, dependencies: Sequence[DependencyReference]
) -> DependencyReference:
    """Preserve interpreted coordinates before falling back to shorthand parsing."""
    for dependency in dependencies:
        # Explicit git+path coordinates can contain dotted bundle directories
        # whose canonical display is not a valid shorthand file reference.
        if dependency.to_canonical() == package:
            return dependency
    return DependencyReference.parse(package)


def selected_dependency_identity(package: str, dependencies: Sequence[DependencyReference]) -> str:
    """Resolve a selector using interpreted references before shorthand parsing."""
    return _selected_dependency_reference(package, dependencies).get_identity()


def selected_subtree_dependencies(
    packages: Sequence[str],
    tree: DependencyTree,
    dependencies: Sequence[DependencyReference],
) -> list[DependencyReference]:
    """Filter the complete graph's winners to selected roots and their descendants."""
    resolved_refs = [node.dependency_ref for node in tree.nodes.values()]
    only_identities: set[str] = set()
    for package in packages:
        try:
            only_identities.add(selected_dependency_identity(package, resolved_refs))
        except (ValueError, TypeError):
            only_identities.add(package)

    expanded_nodes: set[int] = set()

    def collect_descendants(node: DependencyNode) -> None:
        if id(node) in expanded_nodes:
            return
        expanded_nodes.add(id(node))
        for child in node.children:
            identity = child.dependency_ref.get_identity()
            only_identities.add(identity)
            collect_descendants(child)

    for node in tree.nodes.values():
        if node.dependency_ref.get_identity() in only_identities:
            collect_descendants(node)
    return [dep for dep in dependencies if dep.get_identity() in only_identities]


def existing_dependency_identities(current_dependencies: list[object]) -> set[str]:
    """Return canonical identities for every parseable manifest dependency."""
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


def selected_dependency_keys(
    packages: list[str], dependencies: Sequence[DependencyReference] = ()
) -> set[str]:
    """Use the shared interpreted selector owner, retaining raw selectors on failure."""
    keys: set[str] = set()
    for spec in packages:
        try:
            keys.add(_selected_dependency_reference(spec, dependencies).get_unique_key())
        except (ValueError, TypeError):
            keys.add(spec)
    return keys
