"""APM dependency resolution engine with recursive resolution and conflict detection."""

import inspect
import logging
import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Protocol, Set, Tuple  # noqa: F401, UP035

from ..models.apm_package import APMPackage, DependencyReference
from .dependency_graph import (
    CircularRef,
    ConflictInfo,  # noqa: F401
    DependencyGraph,
    DependencyNode,
    DependencyTree,
    FlatDependencyMap,
)

_logger = logging.getLogger(__name__)


# Default worker pool size for the level-batched BFS download phase.
# Parallel resolution is the CENTRAL execution model (uv-inspired);
# the ``APM_RESOLVE_PARALLEL`` env var exists solely as a diagnostic /
# parity-testing knob (e.g. ``APM_RESOLVE_PARALLEL=1 apm install`` to
# reproduce legacy sequential ordering for diff-debugging).  It is NOT
# a user-facing feature toggle.
_DEFAULT_RESOLVE_PARALLEL = 4


# Type alias for the download callback.
# Takes (dep_ref, apm_modules_dir, parent_chain, parent_pkg) and returns the
# install path if successful. ``parent_chain`` is a human-readable breadcrumb
# string like "root-pkg > mid-pkg > this-pkg" showing the full dependency
# path including the current node, or just the node's display name for
# direct (depth-1) deps. ``parent_pkg`` is the APMPackage that declared this
# dependency (None for direct deps from the root); callers use its
# ``source_path`` to anchor relative ``local_path`` resolution (#857).
#
# Note: NOT @runtime_checkable -- we use signature inspection
# (``_signature_accepts_parent_pkg``) to detect legacy callbacks, never
# isinstance, so the runtime-checkable overhead would be dead weight.
class DownloadCallback(Protocol):
    def __call__(
        self,
        dep_ref: "DependencyReference",
        apm_modules_dir: Path,
        parent_chain: str = "",
        parent_pkg: Optional["APMPackage"] = None,
    ) -> Path | None: ...


