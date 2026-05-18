"""APM dependency resolution engine with recursive resolution and conflict detection."""

import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..models.apm_package import APMPackage, DependencyReference
from .dependency_graph import (
    CircularRef,
    DependencyNode,
    DependencyTree,
    FlatDependencyMap,
)

_logger = logging.getLogger(__name__)
_DEFAULT_RESOLVE_PARALLEL = 4


def _build_phase_a_work_items(
    tree: "DependencyTree",
    level_items: list,
    queued_keys: set,
    max_depth: int,
) -> list:
    """Dedup, node-creation, and depth-filter for one BFS level (Phase A).

    Extracted from :func:`build_dependency_tree` to reduce its McCabe
    complexity within the configured Ruff thresholds.
    """
    work_items: list[tuple[DependencyNode, DependencyReference, DependencyNode | None, bool]]
    work_items = []
    for dep_ref, depth, parent_node, is_dev in level_items:
        # Remove from queued set since we're now processing this dependency
        queued_keys.discard(dep_ref.get_unique_key())

        # Check maximum depth to prevent infinite recursion
        if depth > max_depth:
            continue

        # Check if we already processed this dependency at this level or higher
        existing_node = tree.get_node(dep_ref.get_unique_key())
        if existing_node and existing_node.depth <= depth:
            # Prod wins over dev: if existing was dev and this is prod, promote it
            if existing_node.is_dev and not is_dev:
                existing_node.is_dev = False
            # We've already processed this dependency at a shallower or equal depth
            # Create parent-child relationship if parent exists
            if parent_node and existing_node not in parent_node.children:
                parent_node.children.append(existing_node)
            continue

        # Create a new node for this dependency
        # Note: In a real implementation, we would load the actual package here
        # For now, create a placeholder package
        placeholder_package = APMPackage(
            name=dep_ref.get_display_name(), version="unknown", source=dep_ref.repo_url
        )

        node = DependencyNode(
            package=placeholder_package,
            dependency_ref=dep_ref,
            depth=depth,
            parent=parent_node,
            is_dev=is_dev,
        )

        # Add to tree
        tree.add_node(node)

        # Create parent-child relationship
        if parent_node:
            parent_node.children.append(node)

        work_items.append((node, dep_ref, parent_node, is_dev))
    return work_items


def _integrate_phase_c_results(
    results: list,
    processing_queue: "deque",
    queued_keys: set,
    expand_parent_repo_decl: "object",
) -> None:
    """Integrate worker results and enqueue sub-dependencies (Phase C).

    Extracted from :func:`build_dependency_tree` to reduce its McCabe
    complexity within the configured Ruff thresholds.
    """
    for (node, dep_ref, _parent_node, is_dev), loaded_package, exc in results:
        if exc is not None:
            if isinstance(exc, ValueError):
                _logger.warning(
                    "Invalid transitive apm.yml for %s: %s",
                    dep_ref.get_display_name(),
                    exc,
                )
            else:
                _logger.debug(
                    "Could not load transitive apm.yml for %s: %s",
                    dep_ref.get_display_name(),
                    exc,
                )
            continue
        if loaded_package:
            # Update the node with the actual loaded package
            node.package = loaded_package

            # Get sub-dependencies and add them to the processing queue
            # Transitive deps inherit is_dev from parent. Iteration
            # order matches the manifest's declaration order, which
            # ``loaded_package.get_apm_dependencies()`` preserves.
            sub_dependencies = loaded_package.get_apm_dependencies()
            for sub_dep in sub_dependencies:
                if sub_dep.is_parent_repo_inheritance:
                    sub_dep = expand_parent_repo_decl(node.dependency_ref, sub_dep)
                # Avoid infinite recursion by checking if we're already processing this dep
                # Use O(1) set lookup instead of O(n) list comprehension
                if sub_dep.get_unique_key() not in queued_keys:
                    processing_queue.append((sub_dep, node.depth + 1, node, is_dev))
                    queued_keys.add(sub_dep.get_unique_key())


