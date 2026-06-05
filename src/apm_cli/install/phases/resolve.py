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
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from apm_cli.utils.short_sha import format_short_sha

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.models.dependency.reference import DependencyReference

_logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Private helpers (each mutates ctx in-place, following existing pattern)
# ------------------------------------------------------------------


def _lockfile_has_registry_deps(existing_lockfile) -> bool:
    """True when the on-disk lockfile records at least one registry-sourced dep.

    Used to construct the registry resolver even when apm.yml's
    ``registries:`` block has been removed but locked deps still need to
    re-install. A user clones a repo, the apm.yml has no registries: block
    but the lockfile says some deps are ``source: registry`` — we still
    want them to install (they'll fail at auth lookup if the URL doesn't
    match anything configured, with a clear remediation per §6.2).
    """
    if not existing_lockfile:
        return False
    return any(
        getattr(dep, "source", None) == "registry"
        for dep in existing_lockfile.dependencies.values()
    )


def _require_package_registry_feature_if_needed(registries_map, existing_lockfile) -> bool:
    """Validate the gate and return whether registry support is needed."""
    needs_registry = bool(registries_map) or _lockfile_has_registry_deps(existing_lockfile)
    if needs_registry:
        from apm_cli.deps.registry.feature_gate import require_package_registry_enabled

        require_package_registry_enabled("Registry-sourced installs")
    return needs_registry


def _git_semver_package_name(dep_ref: DependencyReference) -> str:
    """Return the package name used for git tag ``{name}`` matching."""
    if dep_ref.is_virtual_subdirectory() and dep_ref.virtual_path:
        return dep_ref.virtual_path.rstrip("/").rsplit("/", 1)[-1]
    return dep_ref.repo_url.rsplit("/", 1)[-1]


def _maybe_resolve_git_semver(
    *,
    dep_ref,
    existing_lockfile,
    update_refs: bool,
    auth_resolver=None,
):
    """Resolve a git-source semver-range ``ref:`` to a concrete tag.

    Returns the :class:`~apm_cli.deps.git_semver_resolver.GitSemverResolution`
    when resolution ran (and the caller should rewrite ``dep_ref.reference``);
    returns ``None`` for any dep that should NOT route through the
    git-semver resolver (local, registry-sourced, proxy-sourced, literal
    ref, or a lockfile-pinned reinstall without ``--update``).

    Lockfile replay
    ---------------
    When a lockfile entry already records ``constraint == dep_ref.reference``
    and the locked tag still satisfies it, this function rebuilds the
    :class:`GitSemverResolution` from the lockfile WITHOUT touching the
    network. This is the npm-style "honour the lock" path -- the locked
    tag is canonical until the manifest range changes or the user passes
    ``--update`` / ``--refresh``.

    Auth
    ----
    When ``auth_resolver`` is supplied, the per-dep ``AuthContext`` is
    resolved before constructing :class:`RefResolver` and its token is
    embedded in the ``https://`` URL used by ``git ls-remote``. This
    mirrors the auth path used by the clone step downstream, so a
    private-repo semver dep that clones successfully also enumerates
    its tags successfully in CI environments where ``GITHUB_APM_PAT`` /
    ``ADO_APM_PAT`` are the only credential source (no system
    credential helper available). Passing ``auth_resolver=None`` (the
    legacy path) preserves the previous unauthenticated behaviour for
    public repos and for callers that intentionally skip auth.
    """
    # Only git-source deps with a semver-range reference are eligible.
    if dep_ref.is_local:
        return None
    if getattr(dep_ref, "source", None) == "registry":
        return None
    if getattr(dep_ref, "artifactory_prefix", None):
        return None
    if dep_ref.ref_kind != "semver":
        return None

    constraint = dep_ref.reference
    owner_repo = dep_ref.repo_url
    package_name = _git_semver_package_name(dep_ref)

    # Lockfile replay (npm semantics): if the lockfile already records a
    # resolution for this constraint, return it directly. Saves a
    # ls-remote call and keeps installs deterministic across machines.
    if not update_refs and existing_lockfile is not None:
        locked = existing_lockfile.get_dependency(dep_ref.get_unique_key())
        if (
            locked is not None
            and locked.constraint == constraint
            and locked.resolved_tag
            and locked.resolved_commit
            and locked.version
        ):
            from apm_cli.deps.git_semver_resolver import GitSemverResolution

            return GitSemverResolution(
                constraint=locked.constraint,
                resolved_version=locked.version,
                resolved_tag=locked.resolved_tag,
                resolved_sha=locked.resolved_commit,
                # The pattern that produced the locked tag is not
                # persisted (it would just be informational); the empty
                # string here means "unknown / from lockfile".
                matched_pattern="",
                resolved_at=locked.resolved_at or "",
            )

    # Fresh resolution: call git ls-remote and pick the highest matching tag.
    from apm_cli.deps.git_semver_resolver import GitSemverResolver
    from apm_cli.marketplace.ref_resolver import RefResolver

    # Resolve the per-dep token via AuthResolver so ls-remote uses the
    # same credential source the downstream clone will use. Without this
    # threading, ls-remote on a private repo would rely on the host's
    # git credential helper (present on dev laptops, absent in CI).
    token: str | None = None
    if auth_resolver is not None:
        try:
            auth_ctx = auth_resolver.resolve_for_dep(dep_ref)
            token = auth_ctx.token if auth_ctx is not None else None
        except Exception:
            # Auth lookup is best-effort here: if it fails the unauth path
            # remains, the downstream clone will surface the real auth
            # error with its own actionable diagnostic.
            token = None

    ref_resolver = RefResolver(host=dep_ref.host, token=token)
    resolver = GitSemverResolver(ref_resolver)
    return resolver.resolve(
        owner_repo=owner_repo,
        package_name=package_name,
        constraint=constraint,
    )


