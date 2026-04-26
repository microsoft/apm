"""APM dependency resolution engine with recursive resolution and conflict detection."""

import inspect
from pathlib import Path
from typing import List, Set, Optional, Protocol, Tuple, runtime_checkable
from collections import deque

from ..models.apm_package import APMPackage, DependencyReference
from .dependency_graph import (
    DependencyGraph, DependencyTree, DependencyNode, FlatDependencyMap,
    CircularRef, ConflictInfo
)

# Type alias for the download callback.
# Takes (dep_ref, apm_modules_dir, parent_chain, parent_pkg) and returns the
# install path if successful. ``parent_chain`` is a human-readable breadcrumb
# string like "root-pkg > mid-pkg > this-pkg" showing the full dependency path
# including the current node, or just the node's display name for direct
# (depth-1) deps. ``parent_pkg`` is the APMPackage that declared this
# dependency (None for direct deps from the root); callers use its
# ``source_path`` to anchor relative ``local_path`` resolution (#857).
@runtime_checkable
class DownloadCallback(Protocol):
    def __call__(
        self,
        dep_ref: 'DependencyReference',
        apm_modules_dir: Path,
        parent_chain: str = "",
        parent_pkg: Optional['APMPackage'] = None,
    ) -> Optional[Path]: ...


class APMDependencyResolver:
    """Handles recursive APM dependency resolution similar to NPM."""
    
    def __init__(
        self, 
        max_depth: int = 50, 
        apm_modules_dir: Optional[Path] = None,
        download_callback: Optional[DownloadCallback] = None
    ):
        """Initialize the resolver with maximum recursion depth.
        
        Args:
            max_depth: Maximum depth for dependency resolution (default: 50)
            apm_modules_dir: Optional explicit apm_modules directory. If not provided,
                             will be determined from project_root during resolution.
            download_callback: Optional callback to download missing packages. If provided,
                               the resolver will attempt to fetch uninstalled transitive deps.
        """
        self.max_depth = max_depth
        self._apm_modules_dir: Optional[Path] = apm_modules_dir
        self._project_root: Optional[Path] = None
        self._download_callback = download_callback
        # Whether ``download_callback`` accepts ``parent_pkg`` (added in #857).
        # Detected once via signature inspection so legacy callbacks that
        # predate the field still work without raising a silent TypeError
        # that would mask the dependency.
        self._callback_accepts_parent_pkg: bool = (
            self._signature_accepts_parent_pkg(download_callback)
            if download_callback is not None
            else False
        )
        self._downloaded_packages: Set[str] = set()  # Track what we downloaded during this resolution

    @staticmethod
    def _signature_accepts_parent_pkg(callback) -> bool:
        """Return True if ``callback`` declares a ``parent_pkg`` parameter
        (or accepts ``**kwargs``). Falls back to True if the signature can't
        be introspected (e.g. C extensions) so the modern path is preferred.
        """
        try:
            sig = inspect.signature(callback)
        except (TypeError, ValueError):
            return True
        for param in sig.parameters.values():
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if param.name == "parent_pkg":
                return True
        return False
    
    def resolve_dependencies(self, project_root: Path) -> DependencyGraph:
        """
        Resolve all APM dependencies recursively.
        
        Args:
            project_root: Path to the project root containing apm.yml
            
        Returns:
            DependencyGraph: Complete resolved dependency graph
        """
        # Store project root for package loading
        self._project_root = project_root
        if self._apm_modules_dir is None:
            self._apm_modules_dir = project_root / "apm_modules"
        
        # Load the root package
        apm_yml_path = project_root / "apm.yml"
        if not apm_yml_path.exists():
            # Create empty dependency graph for projects without apm.yml
            empty_package = APMPackage(name="unknown", version="0.0.0", package_path=project_root)
            empty_tree = DependencyTree(root_package=empty_package)
            empty_flat = FlatDependencyMap()
            return DependencyGraph(
                root_package=empty_package,
                dependency_tree=empty_tree,
                flattened_dependencies=empty_flat
            )
        
        try:
            root_package = APMPackage.from_apm_yml(apm_yml_path)
            # Anchor for relative local_path dependencies declared at the root.
            root_package.source_path = project_root.resolve()
        except (ValueError, FileNotFoundError) as e:
            # Create error graph
            empty_package = APMPackage(name="error", version="0.0.0", package_path=project_root)
            empty_tree = DependencyTree(root_package=empty_package)
            empty_flat = FlatDependencyMap()
            graph = DependencyGraph(
                root_package=empty_package,
                dependency_tree=empty_tree,
                flattened_dependencies=empty_flat
            )
            graph.add_error(f"Failed to load root apm.yml: {e}")
            return graph
        
        # Build the complete dependency tree
        dependency_tree = self.build_dependency_tree(apm_yml_path)
        
        # Detect circular dependencies
        circular_deps = self.detect_circular_dependencies(dependency_tree)
        
        # Flatten dependencies for installation
        flattened_deps = self.flatten_dependencies(dependency_tree)
        
        # Create and return the complete graph
        graph = DependencyGraph(
            root_package=root_package,
            dependency_tree=dependency_tree,
            flattened_dependencies=flattened_deps,
            circular_dependencies=circular_deps
        )
        
        return graph
    
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
        # Load root package
        try:
            root_package = APMPackage.from_apm_yml(root_apm_yml)
        except (ValueError, FileNotFoundError) as e:
            # Return empty tree with error
            empty_package = APMPackage(name="error", version="0.0.0")
            tree = DependencyTree(root_package=empty_package)
            return tree
        
        # Initialize the tree
        tree = DependencyTree(root_package=root_package)
        
        # Queue for breadth-first traversal: (dependency_ref, depth, parent_node, is_dev)
        processing_queue: deque[Tuple[DependencyReference, int, Optional[DependencyNode], bool]] = deque()
        
        # Set to track queued unique keys for O(1) lookup instead of O(n) list comprehension
        queued_keys: Set[str] = set()
        
        # Add root dependencies to queue
        root_deps = root_package.get_apm_dependencies()
        for dep_ref in root_deps:
            processing_queue.append((dep_ref, 1, None, False))
            queued_keys.add(dep_ref.get_unique_key())

        # Add root devDependencies to queue (marked is_dev=True)
        root_dev_deps = root_package.get_dev_apm_dependencies()
        for dep_ref in root_dev_deps:
            key = dep_ref.get_unique_key()
            if key not in queued_keys:
                processing_queue.append((dep_ref, 1, None, True))
                queued_keys.add(key)
            # If already queued as prod, prod wins — skip
        
        # Process dependencies breadth-first
        while processing_queue:
            dep_ref, depth, parent_node, is_dev = processing_queue.popleft()
            
            # Remove from queued set since we're now processing this dependency
            queued_keys.discard(dep_ref.get_unique_key())
            
            # Check maximum depth to prevent infinite recursion
            if depth > self.max_depth:
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
                name=dep_ref.get_display_name(),
                version="unknown",
                source=dep_ref.repo_url
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
            
            # Try to load the dependency package and its dependencies
            # For Task 3, this focuses on the resolution algorithm structure
            # Package loading integration will be completed in Tasks 2 & 4
            try:
                # Compute breadcrumb chain from this node's ancestry so download
                # errors can report "root > mid > failing-dep" context.
                parent_chain = node.get_ancestor_chain()

                loaded_package = self._try_load_dependency_package(
                    dep_ref,
                    parent_chain=parent_chain,
                    parent_pkg=parent_node.package if parent_node else None,
                )
                if loaded_package:
                    # Update the node with the actual loaded package
                    node.package = loaded_package
                    
                    # Get sub-dependencies and add them to the processing queue
                    # Transitive deps inherit is_dev from parent
                    sub_dependencies = loaded_package.get_apm_dependencies()
                    for sub_dep in sub_dependencies:
                        # Avoid infinite recursion by checking if we're already processing this dep
                        # Use O(1) set lookup instead of O(n) list comprehension
                        if sub_dep.get_unique_key() not in queued_keys:
                            processing_queue.append((sub_dep, depth + 1, node, is_dev))
                            queued_keys.add(sub_dep.get_unique_key())
            except (ValueError, FileNotFoundError) as e:
                # Could not load dependency package - this is expected for remote dependencies
                # The node already has a placeholder package, so continue with that
                pass
        
        return tree
    
    def detect_circular_dependencies(self, tree: DependencyTree) -> List[CircularRef]:
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
        visited: Set[str] = set()
        current_path: List[str] = []
        current_path_set: Set[str] = set()  # O(1) membership test (#171)
        
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
                cycle_path = current_path[cycle_start_index:] + [unique_key]
                
                circular_ref = CircularRef(
                    cycle_path=cycle_path,
                    detected_at_depth=node.depth
                )
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
        seen_keys: Set[str] = set()
        
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
    
    def _validate_dependency_reference(self, dep_ref: DependencyReference) -> bool:
        """
        Validate that a dependency reference is well-formed.
        
        Args:
            dep_ref: The dependency reference to validate
            
        Returns:
            bool: True if valid, False otherwise
        """
        if not dep_ref.repo_url:
            return False
        
        # Basic validation - in real implementation would be more thorough
        if '/' not in dep_ref.repo_url:
            return False
        
        return True
    
    def _try_load_dependency_package(
        self,
        dep_ref: DependencyReference,
        parent_chain: str = "",
        parent_pkg: Optional[APMPackage] = None,
    ) -> Optional[APMPackage]:
        """
        Try to load a dependency package from apm_modules/.

        This method scans apm_modules/ to find installed packages and loads their
        apm.yml to enable transitive dependency resolution. If a package is not
        installed and a download_callback is available, it will attempt to fetch
        the package first.

        Args:
            dep_ref: Reference to the dependency to load
            parent_chain: Human-readable breadcrumb of the dependency path
                that led here (e.g. "root-pkg > mid-pkg").  Forwarded to the
                download callback for contextual error messages.
            parent_pkg: APMPackage that declared *dep_ref* (None for direct
                deps from the root project). Its ``source_path`` is forwarded
                to the download callback so relative ``local_path`` deps
                resolve against the directory containing the declaring
                apm.yml rather than the root consumer (#857).

        Returns:
            APMPackage: Loaded package if found, None otherwise

        Raises:
            ValueError: If package exists but has invalid format
            FileNotFoundError: If package cannot be found
        """
        if self._apm_modules_dir is None:
            return None

        # Get the canonical install path for this dependency
        install_path = dep_ref.get_install_path(self._apm_modules_dir)

        # If package doesn't exist locally, try to download it
        if not install_path.exists():
            if self._download_callback is not None:
                # For local deps, dedupe by the *resolved* on-disk path so that
                # the same literal (e.g. ``../common``) declared by two
                # different parents does not collapse onto a single graph
                # node when each parent's source dir actually points to a
                # different sibling (#857).
                unique_key = self._download_dedup_key(dep_ref, parent_pkg)
                # Avoid re-downloading the same package in a single resolution
                if unique_key not in self._downloaded_packages:
                    try:
                        if self._callback_accepts_parent_pkg:
                            downloaded_path = self._download_callback(
                                dep_ref,
                                self._apm_modules_dir,
                                parent_chain,
                                parent_pkg,
                            )
                        else:
                            # Legacy callback predating #857 -- no parent_pkg.
                            # Local deps from non-root parents will fall back
                            # to project_root resolution; this matches behavior
                            # before the fix, so no surprise regression.
                            downloaded_path = self._download_callback(
                                dep_ref,
                                self._apm_modules_dir,
                                parent_chain,
                            )
                        if downloaded_path and downloaded_path.exists():
                            self._downloaded_packages.add(unique_key)
                            install_path = downloaded_path
                    except Exception:
                        # Download failed - continue without this dependency's sub-deps
                        pass

            # Still doesn't exist after download attempt
            if not install_path.exists():
                return None

        # Anchor for resolving this package's own relative local_path deps:
        # the *original* source dir for local deps (not the apm_modules copy),
        # otherwise the install_path itself.
        resolved_source_path = self._compute_dep_source_path(
            dep_ref, install_path, parent_pkg
        )

        # Look for apm.yml in the install path
        apm_yml_path = install_path / "apm.yml"
        if not apm_yml_path.exists():
            # Package exists but has no apm.yml (e.g., Claude Skill)
            # Check for SKILL.md and create minimal package
            skill_md_path = install_path / "SKILL.md"
            if skill_md_path.exists():
                # Claude Skill without apm.yml - no transitive deps
                return APMPackage(
                    name=dep_ref.get_display_name(),
                    version="1.0.0",
                    source=dep_ref.repo_url,
                    package_path=install_path,
                    source_path=resolved_source_path,
                )
            # No manifest found
            return None

        # Load and return the package
        try:
            package = APMPackage.from_apm_yml(apm_yml_path)
            # Ensure source is set for tracking
            if not package.source:
                package.source = dep_ref.repo_url
            # Set source_path so transitive resolution of THIS package's
            # local-path deps anchors on the right directory.
            package.source_path = resolved_source_path
            return package
        except (ValueError, FileNotFoundError) as e:
            # Package has invalid apm.yml - log warning but continue
            # In production, we might want to surface this to the user
            return None

    def _download_dedup_key(
        self,
        dep_ref: DependencyReference,
        parent_pkg: Optional[APMPackage],
    ) -> str:
        """Return a context-aware key for the per-resolution download cache.

        For remote deps this is just ``dep_ref.get_unique_key()``. For local
        deps the literal ``local_path`` (e.g. ``../common``) is ambiguous
        once #857 anchors it on the declaring package's directory: the same
        string can refer to different on-disk packages depending on which
        parent declared it. We therefore prefix the key with the resolved
        absolute source path so the dedup matches the actual filesystem
        identity, not the textual identity.
        """
        if dep_ref.is_local and dep_ref.local_path:
            local = Path(dep_ref.local_path).expanduser()
            if local.is_absolute():
                resolved = local.resolve()
            else:
                base_dir = (
                    parent_pkg.source_path
                    if parent_pkg is not None and parent_pkg.source_path is not None
                    else self._project_root
                )
                resolved = (base_dir / local).resolve() if base_dir else local.resolve()
            return f"local::{resolved}"
        return dep_ref.get_unique_key()

    def _compute_dep_source_path(
        self,
        dep_ref: DependencyReference,
        install_path: Path,
        parent_pkg: Optional[APMPackage],
    ) -> Path:
        """Return the on-disk anchor for resolving *dep_ref*'s own local deps.

        For local dependencies this is the *original* directory the user
        referenced (so relative paths inside the package's apm.yml mean what
        a developer reading the file would expect). For remote deps it is
        ``install_path`` (the clone location), which is also where the
        package's apm.yml lives.
        """
        if dep_ref.is_local and dep_ref.local_path:
            local = Path(dep_ref.local_path).expanduser()
            if local.is_absolute():
                return local.resolve()
            base_dir = (
                parent_pkg.source_path
                if parent_pkg is not None and parent_pkg.source_path is not None
                else self._project_root
            )
            if base_dir is None:
                # Fallback: best-effort relative-to-cwd. Rare; only hit if
                # the resolver was driven without a project root.
                return local.resolve()
            return (base_dir / local).resolve()
        return install_path.resolve()
    
    def _create_resolution_summary(self, graph: DependencyGraph) -> str:
        """
        Create a human-readable summary of the resolution results.
        
        Args:
            graph: The resolved dependency graph
            
        Returns:
            str: Summary string
        """
        summary = graph.get_summary()
        lines = [
            f"Dependency Resolution Summary:",
            f"  Root package: {summary['root_package']}",
            f"  Total dependencies: {summary['total_dependencies']}",
            f"  Maximum depth: {summary['max_depth']}",
        ]
        
        if summary['has_conflicts']:
            lines.append(f"  Conflicts detected: {summary['conflict_count']}")
        
        if summary['has_circular_dependencies']:
            lines.append(f"  Circular dependencies: {summary['circular_count']}")
        
        if summary['has_errors']:
            lines.append(f"  Resolution errors: {summary['error_count']}")
        
        lines.append(f"  Status: {'[+] Valid' if summary['is_valid'] else '[x] Invalid'}")
        
        return "\n".join(lines)