def _enqueue_root_deps(
    root_package: APMPackage,
    processing_queue: deque,
    queued_keys: set,
) -> None:
    """Queue root-level prod and dev dependencies for BFS processing.

    Validates that no root dependency uses the ``git: parent`` inheritance
    form (which is only valid for transitive dependencies), then appends
    each dependency to *processing_queue* and records its key in
    *queued_keys*.  Prod entries are added first; if a dep appears in both
    prod and dev, the prod entry wins and the dev entry is skipped.

    Raises:
        ValueError: If any root dependency uses ``git: parent``.
    """
    for dep_ref in root_package.get_apm_dependencies():
        if dep_ref.is_parent_repo_inheritance:
            raise ValueError(
                "git: parent cannot be used in the root apm.yml manifest; "
                "specify an explicit repository URL. "
                "The git: parent form is only valid for transitive dependencies."
            )
        processing_queue.append((dep_ref, 1, None, False))
        queued_keys.add(dep_ref.get_unique_key())

    for dep_ref in root_package.get_dev_apm_dependencies():
        if dep_ref.is_parent_repo_inheritance:
            raise ValueError(
                "git: parent cannot be used in the root apm.yml manifest; "
                "specify an explicit repository URL. "
                "The git: parent form is only valid for transitive dependencies."
            )
        key = dep_ref.get_unique_key()
        if key not in queued_keys:
            processing_queue.append((dep_ref, 1, None, True))
            queued_keys.add(key)
        # If already queued as prod, prod wins — skip


def build_dependency_tree(self, root_apm_yml: Path) -> DependencyTree:
    """
    Build complete tree of all dependencies and sub-dependencies.

    Uses breadth-first traversal to build the dependency tree level by level.
    This allows for early conflict detection and clearer error reporting.

    Args:
        root_apm_yml: Path to the root apm.yml file

    Returns:
        DependencyTree: Hierarchical dependency tree
    """
    # Load root package. Anchor source_path on the project root so direct
    # dep relative paths resolve from there (#857).
    try:
        root_package = APMPackage.from_apm_yml(
            root_apm_yml,
            source_path=self._project_root.resolve()
            if self._project_root is not None
            else root_apm_yml.parent.resolve(),
        )
    except (ValueError, FileNotFoundError) as e:
        _logger.warning("Failed to parse root apm.yml: %s", e)
        empty_package = APMPackage(name="error", version="0.0.0")
        tree = DependencyTree(root_package=empty_package)
        return tree

    # Initialize the tree
    tree = DependencyTree(root_package=root_package)

    # Queue for breadth-first traversal: (dependency_ref, depth, parent_node, is_dev)
    processing_queue: deque[tuple[DependencyReference, int, DependencyNode | None, bool]] = deque()

    # Set to track queued unique keys for O(1) lookup instead of O(n) list comprehension
    queued_keys: set[str] = set()

    # Add root dependencies to queue
    _enqueue_root_deps(root_package, processing_queue, queued_keys)

    # Process dependencies breadth-first with level-batched parallelism.
    #
    # Parallel BFS is the CENTRAL resolution strategy (uv-inspired).
    # Each level fans out potentially I/O-bound
    # ``_try_load_dependency_package`` calls across a bounded worker
    # pool. All tree mutations -- ``tree.add_node``,
    # ``parent_node.children.append``, ``processing_queue.append``,
    # ``queued_keys`` writes -- still happen on the main thread, in
    # deterministic submission order, so parallelism never affects
    # the resolved tree shape.
    #
    # The ``max_parallel == 1`` branch exists SOLELY as a parity-
    # testing escape hatch (verifies sequential-identical output);
    # it is not a user-facing toggle.
    while processing_queue:
        # --- Drain one level ---
        current_depth = processing_queue[0][1]
        level_items: list[tuple[DependencyReference, int, DependencyNode | None, bool]] = []
        while processing_queue and processing_queue[0][1] == current_depth:
            level_items.append(processing_queue.popleft())

        # --- Phase A (main thread): dedup + node creation ---
        work_items = _build_phase_a_work_items(tree, level_items, queued_keys, self.max_depth)

        # --- Phase B (workers): load packages ---
        if not work_items:
            results: list[
                tuple[
                    tuple[DependencyNode, DependencyReference, DependencyNode | None, bool],
                    APMPackage | None,
                    Exception | None,
                ]
            ] = []
        elif self._max_parallel == 1 or len(work_items) == 1:
            # Parity-testing path: byte-identical to legacy sequential
            # output so ``APM_RESOLVE_PARALLEL=1`` can be used to
            # diff-debug ordering issues.  NOT a feature flag.
            results = [self._load_work_item(it) for it in work_items]
        else:
            workers = min(self._max_parallel, len(work_items))
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="apm-resolve"
            ) as executor:
                # ``executor.map`` preserves submission order, which
                # keeps next-level enqueuing deterministic regardless
                # of which worker finishes first.
                results = list(executor.map(self._load_work_item, work_items))

        # --- Phase C (main thread): integrate results, enqueue sub-deps ---
        _integrate_phase_c_results(
            results, processing_queue, queued_keys, self.expand_parent_repo_decl
        )

    return tree