def _purge_cached_semver_paths_for_update(
    *,
    all_apm_deps,
    apm_modules_dir,
    logger,
) -> None:
    """Pre-purge on-disk install paths for direct git-source semver deps
    when ``--update`` / ``--refresh`` is set.

    Bug 1 fix (#1496): the BFS resolver short-circuits at
    ``install_path.exists()`` and never invokes ``download_callback``,
    which is where ``_maybe_resolve_git_semver`` lives. For git-source
    semver direct deps we therefore pre-purge the install path so the
    resolver is forced through the callback, re-runs ``git ls-remote``,
    and rewrites the lockfile with the latest matching tag. Matches
    npm / cargo / bundler: ``--update`` is the explicit re-resolve
    trigger and must not be swallowed by the on-disk cache. Scoped to
    direct deps to avoid disturbing transitive cached content; the
    resolver re-walks transitives naturally once a direct dep's
    callback rewrites its ref. Local, registry, and proxy deps are
    excluded -- their semver semantics (if any) belong to a different
    resolver path.
    """
    from contextlib import suppress

    from apm_cli.utils.file_ops import robust_rmtree as _rrm

    for _dep in all_apm_deps:
        if getattr(_dep, "ref_kind", None) != "semver":
            continue
        if _dep.is_local:
            continue
        if getattr(_dep, "source", None) == "registry":
            continue
        if getattr(_dep, "artifactory_prefix", None):
            continue
        try:
            _ip = _dep.get_install_path(apm_modules_dir)
        except Exception:  # noqa: S112
            # Path computation failure (e.g. malformed dep) is non-fatal
            # here -- the resolver will surface a real error downstream.
            continue
        if _ip.exists():
            with suppress(Exception):
                _rrm(_ip, ignore_errors=True)
            if logger:
                logger.verbose_detail(
                    f"[*] --update: cleared cached install path for "
                    f"{_dep.get_unique_key()} to force semver re-resolution"
                )


def _load_lockfile(ctx: InstallContext) -> None:
    """Load ``apm.lock.yaml`` and populate ``ctx.existing_lockfile`` / ``ctx.lockfile_path``."""
    # ------------------------------------------------------------------
    # 1. Lockfile loading
    # ------------------------------------------------------------------
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

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
                    _sha = format_short_sha(locked_dep.resolved_commit)
                    _ref = (
                        locked_dep.resolved_ref
                        if hasattr(locked_dep, "resolved_ref") and locked_dep.resolved_ref
                        else ""
                    )
                    ctx.logger.lockfile_entry(locked_dep.get_unique_key(), ref=_ref, sha=_sha)
    ctx.existing_lockfile = existing_lockfile


