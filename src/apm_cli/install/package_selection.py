"""Helpers for deriving scoped package install selections."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING

from apm_cli.models.dependency.reference import DependencyReference

if TYPE_CHECKING:
    from apm_cli.core.command_logger import _ValidationOutcome
    from apm_cli.deps.dependency_graph import DependencyNode, DependencyTree
    from apm_cli.deps.lockfile import LockFile
    from apm_cli.install.context import InstallContext


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


def _subtree_dependencies(
    root_identities: set[str],
    tree: DependencyTree,
    dependencies: Sequence[DependencyReference],
) -> list[DependencyReference]:
    """Return *dependencies* reached from the root identities through *tree*."""
    only_identities = set(root_identities)
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
    return _subtree_dependencies(only_identities, tree, dependencies)


def unlocked_local_dependencies(
    tree: DependencyTree,
    dependencies: Sequence[DependencyReference],
    locked_keys: Collection[str],
) -> list[DependencyReference]:
    """Return direct local-path dependencies no lock row records, with their subtrees.

    A partial install (``apm install <pkg>``) installs these alongside the
    selection: their files already live in the project, so the lock it writes
    must claim them (#4198). The selection itself, and its preview, is unchanged.
    """
    roots = {
        node.dependency_ref.get_identity()
        for node in tree.nodes.values()
        if node.dependency_ref.is_local
        and node.depth == 1
        and node.dependency_ref.get_unique_key() not in locked_keys
    }
    return _subtree_dependencies(roots, tree, dependencies) if roots else []


def complete_partial_selection(
    selected: Sequence[DependencyReference],
    complete: Sequence[DependencyReference],
    tree: DependencyTree,
    lockfile: LockFile | None,
) -> tuple[list[DependencyReference], frozenset[str]]:
    """Add declared local deps the lock lacks to a partial selection, in graph order (#4198).

    Returns the completed selection and the unique keys added implicitly. The
    install pipeline and its dry-run preview both call this, so the preview
    names exactly what a real partial install locks.
    """
    locked = lockfile.dependencies if lockfile is not None else {}
    keep = {dep.get_identity() for dep in selected}
    implicit = [
        dep
        for dep in unlocked_local_dependencies(tree, complete, locked)
        if dep.get_identity() not in keep
    ]
    keep |= {dep.get_identity() for dep in implicit}
    completed = [dep for dep in complete if dep.get_identity() in keep]
    return completed, frozenset(dep.get_unique_key() for dep in implicit)


def with_unlocked_local_dependencies(
    ctx: InstallContext, complete: Sequence[DependencyReference]
) -> list[DependencyReference]:
    """Complete the pipeline's partial selection and record the implicit locals (#4198)."""
    completed, implicit_keys = complete_partial_selection(
        ctx.deps_to_install, complete, ctx.dependency_graph.dependency_tree, ctx.existing_lockfile
    )
    # The CLI --skill overlay targets the named packages, never these (#4198).
    ctx.implicit_local_keys = implicit_keys
    return completed


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


def selected_removal_roots(
    packages: Sequence[str],
    declarations: Sequence[DependencyReference],
    lockfile: LockFile,
) -> list[object]:
    """Return the removal roots a targeted install's selectors name.

    Children record their parent's spelling at lock time; scan from both the
    interpreted declaration and the selected package's own lock entry so a
    canonical selector still reaches entries recorded under declared casing.
    """
    roots: list[object] = []
    for package in packages:
        try:
            selected = _selected_dependency_reference(package, declarations)
        except (ValueError, TypeError):
            roots.append(package)
            continue
        roots.append(selected)
        locked = lockfile.dependencies.get(selected.get_unique_key())
        if locked is not None:
            roots.append(locked.to_dependency_ref())
    return roots


def previous_selected_subtree_keys(
    packages: Sequence[str],
    declarations: Sequence[DependencyReference],
    lockfile: LockFile,
    complete_dep_keys: Collection[str] | None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Split the selected packages' previously locked descendants for a partial install.

    Returns ``(dropped, rescued)`` as the orphan owner decides them:
    descendants the complete graph resolved this run no longer contains, and
    those it still reaches through another retained package.
    ``complete_dep_keys`` is ``None`` when resolution was incomplete.
    """
    from apm_cli.install.orphans import OrphanOperation, decide_orphan_removal

    decision = decide_orphan_removal(
        operation=OrphanOperation.TARGETED_INSTALL,
        roots=selected_removal_roots(packages, declarations, lockfile),
        candidate_universe=lockfile,
        installed_paths=None,
        resolved_keys=complete_dep_keys,
        intended=None,
        materialized=None,
        reachability=None,
        deployment_claims=None,
    )
    return decision.candidate_keys, decision.rescued_keys


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