def detect_circular_dependencies(self, tree: DependencyTree) -> list[CircularRef]:
    """
    Detect and report circular dependency chains.

    Uses depth-first search to detect cycles in the dependency graph.
    A cycle is detected when we encounter the same repository URL
    in our current traversal path.

    Args:
        tree: The dependency tree to analyze

    Returns:
        List[CircularRef]: List of detected circular dependencies
    """
    circular_deps = []
    visited: set[str] = set()
    current_path: list[str] = []
    current_path_set: set[str] = set()  # O(1) membership test (#171)

    def dfs_detect_cycles(node: DependencyNode) -> None:
        """Recursive DFS function to detect cycles."""
        node_id = node.get_id()
        # Use unique key (includes subdirectory path) to distinguish monorepo packages
        # e.g., vineethsoma/agent-packages/agents/X vs vineethsoma/agent-packages/skills/Y
        unique_key = node.dependency_ref.get_unique_key()

        # Check if this unique key is already in our current path (cycle detected)
        if unique_key in current_path_set:
            # Found a cycle - create the cycle path
            cycle_start_index = current_path.index(unique_key)
            cycle_path = current_path[cycle_start_index:] + [unique_key]  # noqa: RUF005

            circular_ref = CircularRef(cycle_path=cycle_path, detected_at_depth=node.depth)
            circular_deps.append(circular_ref)
            return

        # Mark current node as visited and add unique key to path
        visited.add(node_id)
        current_path.append(unique_key)
        current_path_set.add(unique_key)

        # Check all children
        for child in node.children:
            child_id = child.get_id()

            # Only recurse if we haven't processed this subtree completely
            if child_id not in visited or child.dependency_ref.get_unique_key() in current_path_set:
                dfs_detect_cycles(child)

        # Remove from path when backtracking (but keep in visited)
        current_path_set.discard(current_path.pop())

    # Start DFS from all root level dependencies (depth 1)
    root_deps = tree.get_nodes_at_depth(1)
    for root_dep in root_deps:
        if root_dep.get_id() not in visited:
            current_path = []  # Reset path for each root
            current_path_set = set()
            dfs_detect_cycles(root_dep)

    return circular_deps


def flatten_dependencies(self, tree: DependencyTree) -> FlatDependencyMap:
    """
    Flatten tree to avoid duplicate installations (NPM hoisting).

    Implements "first wins" conflict resolution strategy where the first
    declared dependency takes precedence over later conflicting dependencies.

    Args:
        tree: The dependency tree to flatten

    Returns:
        FlatDependencyMap: Flattened dependencies ready for installation
    """
    flat_map = FlatDependencyMap()
    seen_keys: set[str] = set()

    # Process dependencies level by level (breadth-first)
    # This ensures that dependencies declared earlier in the tree get priority
    for depth in range(1, tree.max_depth + 1):
        nodes_at_depth = tree.get_nodes_at_depth(depth)

        # Sort nodes by their position in the tree to ensure deterministic ordering
        # In a real implementation, this would be based on declaration order
        nodes_at_depth.sort(key=lambda node: node.get_id())

        for node in nodes_at_depth:
            unique_key = node.dependency_ref.get_unique_key()

            if unique_key not in seen_keys:
                # First occurrence - add without conflict
                flat_map.add_dependency(node.dependency_ref, is_conflict=False)
                seen_keys.add(unique_key)
            else:
                # Conflict - record it but keep the first one
                flat_map.add_dependency(node.dependency_ref, is_conflict=True)

    return flat_map