def _ensure_modules_dir(ctx: InstallContext) -> None:
    """Create the ``apm_modules/`` directory and populate ``ctx.apm_modules_dir``."""
    # ------------------------------------------------------------------
    # 2. apm_modules directory
    # ------------------------------------------------------------------
    from apm_cli.core.scope import get_modules_dir

    apm_modules_dir = get_modules_dir(ctx.scope)
    apm_modules_dir.mkdir(parents=True, exist_ok=True)
    ctx.apm_modules_dir = apm_modules_dir


def _setup_downloader(ctx: InstallContext) -> None:
    """Create auth resolver and downloader; populate ``ctx.auth_resolver`` / ``ctx.downloader``."""
    # ------------------------------------------------------------------
    # 3. Auth resolver + downloader
    # ------------------------------------------------------------------
    import os as _os

    from apm_cli.core.auth import AuthResolver
    from apm_cli.deps import github_downloader as _ghd_mod

    if ctx.auth_resolver is None:
        ctx.auth_resolver = AuthResolver()

    downloader = _ghd_mod.GitHubPackageDownloader(
        auth_resolver=ctx.auth_resolver,
        protocol_pref=ctx.protocol_pref,
        allow_fallback=ctx.allow_protocol_fallback,
    )
    ctx.downloader = downloader

    # WS2a (#1116): attach a per-run shared clone cache so subdirectory
    # deps from the same upstream repo+ref share a single git clone.
    # The cache is cleaned up after resolution completes (see _resolve_dependencies).
    from apm_cli.deps.shared_clone_cache import SharedCloneCache

    shared_cache = SharedCloneCache()
    downloader.shared_clone_cache = shared_cache

    # WS3 (#1116): attach persistent cross-run git cache unless disabled
    # via APM_NO_CACHE environment variable.
    if not _os.environ.get("APM_NO_CACHE"):
        from apm_cli.cache.paths import get_cache_root

        try:
            from apm_cli.cache.git_cache import GitCache

            _cache_root = get_cache_root()
            downloader.persistent_git_cache = GitCache(
                _cache_root,
                refresh=ctx.refresh,
            )
        except (OSError, ValueError):
            pass  # Cache unavailable (permissions, missing dir) -- degrade gracefully

    # Perf #1433: attach the InstallLogger so the subdir download path
    # can emit verbose-only [perf] lines (subdir cache state, bare
    # clone strategy + elapsed, materialize sparse-applied + size).
    # Optional; tests / non-install drivers leave this None.
    if ctx.logger is not None:
        downloader.install_logger = ctx.logger

    # #1369: tiered ref resolver. Collapses N redundant shallow clones
    # for ref->SHA resolution into a per-run cache + cheap commits API
    # + bare-rev-parse waterfall, falling back to the legacy clone path.
    # Wired AFTER persistent_git_cache so L2 can reach it. Reused by
    # every code path that calls downloader.resolve_git_reference():
    # install, update, outdated, publish.
    try:
        from apm_cli.deps.tiered_ref_resolver import build_tiered_ref_resolver

        _tiered = build_tiered_ref_resolver(
            downloader=downloader,
            git_cache=getattr(downloader, "persistent_git_cache", None),
        )
        if _tiered is not None:
            downloader._tiered_resolver = _tiered
            ctx.ref_resolver = _tiered
    except Exception as exc:  # pragma: no cover - defensive: never block resolve phase
        # Keep non-blocking behavior, but make it diagnosable in --verbose.
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "Tiered ref resolver wiring skipped (%s): %s",
            type(exc).__name__,
            exc,
        )