class APMDependencyResolver:
    """Handles recursive APM dependency resolution similar to NPM."""

    def __init__(
        self,
        max_depth: int = 50,
        apm_modules_dir: Path | None = None,
        download_callback: DownloadCallback | None = None,
        max_parallel: int | None = None,
    ):
        """Initialize the resolver with maximum recursion depth.

        Args:
            max_depth: Maximum depth for dependency resolution (default: 50)
            apm_modules_dir: Optional explicit apm_modules directory. If not provided,
                             will be determined from project_root during resolution.
            download_callback: Optional callback to download missing packages. If provided,
                               the resolver will attempt to fetch uninstalled transitive deps.
            max_parallel: Max worker threads for the level-batched
                parallel BFS download phase (the default execution
                model). ``None`` resolves from the
                ``APM_RESOLVE_PARALLEL`` env var, falling back to
                ``_DEFAULT_RESOLVE_PARALLEL`` (4). Set to ``1`` ONLY
                for parity-testing against the legacy sequential path
                -- this is a diagnostic knob, not a user toggle.
        """
        self.max_depth = max_depth
        self._apm_modules_dir: Path | None = apm_modules_dir
        self._project_root: Path | None = None
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
        self._downloaded_packages: set[str] = (
            set()
        )  # Track what we downloaded during this resolution
        # Tracks ``dep_ref.get_unique_key()`` values rejected by the
        # remote-parent local_path guard (#940 / PR #1111 review C2). The
        # resolve phase folds this into ``ctx.callback_failures`` so the
        # integrate phase skips them with the same "already failed during
        # resolution" path used for download failures -- otherwise the
        # rejected dep would still sit in the dependency tree and get
        # copied later via ``_copy_local_package``, defeating the
        # fail-closed posture this guard is meant to enforce.
        self._rejected_remote_local_keys: set[str] = set()
        # Protects mutations of ``_downloaded_packages`` and
        # ``_rejected_remote_local_keys`` when the parallel BFS
        # dispatches ``_try_load_dependency_package`` calls onto a
        # worker pool. The ``max_parallel=1`` parity path still
        # acquires the lock -- the overhead is negligible and the
        # symmetry simplifies reasoning.
        self._download_lock = threading.Lock()
        self._max_parallel = self._resolve_max_parallel(max_parallel)

    @staticmethod
    def _resolve_max_parallel(explicit: int | None) -> int:
        """Compute effective worker count for level-batched parallel BFS.

        Parallel is the default and central execution model.  The
        override exists for parity testing (``APM_RESOLVE_PARALLEL=1``)
        and CI diagnostics, not as a user-facing knob.

        Order of precedence:
        1. Explicit ``max_parallel`` ctor arg.
        2. ``APM_RESOLVE_PARALLEL`` env var (diagnostic/parity knob).
        3. ``_DEFAULT_RESOLVE_PARALLEL``.

        Always coerced to ``>= 1`` so the executor never gets a zero
        or negative ``max_workers``.
        """
        if explicit is not None:
            return max(1, int(explicit))
        env = os.environ.get("APM_RESOLVE_PARALLEL", "").strip()
        if env:
            try:
                return max(1, int(env))
            except ValueError:
                _logger.debug("Ignoring invalid APM_RESOLVE_PARALLEL=%r", env)
        return _DEFAULT_RESOLVE_PARALLEL

    @staticmethod
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
                flattened_dependencies=empty_flat,
            )

        try:
            root_package = APMPackage.from_apm_yml(apm_yml_path, source_path=project_root.resolve())
        except (ValueError, FileNotFoundError) as e:
            # Create error graph
            empty_package = APMPackage(name="error", version="0.0.0", package_path=project_root)
            empty_tree = DependencyTree(root_package=empty_package)
            empty_flat = FlatDependencyMap()
            graph = DependencyGraph(
                root_package=empty_package,
                dependency_tree=empty_tree,
                flattened_dependencies=empty_flat,
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
            circular_dependencies=circular_deps,
        )

        return graph

    def _remote_parent_eligible(self, parent_dep: DependencyReference) -> bool:
        """Return True if *parent_dep* can serve as the Git repo for ``git: parent`` expansion."""
        if parent_dep.is_azure_devops():
            return bool(parent_dep.ado_repo and parent_dep.repo_url.count("/") >= 2)
        return "/" in parent_dep.repo_url

    def expand_parent_repo_decl(
        self,
        parent_dep: DependencyReference,
        child_dep: DependencyReference,
    ) -> DependencyReference:
        """Expand ``{ git: parent, path: ... }`` using the declaring package's coordinates.

        The child keeps its ``virtual_path`` (monorepo subdirectory), ``alias``, and
        optional ``ref`` override; repository identity (host, ``repo_url``, ADO
        fields, etc.) is inherited from *parent_dep*.
        """
        if not child_dep.is_parent_repo_inheritance:
            raise ValueError(
                "expand_parent_repo_decl requires child_dep.is_parent_repo_inheritance"
            )
        if parent_dep.is_local:
            raise ValueError("git: parent cannot inherit from a local path dependency")
        if parent_dep.repo_url.startswith("_local/"):
            raise ValueError("git: parent cannot inherit from a local path dependency")
        if not self._remote_parent_eligible(parent_dep):
            raise ValueError("git: parent requires a remote Git parent package dependency")

        merged_ref = (
            child_dep.reference if child_dep.reference is not None else parent_dep.reference
        )

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
        processing_queue: deque[tuple[DependencyReference, int, DependencyNode | None, bool]] = (
            deque()
        )

        # Set to track queued unique keys for O(1) lookup instead of O(n) list comprehension
        queued_keys: set[str] = set()

        # Add root dependencies to queue
        root_deps = root_package.get_apm_dependencies()
        for dep_ref in root_deps:
            if dep_ref.is_parent_repo_inheritance:
                raise ValueError(
                    "git: parent cannot be used in the root apm.yml manifest; "
                    "specify an explicit repository URL. "
                    "The git: parent form is only valid for transitive dependencies."
                )
            processing_queue.append((dep_ref, 1, None, False))
            queued_keys.add(dep_ref.get_unique_key())

        # Add root devDependencies to queue (marked is_dev=True)
        root_dev_deps = root_package.get_dev_apm_dependencies()
        for dep_ref in root_dev_deps:
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
            # Each work_item is (node, dep_ref, parent_node, is_dev)
            # and represents a NEW node that needs its package loaded.
            # Items that hit the existing-node fast-path or exceed
            # ``max_depth`` are resolved here and never reach the worker
            # pool.
            work_items: list[
                tuple[DependencyNode, DependencyReference, DependencyNode | None, bool]
            ]
            work_items = []
            for dep_ref, depth, parent_node, is_dev in level_items:
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
                            sub_dep = self.expand_parent_repo_decl(node.dependency_ref, sub_dep)
                        # Avoid infinite recursion by checking if we're already processing this dep
                        # Use O(1) set lookup instead of O(n) list comprehension
                        if sub_dep.get_unique_key() not in queued_keys:
                            processing_queue.append((sub_dep, node.depth + 1, node, is_dev))
                            queued_keys.add(sub_dep.get_unique_key())

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
                if (
                    child_id not in visited
                    or child.dependency_ref.get_unique_key() in current_path_set
                ):
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
        if "/" not in dep_ref.repo_url:  # noqa: SIM103
            return False

        return True

    def _load_work_item(self, item):
        """Worker payload for the level-batched parallel BFS.

        Pure I/O wrapper around ``_try_load_dependency_package`` that
        returns ``(item, loaded_package_or_None, exception_or_None)``
        so the main thread can keep all tree mutations on its side.
        Defined as a method (not a closure inside the BFS while-loop)
        to satisfy ruff B023 -- no risk of accidentally capturing a
        loop-iteration variable.
        """
        node, dep_ref, parent_node, _is_dev = item
        # Compute breadcrumb chain from this node's ancestry so download
        # errors can report "root > mid > failing-dep" context.
        parent_chain = node.get_ancestor_chain()
        try:
            loaded = self._try_load_dependency_package(
                dep_ref,
                parent_chain=parent_chain,
                parent_pkg=parent_node.package if parent_node else None,
            )
            return (item, loaded, None)
        except (ValueError, FileNotFoundError) as exc:
            return (item, None, exc)

    def _try_load_dependency_package(
        self,
        dep_ref: DependencyReference,
        parent_chain: str = "",
        parent_pkg: APMPackage | None = None,
    ) -> APMPackage | None:
        """
        Try to load a dependency package from apm_modules/.

        This method scans apm_modules/ to find installed packages and loads their
        apm.yml to enable transitive dependency resolution. If a package is not
        installed and a download_callback is available, it will attempt to fetch
        the package first.

        Args:
            dep_ref: Reference to the dependency to load.
            parent_chain: Human-readable breadcrumb of the dependency path
                that led here (e.g. "root-pkg > mid-pkg").  Forwarded to the
                download callback for contextual error messages.
            parent_pkg: APMPackage that declared *dep_ref*, or None if this is
                a direct dep from the root project. Used to (a) anchor relative
                ``local_path`` resolution to the declaring package's source
                directory (#857) and (b) reject ``local_path`` deps declared
                inside REMOTE packages -- a remote package can't reasonably
                refer to a path on the consumer's filesystem (#940).

        Returns:
            APMPackage: Loaded package if found, None otherwise

        Raises:
            ValueError: If package exists but has invalid format
            FileNotFoundError: If package cannot be found
        """
        if self._apm_modules_dir is None:
            return None

        # Reject local_path deps declared by remote packages BEFORE asking the
        # download callback to materialize them. A remote package referencing
        # a local path on the consumer's filesystem is a path-confusion vector
        # whether the path is relative (resolves against the parent's
        # apm_modules clone) or absolute (presumes filesystem layout). Both
        # branches reject at ERROR severity so the operator sees red, not the
        # yellow of an advisory warning (#940 F3).
        if dep_ref.is_local and dep_ref.local_path and self._is_remote_parent(parent_pkg):
            local_str = str(dep_ref.local_path)
            try:
                from apm_cli.utils.console import _rich_error

                if Path(local_str).expanduser().is_absolute():
                    _rich_error(
                        f"Refusing to install local_path dependency '{local_str}' "
                        f"declared by remote package '{parent_pkg.name if parent_pkg else '?'}': "
                        "absolute paths inside remote packages are a security risk. "
                        "Publish the dependency as a standalone package and reference "
                        "it via owner/repo or marketplace handle."
                    )
                else:
                    _rich_error(
                        f"Refusing to install local_path dependency '{local_str}' "
                        f"declared by remote package '{parent_pkg.name if parent_pkg else '?'}': "
                        "remote packages cannot reference paths on the consumer "
                        "filesystem. Publish the dependency as a standalone package "
                        "and reference it via owner/repo or marketplace handle."
                    )
            except Exception:
                _logger.debug("Could not emit remote-parent rejection notice", exc_info=True)
            # Mark the dep as failed at resolve time so the integrate phase
            # skips it (PR #1111 review C2). Without this, the dep would
            # remain in the dep tree -> ``deps_to_install`` -> the integrate
            # loop would still call ``_copy_local_package`` and copy the
            # very path we just refused.
            with self._download_lock:
                self._rejected_remote_local_keys.add(dep_ref.get_unique_key())
            return None

        # Get the canonical install path for this dependency
        install_path = dep_ref.get_install_path(self._apm_modules_dir)

        # If package doesn't exist locally, try to download it
        if not install_path.exists():
            if self._download_callback is not None:
                unique_key = self._download_dedup_key(dep_ref, parent_pkg)
                # Avoid re-downloading the same logical (dep_ref, anchor) pair
                # in a single resolution. The anchor is part of the key so that
                # two parents with different ``source_path`` values can each
                # fetch / copy the same dep into their own slot if needed.
                #
                # F7 (#1116): atomically check-and-reserve under
                # ``_download_lock`` so two BFS workers racing on the
                # same logical dep can't both pass the gate and double-
                # fetch. The reserving worker fetches; later workers
                # observe the reservation and skip the callback.
                with self._download_lock:
                    should_fetch = unique_key not in self._downloaded_packages
                    if should_fetch:
                        # Reserve the slot before releasing the lock so a
                        # concurrent worker can't slip past the gate while
                        # we're inside the (potentially slow) callback.
                        self._downloaded_packages.add(unique_key)
                if should_fetch:
                    try:
                        if self._callback_accepts_parent_pkg:
                            downloaded_path = self._download_callback(
                                dep_ref,
                                self._apm_modules_dir,
                                parent_chain,
                                parent_pkg=parent_pkg,
                            )
                        else:
                            downloaded_path = self._download_callback(
                                dep_ref, self._apm_modules_dir, parent_chain
                            )
                        if downloaded_path and downloaded_path.exists():
                            install_path = downloaded_path
                        else:
                            # Fetch produced no usable path -- release the
                            # reservation so a subsequent retry (or a
                            # different anchor with the same key) can try
                            # again rather than silently treating the dep
                            # as already-downloaded.
                            with self._download_lock:
                                self._downloaded_packages.discard(unique_key)
                    except Exception as exc:
                        # Surface the failure at default verbosity AND log a
                        # traceback at debug. Previously this branch silently
                        # swallowed any error, masking transient network /
                        # auth failures behind a generic "package not found"
                        # downstream message (#940 F2 + SR5).
                        with self._download_lock:
                            self._downloaded_packages.discard(unique_key)
                        try:
                            from apm_cli.utils.console import _rich_warning

                            _rich_warning(
                                f"Failed to download dependency "
                                f"'{dep_ref.get_display_name()}': {exc}"
                            )
                        except Exception:
                            _logger.debug("Could not emit download-failure warning", exc_info=True)
                        _logger.debug(
                            "Download callback raised for %s",
                            dep_ref.get_display_name(),
                            exc_info=True,
                        )

            # Still doesn't exist after download attempt
            if not install_path.exists():
                return None

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
                    source_path=self._compute_dep_source_path(dep_ref, parent_pkg, install_path),
                )
            # No manifest found
            return None

        # Load and return the package, anchoring relative ``local_path`` deps
        # on the declaring package's source dir (#857). For local deps this
        # is the *original* user source; for remote deps it is the clone in
        # apm_modules.
        dep_source_path = self._compute_dep_source_path(dep_ref, parent_pkg, install_path)
        try:
            package = APMPackage.from_apm_yml(apm_yml_path, source_path=dep_source_path)
            # Ensure source is set for tracking. TODO(#940): the cache key
            # already considers source_path; this post-construction mutation
            # of ``source`` (a separate field) is safe today but has the same
            # shape as the bug we just fixed -- review when refactoring.
            if not package.source:
                package.source = dep_ref.repo_url
            return package
        except FileNotFoundError:
            return None
        except ValueError:
            raise

    @staticmethod
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

    @staticmethod
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

    @staticmethod
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

    @staticmethod
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
