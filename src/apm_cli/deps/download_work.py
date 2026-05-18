"""APM dependency resolution engine with recursive resolution and conflict detection."""

import logging
from dataclasses import replace
from pathlib import Path

from ..models.apm_package import APMPackage, DependencyReference
from .dependency_graph import (
    DependencyGraph,
    DependencyTree,
    FlatDependencyMap,
)

_logger = logging.getLogger(__name__)
_DEFAULT_RESOLVE_PARALLEL = 4


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
        raise ValueError("expand_parent_repo_decl requires child_dep.is_parent_repo_inheritance")
    if parent_dep.is_local:
        raise ValueError("git: parent cannot inherit from a local path dependency")
    if parent_dep.repo_url.startswith("_local/"):
        raise ValueError("git: parent cannot inherit from a local path dependency")
    if not self._remote_parent_eligible(parent_dep):
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


def _load_from_install_path(
    self,
    dep_ref: DependencyReference,
    install_path: Path,
    parent_pkg: APMPackage | None,
) -> APMPackage | None:
    """Load an APMPackage from an already-located *install_path*.

    Covers four outcomes in priority order:
    1. No ``apm.yml`` but ``SKILL.md`` present -- return a minimal package.
    2. No ``apm.yml`` and no ``SKILL.md`` -- return None.
    3. ``apm.yml`` found -- parse and return the package.
    4. ``FileNotFoundError`` during parse -- return None (re-raises ValueError).
    """
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


def _handle_download_callback(
    self,
    dep_ref: DependencyReference,
    parent_chain: str,
    parent_pkg: APMPackage | None,
    install_path: Path,
) -> Path:
    """Invoke the download callback (if set) and return the (possibly updated) install_path."""
    if self._download_callback is None:
        return install_path

    unique_key = self._download_dedup_key(dep_ref, parent_pkg)
    # F7 (#1116): atomically check-and-reserve under ``_download_lock`` so two
    # BFS workers racing on the same logical dep can't both pass the gate and
    # double-fetch.
    with self._download_lock:
        should_fetch = unique_key not in self._downloaded_packages
        if should_fetch:
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
                # Fetch produced no usable path -- release the reservation so a
                # subsequent retry can try again.
                with self._download_lock:
                    self._downloaded_packages.discard(unique_key)
        except Exception as exc:
            # Surface the failure at default verbosity AND log a traceback at
            # debug (#940 F2 + SR5).
            with self._download_lock:
                self._downloaded_packages.discard(unique_key)
            try:
                from apm_cli.utils.console import _rich_warning

                _rich_warning(
                    f"Failed to download dependency '{dep_ref.get_display_name()}': {exc}"
                )
            except Exception:
                _logger.debug("Could not emit download-failure warning", exc_info=True)
            _logger.debug(
                "Download callback raised for %s",
                dep_ref.get_display_name(),
                exc_info=True,
            )
    return install_path


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
        install_path = _handle_download_callback(
            self, dep_ref, parent_chain, parent_pkg, install_path
        )

        # Still doesn't exist after download attempt
        if not install_path.exists():
            return None

    # Look for apm.yml in the install path -- delegate to helper
    return _load_from_install_path(self, dep_ref, install_path, parent_pkg)
