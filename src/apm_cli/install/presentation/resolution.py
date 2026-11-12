"""Report resolved dependency sources through the install logger."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.deps.apm_resolver import APMDependencyResolver
    from apm_cli.deps.dependency_graph import DependencyTree
    from apm_cli.install.context import InstallContext
    from apm_cli.models.dependency.reference import DependencyReference


def report_resolved_dependencies(
    ctx: InstallContext,
    resolver: APMDependencyResolver,
    dependencies: list[DependencyReference],
    tree: DependencyTree,
) -> None:
    """Report the graph and sources, deferring dormant-rule warnings to the summary."""
    if ctx.logger is None:
        return
    direct_count = len(tree.get_nodes_at_depth(1))
    transitive_count = len(tree.nodes) - direct_count
    if transitive_count > 0:
        ctx.logger.verbose_detail(
            f"Resolved dependency tree: {direct_count} direct + "
            f"{transitive_count} transitive deps (max depth {tree.max_depth})"
        )
        for node in tree.nodes.values():
            if node.depth > 1:
                ctx.logger.verbose_detail(f"    {node.get_ancestor_chain()}")
    else:
        ctx.logger.verbose_detail(f"Resolved {direct_count} direct dependencies (no transitive)")
    if ctx.apm_package.replace_rules:
        for rule in resolver.replacement_index.unused_rules:
            selector = rule.selector
            name = selector.repo_url + (f"/{selector.subpath}" if selector.subpath else "")
            ctx.logger.diagnostics.warn(
                f"Unused replacement rule for {name}. "
                "Check the exact host and package path, or remove the rule from apm.yml."
            )
    for dependency in dependencies:
        if dependency.replacement is not None:
            ctx.logger.progress(
                f"Replacement: {dependency.ownership_reference.to_display_reference()} "
                f"-> {dependency.source_reference.to_display_reference()}"
            )
