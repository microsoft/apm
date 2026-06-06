"""Static helper functions extracted from :class:`APMDependencyResolver`.

Moved to this sibling module to keep :mod:`apm_resolver` under the
file-length guardrail.  All functions are pure (no I/O, no class state)
and are re-exported from :mod:`apm_resolver` via thin
``@staticmethod`` / instance-method stubs so existing callers --
including ``APMDependencyResolver._resolve_max_parallel(7)``-style
test assertions -- are unchanged.

Rule A: every public name here that was previously accessible as
``apm_cli.deps.apm_resolver.<name>`` is re-exported (redundant-alias
form) from :mod:`apm_resolver` to preserve patch targets.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

from ..models.apm_package import APMPackage, DependencyReference
from .dependency_graph import (
    CircularRef,
    DependencyGraph,
    DependencyNode,
    DependencyTree,
    FlatDependencyMap,
)

# Must match the constant defined in apm_resolver (same value, separated to
# avoid a circular import).
_DEFAULT_RESOLVE_PARALLEL = 4


# ---------------------------------------------------------------------------
# Parallel-worker helpers
# ---------------------------------------------------------------------------


def _resolve_max_parallel(explicit: int | None) -> int:
    """Compute effective worker count for level-batched parallel BFS.

    Parallel is the default and central execution model.  The override
    exists for parity testing (``APM_RESOLVE_PARALLEL=1``) and CI
    diagnostics, not as a user-facing knob.

    Order of precedence:
    1. Explicit ``max_parallel`` ctor arg.
    2. ``APM_RESOLVE_PARALLEL`` env var (diagnostic/parity knob).
    3. ``_DEFAULT_RESOLVE_PARALLEL``.

    Always coerced to ``>= 1`` so the executor never gets a zero or
    negative ``max_workers``.
    """
    import logging

    if explicit is not None:
        return max(1, int(explicit))
    env = os.environ.get("APM_RESOLVE_PARALLEL", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            logging.getLogger(__name__).debug("Ignoring invalid APM_RESOLVE_PARALLEL=%r", env)
    return _DEFAULT_RESOLVE_PARALLEL


def _signature_accepts_parent_pkg(callback) -> bool:
    """Return True if ``callback`` declares a ``parent_pkg`` parameter
    (or accepts ``**kwargs``).

    Falls back to False if the signature can't be introspected (e.g. C
    extensions, builtins). The conservative fallback is correct: if we
    don't know the callback's shape, assume the legacy 3-arg form so
    the resolver won't pass an extra positional/keyword that triggers
    TypeError and silently drops the dependency (#940 SR1).
    """
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "parent_pkg":
            return True
    return False


# ---------------------------------------------------------------------------
# Dependency-reference guards (no I/O)
# ---------------------------------------------------------------------------


def _remote_parent_eligible(parent_dep: DependencyReference) -> bool:
    """Return True if *parent_dep* can serve as the Git repo for ``git: parent`` expansion."""
    if parent_dep.is_azure_devops():
        return bool(parent_dep.ado_repo and parent_dep.repo_url.count("/") >= 2)
    return "/" in parent_dep.repo_url


def _expand_parent_repo_decl(
    parent_dep: DependencyReference,
    child_dep: DependencyReference,
) -> DependencyReference:
    """Expand ``{ git: parent, path: ... }`` using the declaring package's coordinates.

    The child keeps its ``virtual_path`` (monorepo subdirectory), ``alias``, and
    optional ``ref`` override; repository identity (host, ``repo_url``, ADO
    fields, etc.) is inherited from *parent_dep*.
    """
    from dataclasses import replace

    if not child_dep.is_parent_repo_inheritance:
        raise ValueError("expand_parent_repo_decl requires child_dep.is_parent_repo_inheritance")
    if parent_dep.is_local:
        raise ValueError("git: parent cannot inherit from a local path dependency")
    if parent_dep.repo_url.startswith("_local/"):
        raise ValueError("git: parent cannot inherit from a local path dependency")
    if not _remote_parent_eligible(parent_dep):
        raise ValueError("git: parent requires a remote Git parent package dependency")

    merged_ref = child_dep.reference if child_dep.reference is not None else parent_dep.reference

    return replace(
        child_dep,
        repo_url=parent_dep.repo_url,
        host=parent_dep.host,
        port=parent_dep.port,
        explicit_scheme=parent_dep.explicit_scheme,
        ado_organization=parent_dep.ado_organization,
        ado_project=parent_dep.ado_project,
        ado_repo=parent_dep.ado_repo,
        artifactory_prefix=parent_dep.artifactory_prefix,
        is_insecure=parent_dep.is_insecure,
        allow_insecure=parent_dep.allow_insecure,
        reference=merged_ref,
        is_virtual=True,
        is_parent_repo_inheritance=False,
        is_local=False,
        local_path=None,
    )


# ---------------------------------------------------------------------------
# Tree algorithms (pure graph operations -- no package loading)
# ---------------------------------------------------------------------------


def _detect_circular_deps(tree: DependencyTree) -> list[CircularRef]:
    """Detect and report circular dependency chains.

    Uses depth-first search to detect cycles in the dependency graph.
    A cycle is detected when we encounter the same repository URL
    in our current traversal path.

    Args:
        tree: The dependency tree to analyse.

    Returns:
        List[CircularRef]: List of detected circular dependencies.
    """
    circular_deps: list[CircularRef] = []
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
            current_path.clear()
            current_path_set.clear()
            dfs_detect_cycles(root_dep)

    return circular_deps


def _flatten_dependencies(tree: DependencyTree) -> FlatDependencyMap:
    """Flatten tree to avoid duplicate installations (NPM hoisting).

    Implements "first wins" conflict resolution strategy where the first
    declared dependency takes precedence over later conflicting dependencies.

    Args:
        tree: The dependency tree to flatten.

    Returns:
        FlatDependencyMap: Flattened dependencies ready for installation.
    """
    flat_map = FlatDependencyMap()
    seen_keys: set[str] = set()

    # Process dependencies level by level (breadth-first)
    # This ensures that dependencies declared earlier in the tree get priority
    for depth in range(1, tree.max_depth + 1):
        nodes_at_depth = tree.get_nodes_at_depth(depth)

        # Sort nodes by their position in the tree to ensure deterministic ordering
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


# ---------------------------------------------------------------------------
# Package-loading utilities
# ---------------------------------------------------------------------------


def _is_remote_parent(parent_pkg: APMPackage | None) -> bool:
    """Return True if *parent_pkg* is a REMOTE package (i.e. fetched via
    git URL or pinned by ref/path).

    Used to gate ``local_path`` deps: only the root project and other
    local packages may legitimately declare them. Remote packages
    declaring a local_path is a path-confusion vector.

    SECURITY NOTE: this is a heuristic on the ``source`` field. A
    sufficiently adversarial remote could spoof a local-looking source.
    The downstream containment check via ``ensure_path_within`` is the
    actual security boundary; this gate just produces the user-facing
    error early.
    """
    if parent_pkg is None or not parent_pkg.source:
        return False
    src = str(parent_pkg.source)
    # Local deps get ``source = "_local/<name>"`` (see DependencyReference
    # construction for is_local=True). Treat that prefix as definitively
    # local even though it contains a slash.
    if src.startswith("_local/"):
        return False
    # Remote sources look like URLs or owner/repo refs. Local sources
    # are filesystem paths the user typed in their apm.yml.
    return (
        src.startswith(("http://", "https://", "git@", "ssh://", "git+"))
        or "://" in src
        or (src.count("/") >= 1 and not src.startswith((".", "/", "~")))
    )


def _compute_dep_source_path(
    dep_ref: DependencyReference,
    parent_pkg: APMPackage | None,
    install_path: Path,
) -> Path:
    """Return the source-path anchor for a dependency.

    For LOCAL deps we return the *original* user source directory so that
    transitive ``../sibling`` references inside its apm.yml resolve as a
    developer reading the file expects (#857). For REMOTE deps we return
    the clone location under apm_modules.
    """
    if dep_ref.is_local and dep_ref.local_path:
        local = Path(dep_ref.local_path).expanduser()
        if not local.is_absolute() and parent_pkg is not None and parent_pkg.source_path:
            return (parent_pkg.source_path / local).resolve()
        return local.resolve()
    return install_path.resolve()


def _download_dedup_key(dep_ref: DependencyReference, parent_pkg: APMPackage | None) -> str:
    """Dedup key for the download cache.

    Includes the parent's source_path so two parents anchoring the same
    local dep at different absolute locations don't collide on the first
    one's resolved path. For non-local deps, the parent anchor doesn't
    affect resolution, so the bare unique key suffices.
    """
    base = dep_ref.get_unique_key()
    if dep_ref.is_local and parent_pkg is not None and parent_pkg.source_path:
        return f"{base}@{parent_pkg.source_path}"
    return base


def _effective_base_dir(parent_pkg: APMPackage | None, project_root: Path) -> Path:
    """Return the directory used to anchor relative ``local_path`` deps.

    For direct (root-declared) deps, this is the project root. For
    transitive deps, it is the declaring package's source_path so a
    ``../sibling`` written inside the original package directory means
    what the author meant (#857).
    """
    if parent_pkg is not None and parent_pkg.source_path is not None:
        return parent_pkg.source_path
    return project_root


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------


def _create_resolution_summary(graph: DependencyGraph) -> str:
    """Create a human-readable summary of the resolution results.

    Args:
        graph: The resolved dependency graph.

    Returns:
        str: Summary string.
    """
    summary = graph.get_summary()
    lines = [
        "Dependency Resolution Summary:",
        f"  Root package: {summary['root_package']}",
        f"  Total dependencies: {summary['total_dependencies']}",
        f"  Maximum depth: {summary['max_depth']}",
    ]

    if summary["has_conflicts"]:
        lines.append(f"  Conflicts detected: {summary['conflict_count']}")

    if summary["has_circular_dependencies"]:
        lines.append(f"  Circular dependencies: {summary['circular_count']}")

    if summary["has_errors"]:
        lines.append(f"  Resolution errors: {summary['error_count']}")

    lines.append(f"  Status: {'[+] Valid' if summary['is_valid'] else '[x] Invalid'}")

    return "\n".join(lines)


def _validate_dependency_reference(dep_ref: DependencyReference) -> bool:
    """Validate that *dep_ref* is well-formed (non-empty repo_url with a slash)."""
    if not dep_ref.repo_url:
        return False
    if "/" not in dep_ref.repo_url:  # noqa: SIM103
        return False
    return True