def _fail_on_resolution_errors(ctx: InstallContext, dependency_graph) -> None:
    """Raise when the resolver recorded fatal dependency-resolution errors."""
    if not dependency_graph.resolution_errors:
        return
    for error in dependency_graph.resolution_errors:
        if ctx.logger:
            ctx.logger.error(error)
    joined_errors = "; ".join(dependency_graph.resolution_errors)
    raise RuntimeError(f"Dependency resolution failed: {joined_errors}")


def _resolve_dependencies(ctx: InstallContext) -> None:
    """Run ``APMDependencyResolver``, handle errors; populate ``ctx.deps_to_install`` and ``ctx.dependency_graph``.

    Also wires the download callback (which handles transitive package fetching),
    builds ``ctx.dep_base_dirs``, writes ancillary state to ``ctx``, and cleans up
    the shared clone cache.
    """
    from apm_cli.deps.apm_resolver import APMDependencyResolver
    from apm_cli.install.insecure_policy import (
        _check_insecure_dependencies,
        _collect_insecure_dependency_infos,
        _guard_transitive_insecure_dependencies,
        _warn_insecure_dependencies,
    )

    # ------------------------------------------------------------------
    # 3b. Dedicated registry resolver (design §3.1, §8)
    # ------------------------------------------------------------------
    # Built when:
    #   - the manifest's apm.yml has a top-level ``registries:`` block, OR
    #   - the on-disk lockfile has at least one ``source: registry`` entry
    #     (re-install of a project whose authors removed the block but the
    #     locked deps still need somewhere to land).
    # In the second case the URL is the trust anchor — auth resolves by
    # URL prefix against the apm.yml registries map (which may be empty,
    # forcing anonymous fetch).
    registry_resolver = None
    _apply_lockfile_registry_name = None
    existing_lockfile = ctx.existing_lockfile
    registries_map = getattr(ctx.apm_package, "registries", None) or {}
    needs_registry = _require_package_registry_feature_if_needed(registries_map, existing_lockfile)
    if needs_registry:
        from apm_cli.deps.registry.auth import (
            dependency_ref_with_registry_name_from_lockfile,
        )
        from apm_cli.deps.registry.resolver import RegistryPackageResolver

        registry_resolver = RegistryPackageResolver(registries_map)
        _apply_lockfile_registry_name = dependency_ref_with_registry_name_from_lockfile
    ctx.registry_resolver = registry_resolver

    # ------------------------------------------------------------------
    # 4. Tracking variables + transitive download callback
    # ------------------------------------------------------------------
    # direct_dep_keys is phase-local (only read by the download callback).
    direct_dep_keys = builtins.set(dep.get_unique_key() for dep in ctx.all_apm_deps)
    # project_root is reused below when building dep_base_dirs for transitive
    # local deps (#857).
    project_root = ctx.project_root
    # --refresh implies re-resolution of all refs (but does NOT discard
    # lockfile entries for packages not in the manifest, unlike --update
    # which may restructure the whole graph).
    update_refs = ctx.update_refs or ctx.refresh
    if ctx.refresh and ctx.logger:
        ctx.logger.verbose_detail("[*] --refresh: re-resolving all refs")

    # The former nested ``download_callback`` closure now lives in a stateful
    # callable so this function stays within the complexity/statement budget.
    # It accumulates downloaded / failures / transitive_failures which are
    # folded back onto ctx after resolution (same mutable objects, by
    # identity). Constructed lazily to avoid a resolve <-> resolve_transitive
    # import cycle.
    from apm_cli.install.phases.resolve_transitive import _TransitiveDownloader

    download_cb = _TransitiveDownloader(
        ctx,
        registry_resolver=registry_resolver,
        apply_lockfile_registry_name=_apply_lockfile_registry_name,
        registries_map=registries_map,
        direct_dep_keys=direct_dep_keys,
        update_refs=update_refs,
    )

    # ------------------------------------------------------------------
    # 6. Resolver creation + dependency resolution
    # ------------------------------------------------------------------
    if update_refs:
        _purge_cached_semver_paths_for_update(
            all_apm_deps=ctx.all_apm_deps,
            apm_modules_dir=ctx.apm_modules_dir,
            logger=ctx.logger,
        )

    resolver = APMDependencyResolver(
        apm_modules_dir=ctx.apm_modules_dir,
        download_callback=download_cb,
        auth_resolver=ctx.auth_resolver,
    )

    # Resolver reads ``<anchor>/apm.yml``. Preserve the original
    # ``ctx.apm_dir`` anchor for every non-``--root`` install (zero
    # behavior change: USER -> ``~/.apm``, PROJECT -> deploy root == cwd).
    # When ``ctx.source_root`` differs from ``ctx.project_root`` (set by
    # ``apm install --root`` via the pipeline), the manifest read diverges
    # to ``ctx.source_root`` ($PWD) so sources keep resolving from the
    # user's working directory while writes land under the deploy root.
    # Using the ctx field (rather than the global ContextVar) makes this
    # branch reachable for any caller that sets source_root directly.
    # ``apm_modules_dir`` is already pinned on the resolver above, so
    # this arg selects only where ``apm.yml`` is read -- never where
    # ``apm_modules/`` is written.
    manifest_anchor = ctx.source_root if ctx.source_root != ctx.project_root else ctx.apm_dir
    dependency_graph = resolver.resolve_dependencies(manifest_anchor)
    ctx.dependency_graph = dependency_graph
    _fail_on_resolution_errors(ctx, dependency_graph)

    # Read back the accumulators populated by the download callback (same
    # mutable objects, by identity) so the post-resolution code and the
    # ctx.callback_* assignments below operate unchanged.
    callback_downloaded = download_cb.downloaded
    transitive_failures = download_cb.transitive_failures
    callback_failures = download_cb.failures

    # Fold remote-parent local_path rejections into ``callback_failures`` so
    # the integrate phase skips them via the same gate used for download
    # failures (PR #1111 review C2). The resolver has already emitted the
    # red ERROR notice; here we just propagate the dep_key.
    rejected_remote_local = getattr(resolver, "_rejected_remote_local_keys", set())
    if rejected_remote_local:
        callback_failures.update(rejected_remote_local)

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
    # 7.5 Build dep_key -> parent source_path map for transitive locals
    # ------------------------------------------------------------------
    # Local deps declared by a transitive parent must be anchored on the
    # parent's source dir, not on the consumer's project root (#857). We
    # walk the dependency tree once here and stash the per-dep base_dir
    # for the integrate phase to consume.
    #
    # Keying caveat (PR #1111 review C3): the map is keyed by
    # ``dep_ref.get_unique_key()``, which for local deps is the raw
    # ``local_path`` string. Two different parents that both declare the
    # same relative ``local_path`` (e.g. both write ``../base``) collapse
    # to the same key. In the current architecture this collision is
    # latent: the BFS walk in ``APMDependencyResolver`` already dedupes
    # by ``get_unique_key()`` so only one node ever exists for that key,
    # and ``DependencyReference.get_install_path`` shares the same
    # ``apm_modules/_local/<basename>`` slot regardless of the parent.
    # That means today the "second parent wins" question never actually
    # fires -- the second occurrence is dropped at queue-time. We still
    # detect divergent-anchor writes here and warn loudly, both because
    # silent first-wins behaviour would mask a real bug if BFS dedup ever
    # changes, and because the warning gives the user a path to diagnose
    # surprising layouts (e.g. ``../base`` from two parents resolving to
    # different absolute directories).
    dep_base_dirs: builtins.dict[str, Path] = {}
    try:
        tree = dependency_graph.dependency_tree
        for node in tree.nodes.values():
            parent_node = node.parent
            if parent_node is None or parent_node.package is None:
                continue
            anchor = (
                parent_node.package.source_path
                if parent_node.package.source_path is not None
                else project_root
            )
            key = node.dependency_ref.get_unique_key()
            existing = dep_base_dirs.get(key)
            if existing is not None and existing != anchor:
                # Divergent anchors for the same dep key. Keep the first
                # (deterministic) and surface the conflict so the user can
                # rename one of the colliding refs or use absolute paths.
                _logger.warning(
                    "Local dep %r is referenced from two parents with "
                    "different anchors (%s vs %s). Using the first; "
                    "rename one of the local_path values or use absolute "
                    "paths to disambiguate.",
                    key,
                    existing,
                    anchor,
                )
                continue
            dep_base_dirs[key] = anchor
    except (AttributeError, KeyError):
        # Tree shape may differ across releases; fall back to empty map
        # (callers default to project_root anchoring, matching legacy).
        # Narrow set: real bugs (TypeError/NameError) should surface, not
        # silently degrade to legacy anchoring.
        dep_base_dirs = {}
    ctx.dep_base_dirs = dep_base_dirs

    # ------------------------------------------------------------------
    # Write ancillary state to ctx for later phases
    # ------------------------------------------------------------------
    ctx.callback_downloaded = callback_downloaded
    ctx.callback_failures = callback_failures
    ctx.transitive_failures = transitive_failures

    # WS2a (#1116): release shared clone temp dirs now that all subdir
    # deps have extracted their subpaths.  Safe to call even if no
    # subdir deps were processed (no-op in that case).
    shared_cache = getattr(ctx.downloader, "shared_clone_cache", None)
    if shared_cache is not None:
        shared_cache.cleanup()

    # Perf #1433: emit ref-resolver tier hit counts at the end of the
    # resolve phase. Verbose only; one line; lets reviewers see which
    # waterfall tier carried the run without attaching a debugger.
    if ctx.logger is not None and ctx.ref_resolver is not None:
        _tier_stats = getattr(ctx.ref_resolver, "stats", None)
        if _tier_stats:
            # tier_summary is install-only; other loggers degrade silently.
            if hasattr(ctx.logger, "tier_summary"):
                ctx.logger.tier_summary(_tier_stats)


