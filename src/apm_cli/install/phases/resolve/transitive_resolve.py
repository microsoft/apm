"""Transitive dependency filtering helpers for resolve phase."""

from __future__ import annotations

import builtins

from apm_cli.models.apm_package import DependencyReference


def _collect_descendants(node, only_identities: set, visited: set | None = None) -> None:
    """Walk the tree and add every child identity."""
    if visited is None:
        visited = builtins.set()
    for child in node.children:
        identity = child.dependency_ref.get_identity()
        if identity in visited:
            continue
        visited.add(identity)
        only_identities.add(identity)
        _collect_descendants(child, only_identities, visited)


def _apply_only_filter(ctx, deps_to_install: list, dependency_graph) -> list:
    """Restrict deps to requested packages plus their transitive descendants."""
    if not ctx.only_packages:
        return deps_to_install
    only_identities = builtins.set()
    for package in ctx.only_packages:
        try:
            ref = DependencyReference.parse(package)
            only_identities.add(ref.get_identity())
        except Exception:
            only_identities.add(package)

    tree = dependency_graph.dependency_tree
    for node in tree.nodes.values():
        if node.dependency_ref.get_identity() in only_identities:
            _collect_descendants(node, only_identities)
    return [dep for dep in deps_to_install if dep.get_identity() in only_identities]
