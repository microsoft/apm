"""Dependency resolution phase.

Reads ``ctx.apm_package``, ``ctx.update_refs``, ``ctx.scope``, etc.;
populates ``ctx.deps_to_install``, ``ctx.intended_dep_keys``,
``ctx.dependency_graph``, ``ctx.existing_lockfile``, and several ancillary
fields consumed by later phases (download, integrate, cleanup, lockfile).

This is the first phase of the install pipeline.  It covers:

1. Lockfile loading (``apm.lock.yaml``)
2. ``apm_modules/`` directory creation
3. Auth resolver defaulting + downloader construction
4. Transitive dependency resolution via ``APMDependencyResolver``
5. ``--only`` filtering (restrict to named packages + their subtrees)
6. ``intended_dep_keys`` computation (the manifest-intent set used by
   orphan cleanup in a later phase)
"""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def run(ctx: InstallContext) -> None:
    """Execute the resolve phase.

    On return every field listed in the *Resolve phase outputs* section of
    :class:`~apm_cli.install.context.InstallContext` is populated.
    """
    from apm_cli.core.auth import AuthResolver
    from apm_cli.core.scope import InstallScope, get_modules_dir
    from apm_cli.deps import github_downloader as _ghd_mod
    from apm_cli.deps.apm_resolver import APMDependencyResolver
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path
    from apm_cli.install.phases.local_content import _copy_local_package
    from apm_cli.models.apm_package import DependencyReference

    # ------------------------------------------------------------------
    # 1. Lockfile loading
    # ------------------------------------------------------------------
    lockfile_path = get_lockfile_path(ctx.apm_dir)
    ctx.lockfile_path = lockfile_path
    existing_lockfile = None
    lockfile_count = 0
    if ctx.early_lockfile is not None:
        existing_lockfile = ctx.early_lockfile
    elif lockfile_path.exists():
        existing_lockfile = LockFile.read(lockfile_path)
    if existing_lockfile and existing_lockfile.dependencies:
        lockfile_count = len(existing_lockfile.dependencies)
        if ctx.logger:
            if ctx.update_refs:
                ctx.logger.verbose_detail(
                    f"Loaded apm.lock.yaml for SHA comparison ({lockfile_count} dependencies)"
                )
            else:
                ctx.logger.verbose_detail(
                    f"Using apm.lock.yaml ({lockfile_count} locked dependencies)"
                )
            if ctx.logger.verbose:
                for locked_dep in existing_lockfile.get_all_dependencies():
                    _sha = locked_dep.resolved_commit[:8] if locked_dep.resolved_commit else ""
                    _ref = (
                        locked_dep.resolved_ref
                        if hasattr(locked_dep, "resolved_ref") and locked_dep.resolved_ref
                        else ""
                    )
                    ctx.logger.lockfile_entry(locked_dep.get_unique_key(), ref=_ref, sha=_sha)
    ctx.existing_lockfile = existing_lockfile

    # ------------------------------------------------------------------
    # 2. apm_modules directory
    # ------------------------------------------------------------------
    apm_modules_dir = get_modules_dir(ctx.scope)
    apm_modules_dir.mkdir(parents=True, exist_ok=True)
    ctx.apm_modules_dir = apm_modules_dir

    # ------------------------------------------------------------------
    # 3. Auth resolver + downloader
    # ------------------------------------------------------------------
    if ctx.auth_resolver is None:
        ctx.auth_resolver = AuthResolver()

    downloader = _ghd_mod.GitHubPackageDownloader(
        auth_resolver=ctx.auth_resolver,
        protocol_pref=ctx.protocol_pref,
        allow_fallback=ctx.allow_protocol_fallback,
    )
    ctx.downloader = downloader

    # ------------------------------------------------------------------
    # 4. Tracking variables (phase-local except where noted)
    # ------------------------------------------------------------------
    # direct_dep_keys is phase-local (only read inside download_callback)
    direct_dep_keys = builtins.set(dep.get_unique_key() for dep in ctx.all_apm_deps)
    # These three escape to later phases via ctx
    callback_downloaded: builtins.dict = {}
    transitive_failures: builtins.list = []
    callback_failures: builtins.set = builtins.set()

    # ------------------------------------------------------------------
    # 5. Download callback for transitive resolution
    # ------------------------------------------------------------------
    # Capture frequently-used ctx fields as locals for the closure.
    # This matches the original code's closure over function-level locals.
    scope = ctx.scope
    project_root = ctx.project_root
    update_refs = ctx.update_refs
    logger = ctx.logger
    verbose = ctx.verbose  # noqa: F841

    def download_callback(dep_ref, modules_dir, parent_chain=""):
        """Download a package during dependency resolution.

        Args:
            dep_ref: The dependency to download.
            modules_dir: Target apm_modules directory.
            parent_chain: Human-readable breadcrumb (e.g. "root > mid")
                showing which dependency path led to this transitive dep.
        """
        install_path = dep_ref.get_install_path(modules_dir)
        if install_path.exists():
            return install_path
        try:
            # Handle local packages: copy instead of git clone
            if dep_ref.is_local and dep_ref.local_path:
                if (
                    scope is InstallScope.USER
                    and not Path(dep_ref.local_path).expanduser().is_absolute()
                ):
                    # At user scope, relative local paths have no meaningful
                    # root (cwd is arbitrary, $HOME is not a project).  Only
                    # absolute paths are unambiguous; reject relative refs.
                    # Note: callback_failures is a set (see line ~105),
                    # so use .add() rather than dict-style assignment.
                    callback_failures.add(dep_ref.get_unique_key())
                    return None
                result_path = _copy_local_package(
                    dep_ref, install_path, project_root, logger=logger
                )
                if result_path:
                    callback_downloaded[dep_ref.get_unique_key()] = None
                    return result_path
                return None

            # T5: Use locked commit if available (reproducible installs)
            locked_ref = None
            if existing_lockfile:
                locked_dep = existing_lockfile.get_dependency(dep_ref.get_unique_key())
                if (
                    locked_dep
                    and locked_dep.resolved_commit
                    and locked_dep.resolved_commit != "cached"
                ):
                    locked_ref = locked_dep.resolved_commit

            # Build a DependencyReference with the right ref to avoid lossy
            # str() -> parse() round-trips (#382).
            from dataclasses import replace as _dc_replace

            if locked_ref and not update_refs:
                download_dep = _dc_replace(dep_ref, reference=locked_ref)
            else:
                download_dep = dep_ref

            # Silent download - no progress display for transitive deps
            result = downloader.download_package(download_dep, install_path)
            # Capture resolved commit SHA for lockfile
            resolved_sha = None
            if result and hasattr(result, "resolved_reference") and result.resolved_reference:
                resolved_sha = result.resolved_reference.resolved_commit
            callback_downloaded[dep_ref.get_unique_key()] = resolved_sha
            return install_path
        except Exception as e:
            dep_display = dep_ref.get_display_name()
            dep_key = dep_ref.get_unique_key()
            is_direct = dep_key in direct_dep_keys

            # Distinguish direct vs transitive failure messages so users
            # don't see a misleading "transitive dep" label for top-level deps.
            if is_direct:
                fail_msg = f"Failed to download dependency {dep_ref.repo_url}: {e}"
            else:
                chain_hint = f" (via {parent_chain})" if parent_chain else ""
                fail_msg = f"Failed to resolve transitive dep {dep_ref.repo_url}{chain_hint}: {e}"

            # Verbose: inline detail via logger (single output path).
            # Deferred diagnostics below cover the non-logger case.
            if logger:
                logger.verbose_detail(f"  {fail_msg}")
            # Collect for deferred diagnostics summary (always, even non-verbose)
            callback_failures.add(dep_key)
            transitive_failures.append((dep_display, fail_msg))
            return None

    # ------------------------------------------------------------------
    # 6. Resolver creation + dependency resolution
    # ------------------------------------------------------------------
    resolver = APMDependencyResolver(
        apm_modules_dir=apm_modules_dir,
        download_callback=download_callback,
    )

    dependency_graph = resolver.resolve_dependencies(ctx.apm_dir)
    ctx.dependency_graph = dependency_graph

    # Verbose: show resolved tree summary
    if ctx.logger:
        tree = dependency_graph.dependency_tree
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
            ctx.logger.verbose_detail(
                f"Resolved {direct_count} direct dependencies (no transitive)"
            )

    # Check for circular dependencies
    if dependency_graph.circular_dependencies:
        if ctx.logger:
            ctx.logger.error("Circular dependencies detected:")
        for circular in dependency_graph.circular_dependencies:
            cycle_path = " -> ".join(circular.cycle_path)
            if ctx.logger:
                ctx.logger.error(f"  {cycle_path}")
        raise RuntimeError("Cannot install packages with circular dependencies")

    # Get flattened dependencies for installation
    flat_deps = dependency_graph.flattened_dependencies
    deps_to_install = flat_deps.get_installation_list()

    # ------------------------------------------------------------------
    # 7. --only filtering
    # ------------------------------------------------------------------
    if ctx.only_packages:
        # Build identity set from user-supplied package specs.
        # Accepts any input form: git URLs, FQDN, shorthand.
        only_identities = builtins.set()
        for p in ctx.only_packages:
            try:
                ref = DependencyReference.parse(p)
                only_identities.add(ref.get_identity())
            except Exception:
                only_identities.add(p)

        # Expand the set to include transitive descendants of the
        # requested packages so their MCP servers, primitives, etc.
        # are correctly installed and written to the lockfile.
        tree = dependency_graph.dependency_tree

        def _collect_descendants(node, visited=None):
            """Walk the tree and add every child identity (cycle-safe)."""
            if visited is None:
                visited = builtins.set()
            for child in node.children:
                identity = child.dependency_ref.get_identity()
                if identity not in visited:
                    visited.add(identity)
                    only_identities.add(identity)
                    _collect_descendants(child, visited)

        for node in tree.nodes.values():
            if node.dependency_ref.get_identity() in only_identities:
                _collect_descendants(node)

        deps_to_install = [dep for dep in deps_to_install if dep.get_identity() in only_identities]

    from apm_cli.install.insecure_policy import (
        _check_insecure_dependencies,
        _collect_insecure_dependency_infos,
        _guard_transitive_insecure_dependencies,
        _warn_insecure_dependencies,
    )

    _check_insecure_dependencies(
        ctx.all_apm_deps,
        ctx.allow_insecure,
        ctx.logger,
    )
    insecure_infos = _collect_insecure_dependency_infos(
        deps_to_install,
        dependency_graph,
    )
    _warn_insecure_dependencies(insecure_infos, ctx.logger)
    _guard_transitive_insecure_dependencies(
        insecure_infos,
        ctx.logger,
        allow_insecure=ctx.allow_insecure,
        allow_insecure_hosts=ctx.allow_insecure_hosts,
    )

    ctx.deps_to_install = deps_to_install

    # ------------------------------------------------------------------
    # 8. Orphan detection: intended_dep_keys
    # ------------------------------------------------------------------
    ctx.intended_dep_keys = builtins.set(d.get_unique_key() for d in deps_to_install)

    # ------------------------------------------------------------------
    # Write ancillary state to ctx for later phases
    # ------------------------------------------------------------------
    ctx.callback_downloaded = callback_downloaded
    ctx.callback_failures = callback_failures
    ctx.transitive_failures = transitive_failures