def _apply_only_filter(ctx: InstallContext) -> None:
    """Filter ``ctx.deps_to_install`` to the ``--only`` package(s) and their subtrees."""
    # ------------------------------------------------------------------
    # 7. --only filtering
    # ------------------------------------------------------------------
    from apm_cli.models.apm_package import DependencyReference

    # Build identity set from user-supplied package specs.
    # Accepts any input form: git URLs, FQDN, shorthand.
    only_identities: builtins.set = builtins.set()
    for p in ctx.only_packages:
        try:
            ref = DependencyReference.parse(p)
            only_identities.add(ref.get_identity())
        except Exception:
            only_identities.add(p)

    # Expand the set to include transitive descendants of the
    # requested packages so their MCP servers, primitives, etc.
    # are correctly installed and written to the lockfile.
    tree = ctx.dependency_graph.dependency_tree

    def _collect_descendants(node: object, visited: builtins.set | None = None) -> None:
        """Walk the tree and add every child identity (cycle-safe)."""
        if visited is None:
            visited = builtins.set()
        for child in node.children:  # type: ignore[attr-defined]
            identity = child.dependency_ref.get_identity()
            if identity not in visited:
                visited.add(identity)
                only_identities.add(identity)
                _collect_descendants(child, visited)

    for node in tree.nodes.values():
        if node.dependency_ref.get_identity() in only_identities:
            _collect_descendants(node)

    ctx.deps_to_install = [
        dep for dep in ctx.deps_to_install if dep.get_identity() in only_identities
    ]


def _compute_intended_dep_keys(ctx: InstallContext) -> None:
    """Populate ``ctx.intended_dep_keys`` (manifest-intent set for orphan cleanup)."""
    # ------------------------------------------------------------------
    # 8. Orphan detection: intended_dep_keys
    # ------------------------------------------------------------------
    ctx.intended_dep_keys = builtins.set(d.get_unique_key() for d in ctx.deps_to_install)


def run(ctx: InstallContext) -> None:
    """Execute the resolve phase.

    On return every field listed in the *Resolve phase outputs* section of
    :class:`~apm_cli.install.context.InstallContext` is populated.
    """
    _load_lockfile(ctx)
    _ensure_modules_dir(ctx)
    _setup_downloader(ctx)
    _resolve_dependencies(ctx)
    if ctx.only_packages:
        _apply_only_filter(ctx)
    _compute_intended_dep_keys(ctx)
