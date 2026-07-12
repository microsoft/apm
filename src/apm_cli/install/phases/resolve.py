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

from apm_cli.install.helpers.ref_seed import seed_ref_resolver_from_lockfile
from apm_cli.install.transaction import resolution_for_context
from apm_cli.models.apm_package import GitReferenceType, ResolvedReference
from apm_cli.utils.short_sha import format_short_sha

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.install.resolution_staging import ResolutionStagingSession
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
    ref_resolver_cache=None,
    ref_resolver_cache_lock=None,
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
    resolved before constructing :class:`RefResolver`; its token and
    authentication scheme are forwarded to ``git ls-remote``, mirroring
    the clone step downstream. A private-repo semver dep that clones
    successfully then also enumerates its tags in CI environments where
    ``GITHUB_APM_PAT`` / ``ADO_APM_PAT`` are the only credential source.
    ``auth_resolver=None`` preserves unauthenticated public-repo behavior.
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

    from apm_cli.deps.git_semver_resolver import GitSemverResolver
    from apm_cli.install.helpers.ref_reuse import (
        get_shared_ref_resolver,
        resolve_dep_auth,
    )

    # Reuse one resolver per auth context so same-repo deps share tag listing.
    token, auth_scheme, git_env = resolve_dep_auth(dep_ref, auth_resolver)
    ref_resolver = get_shared_ref_resolver(
        dep_ref.host,
        token,
        ref_resolver_cache,
        ref_resolver_cache_lock,
        auth_scheme=auth_scheme,
        git_env=git_env,
        auth_resolver=auth_resolver,
        auth_target=dep_ref.host,
    )
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
    staging_session: ResolutionStagingSession,
) -> None:
    """Pre-purge on-disk install paths for direct git-source and registry semver deps
    when ``--update`` / ``--refresh`` is set.

    Bug 1 fix (#1496): the BFS resolver short-circuits at
    ``install_path.exists()`` and never invokes ``download_callback``,
    which is where ``_maybe_resolve_git_semver`` lives. For git-source
    semver direct deps we therefore pre-purge the install path so the
    resolver is forced through the callback, re-runs ``git ls-remote``,
    and rewrites the lockfile with the latest matching tag. Matches
    npm / cargo / bundler: ``--update`` is the explicit re-resolve
    trigger and must not be swallowed by the on-disk cache. Scoped to
    direct deps -- a transitive dep's own range is covered separately by
    ``APMDependencyResolver._should_force_recheck``. Local and proxy deps
    are excluded (different resolver path). Registry semver deps are
    included: their callback also gates on install_path.exists().
    """
    from contextlib import suppress

    from apm_cli.utils.file_ops import robust_rmtree as _rrm

    for _dep in all_apm_deps:
        if getattr(_dep, "ref_kind", None) != "semver":
            continue
        if _dep.is_local:
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
            staging_session.prepare_path(_ip)
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


def _annotate_registry_dep_ref(dep_ref, registry_resolver) -> None:
    reg_res = registry_resolver.last_resolutions.get(dep_ref.get_unique_key())
    if not reg_res or not reg_res.version:
        return
    dep_ref.resolved_reference = ResolvedReference(
        original_ref=dep_ref.reference or reg_res.version,
        ref_type=GitReferenceType.TAG,
        ref_name=reg_res.version,
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


def _resolve_dependencies(ctx: InstallContext, staging_session: ResolutionStagingSession) -> None:
    """Resolve dependencies and populate the resolution fields on ``ctx``."""
    import threading as _threading

    from apm_cli.core.scope import InstallScope
    from apm_cli.deps.apm_resolver import APMDependencyResolver
    from apm_cli.install.insecure_policy import (
        _check_insecure_dependencies,
        _collect_insecure_dependency_infos,
        _guard_transitive_insecure_dependencies,
        _warn_insecure_dependencies,
    )
    from apm_cli.install.phases.local_content import _copy_local_package

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
    # 4. Tracking variables (phase-local except where noted)
    # ------------------------------------------------------------------
    # direct_dep_keys is phase-local (only read inside download_callback)
    direct_dep_keys = builtins.set(dep.get_unique_key() for dep in ctx.all_apm_deps)
    # These three escape to later phases via ctx
    callback_downloaded: builtins.dict = {}
    transitive_failures: builtins.list = []
    callback_failures: builtins.set = builtins.set()
    # F7 (#1116): the resolver may dispatch ``download_callback`` calls
    # across a worker pool. CPython's GIL makes individual dict/set/list
    # mutations atomic, but logging emission and the read+update on
    # ``callback_downloaded`` (e.g. duplicate-key races) are not. A single
    # narrow lock around the result-recording sites is sufficient and
    # cheap; the heavy I/O work runs OUTSIDE the lock.
    callback_lock = _threading.Lock()

    # ------------------------------------------------------------------
    # 5. Download callback for transitive resolution
    # ------------------------------------------------------------------
    # Capture frequently-used ctx fields as locals for the closure.
    # This matches the original code's closure over function-level locals.
    scope = ctx.scope
    project_root = ctx.project_root
    # Local-path package references in apm.yml are relative to the
    # manifest's location (source_root), not the deploy override.
    # source_root is required on InstallContext; equals project_root
    # when --root is not used.
    source_root = ctx.source_root
    # --refresh implies re-resolution of all refs (but does NOT discard
    # lockfile entries for packages not in the manifest, unlike --update
    # which may restructure the whole graph).
    update_refs = ctx.update_refs or ctx.refresh
    if ctx.refresh and ctx.logger:
        ctx.logger.verbose_detail("[*] --refresh: re-resolving all refs")
    logger = ctx.logger
    existing_lockfile = ctx.existing_lockfile
    downloader = ctx.downloader

    # Hoist drift helpers so download_callback avoids per-call sys.modules
    # lookups and static analysis can see the dependency.
    from apm_cli.drift import build_download_ref, detect_ref_change

    verbose = ctx.verbose  # noqa: F841

    def download_callback(dep_ref, modules_dir, parent_chain="", parent_pkg=None):
        """Download a package during dependency resolution.

        Args:
            dep_ref: The dependency to download.
            modules_dir: Target apm_modules directory.
            parent_chain: Human-readable breadcrumb (e.g. "root > mid")
                showing which dependency path led to this transitive dep.
            parent_pkg: APMPackage that declared *dep_ref*, or None for direct
                deps from the root project. For local deps we use its
                ``source_path`` as the anchor for relative paths so a
                transitive ``../sibling`` resolves against the declaring
                package's directory rather than the root consumer (#857).
        """
        install_path = dep_ref.get_install_path(modules_dir)
        # Cache short-circuit: skip the rest of the callback when the
        # install path already exists. Exception: for git-source semver
        # deps under ``--update`` / ``--refresh`` (``update_refs=True``),
        # fall through so ``_maybe_resolve_git_semver`` re-runs
        # ``git ls-remote`` and the lockfile gets rewritten with the
        # latest matching tag. Matches npm/cargo/bundler: ``--update``
        # is the explicit re-resolve trigger and must not be swallowed
        # by the on-disk cache (Bug 1 fix on #1496). The downstream
        # ``downloader.download_package`` rmtrees and re-clones the
        # install path when the resolved tag changes, so refetching is
        # safe.
        if install_path.exists():
            _force_semver_resolve = (
                update_refs
                and not dep_ref.is_local
                and not getattr(dep_ref, "artifactory_prefix", None)
                and getattr(dep_ref, "ref_kind", None) == "semver"
            )
            if not _force_semver_resolve:
                return install_path
        staging_session.prepare_path(install_path)
        # F1 (#1116): surface a heartbeat BEFORE the network/copy work so
        # users see the install advancing past silent transitive lookups.
        # Under F7's parallel BFS this callback may run on a worker
        # thread, so serialise the emission via ``callback_lock`` to
        # keep heartbeat lines from interleaving with each other.
        # Workstream B (#1116): when the shared InstallTui is painting
        # the Live region, the static heartbeat line would interleave
        # with the spinner -- route the heartbeat to the TUI's
        # task_started instead and skip the static line.
        if logger:
            with callback_lock:
                _display = dep_ref.get_display_name()
                _tui = getattr(ctx, "tui", None)
                if _tui is not None:
                    _tui.task_started(dep_ref.get_unique_key(), f"resolve {_display}")
                if _tui is None or not _tui.is_animating():
                    logger.resolving_heartbeat(_display)
        try:
            # ─── Registry-sourced dep (design §8) ──────────────────────
            # Routed before local/git so the registry resolver owns the
            # download for source=="registry" entries. Lockfile re-installs
            # may arrive with registry_name=None — look it up by URL prefix
            # against the configured registries map.
            if dep_ref.source == "registry":
                from apm_cli.deps.registry.feature_gate import (
                    require_package_registry_enabled,
                )

                require_package_registry_enabled("Registry-sourced downloads")

                if registry_resolver is None:
                    raise RuntimeError(
                        f"dep {dep_ref.repo_url!r} is registry-sourced but no "
                        f"registries: block is configured in apm.yml and the "
                        f"lockfile carries no resolved_url for it."
                    )
                dep_ref = _apply_lockfile_registry_name(
                    dep_ref,
                    registries_map,
                    existing_lockfile=existing_lockfile,
                )
                # Registry T5: honor lockfile on apm install (mirrors git T5
                # at lines below). When the lockfile has full replay data and
                # the manifest range still covers the locked version, fetch
                # from the locked URL and verify against the locked hash
                # (npm install model — no /versions API call).
                _locked_reg = (
                    existing_lockfile.get_dependency(dep_ref.get_unique_key())
                    if existing_lockfile
                    else None
                )
                if (
                    not update_refs
                    and _locked_reg
                    and _locked_reg.resolved_url
                    and _locked_reg.resolved_hash
                    and _locked_reg.version
                ):
                    from apm_cli.drift import detect_ref_change as _detect_ref_change

                    if not _detect_ref_change(dep_ref, _locked_reg, update_refs=False):
                        registry_resolver.download_from_lockfile(
                            dep_ref,
                            install_path,
                            resolved_url=_locked_reg.resolved_url,
                            resolved_hash=_locked_reg.resolved_hash,
                            version=_locked_reg.version,
                        )
                        callback_downloaded[dep_ref.get_unique_key()] = None
                        return install_path
                registry_resolver.download_package(dep_ref, install_path)
                _annotate_registry_dep_ref(dep_ref, registry_resolver)
                # Mark as already-downloaded so the parallel pre-download
                # phase skips this dep. No SHA for registry deps.
                callback_downloaded[dep_ref.get_unique_key()] = None
                return install_path

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
                    with callback_lock:
                        callback_failures.add(dep_ref.get_unique_key())
                    _tui = getattr(ctx, "tui", None)
                    if _tui is not None:
                        _tui.task_failed(dep_ref.get_unique_key())
                    return None
                # Anchor relative paths on the *declaring* package's source
                # directory when available (#857). Falls back to project_root
                # for direct deps and for parents that predate source_path.
                # Direct deps from the root project anchor at ``source_root``
                # (which equals ``project_root`` unless ``apm install --root``
                # redirects writes -- then it stays at $PWD).  Transitive
                # deps from a parent local package anchor at that package's
                # source_path, which is already an absolute path and not
                # affected by ``--root``.
                base_dir = (
                    parent_pkg.source_path
                    if parent_pkg is not None and parent_pkg.source_path is not None
                    else source_root
                )
                result_path = _copy_local_package(
                    dep_ref,
                    install_path,
                    base_dir,
                    project_root=project_root,
                    logger=logger,
                )
                if result_path:
                    with callback_lock:
                        callback_downloaded[dep_ref.get_unique_key()] = None
                    _tui = getattr(ctx, "tui", None)
                    if _tui is not None:
                        _tui.task_completed(dep_ref.get_unique_key())
                    return result_path
                _tui = getattr(ctx, "tui", None)
                if _tui is not None:
                    _tui.task_failed(dep_ref.get_unique_key())
                return None

            # --- Git-source semver range resolution (issue #1488) ---
            # When the manifest carries a semver range as ``ref:`` and
            # the dep is non-local, non-registry, and non-proxy, resolve
            # it to a concrete tag BEFORE any git operation. The result
            # is stashed on ctx so install/sources.py can plumb it into
            # the lockfile, and the dep_ref's ``reference`` is replaced
            # with the concrete tag so build_download_ref / clone use a
            # literal git ref.
            _semver_resolution = _maybe_resolve_git_semver(
                dep_ref=dep_ref,
                existing_lockfile=existing_lockfile,
                update_refs=update_refs,
                auth_resolver=ctx.auth_resolver,
                ref_resolver_cache=ctx.ref_resolver_cache,
                ref_resolver_cache_lock=callback_lock,
            )
            if _semver_resolution is not None:
                with callback_lock:
                    ctx.git_semver_resolutions[dep_ref.get_unique_key()] = _semver_resolution
                # Rewrite the dep_ref's ref to the concrete tag so the
                # rest of the pipeline (drift detection, download, etc.)
                # operates on a literal git ref. The original constraint
                # is preserved in the resolution dataclass.
                dep_ref.reference = _semver_resolution.resolved_tag

            # T5: Use locked commit for reproducibility, unless the manifest
            # ref has drifted from what the lockfile recorded (spec drift).
            _locked_dep = (
                existing_lockfile.get_dependency(dep_ref.get_unique_key())
                if existing_lockfile
                else None
            )
            _ref_changed = detect_ref_change(dep_ref, _locked_dep, update_refs=update_refs)

            # When ref drifts, signal downstream that a content-hash change
            # is expected so the supply-chain check in sources.py doesn't
            # treat a legitimate re-resolution as an attack.
            if _ref_changed:
                with callback_lock:
                    ctx.expected_hash_change_deps.add(dep_ref.get_unique_key())
                if logger:
                    _old = (
                        _locked_dep.resolved_ref or _locked_dep.resolved_commit[:8]
                        if _locked_dep
                        else "?"
                    )
                    _new = dep_ref.reference or "HEAD"
                    logger.verbose_detail(
                        f"  [!] Spec drift: {dep_ref.get_unique_key()} "
                        f"{_old} -> {_new}, re-resolving"
                    )

            download_dep = build_download_ref(
                dep_ref,
                existing_lockfile,
                update_refs=update_refs,
                ref_changed=_ref_changed,
            )

            # Silent download - no progress display for transitive deps
            result = downloader.download_package(download_dep, install_path)
            # Capture resolved commit SHA for lockfile
            resolved_sha = None
            if result and hasattr(result, "resolved_reference") and result.resolved_reference:
                resolved_sha = result.resolved_reference.resolved_commit
            callback_downloaded_value = resolved_sha
            with callback_lock:
                callback_downloaded[dep_ref.get_unique_key()] = callback_downloaded_value
            _tui = getattr(ctx, "tui", None)
            if _tui is not None:
                _tui.task_completed(dep_ref.get_unique_key())
            return install_path
        except Exception as e:
            dep_display = dep_ref.get_display_name()
            dep_key = dep_ref.get_unique_key()
            is_direct = dep_key in direct_dep_keys

            # Distinguish resolution failures (git-semver no-match) from
            # download failures: the dep_ref was rewritten to a concrete
            # tag BEFORE clone, so a NoMatchingTagError means we never
            # got to the download step. Using "download" as the verb
            # would mislead users who are debugging an unsatisfied
            # constraint -- nothing was downloaded yet.
            from apm_cli.deps.git_semver_resolver import NoMatchingTagError
            from apm_cli.models.dependency.reference import InvalidSemverRangeError

            if isinstance(e, InvalidSemverRangeError):
                if is_direct:
                    fail_msg = f"Invalid dependency spec for {dep_ref.repo_url}: {e}"
                else:
                    chain_hint = f" (via {parent_chain})" if parent_chain else ""
                    fail_msg = (
                        f"Invalid dependency spec for transitive dep "
                        f"{dep_ref.repo_url}{chain_hint}: {e}"
                    )
            elif isinstance(e, NoMatchingTagError):
                if is_direct:
                    fail_msg = f"No matching tag for {dep_ref.repo_url}: {e}"
                else:
                    chain_hint = f" (via {parent_chain})" if parent_chain else ""
                    fail_msg = (
                        f"No matching tag for transitive dep {dep_ref.repo_url}{chain_hint}: {e}"
                    )
            # Distinguish direct vs transitive failure messages so users
            # don't see a misleading "transitive dep" label for top-level deps.
            elif is_direct:
                fail_msg = f"Failed to download dependency {dep_ref.repo_url}: {e}"
            else:
                chain_hint = f" (via {parent_chain})" if parent_chain else ""
                fail_msg = f"Failed to resolve transitive dep {dep_ref.repo_url}{chain_hint}: {e}"

            # Verbose: inline detail via logger (single output path).
            # Deferred diagnostics below cover the non-logger case.
            # F7 (#1116): single critical section for both the logger
            # emission and the result-recording so concurrent failures
            # don't interleave their lines.
            with callback_lock:
                if logger:
                    logger.verbose_detail(f"  {fail_msg}")
                # Collect for deferred diagnostics summary (always, even non-verbose)
                callback_failures.add(dep_key)
                transitive_failures.append((dep_display, fail_msg))
            _tui = getattr(ctx, "tui", None)
            if _tui is not None:
                _tui.task_failed(dep_key)
            return None

    # ------------------------------------------------------------------
    # 6. Resolver creation + dependency resolution
    # ------------------------------------------------------------------
    if update_refs:
        _purge_cached_semver_paths_for_update(
            all_apm_deps=ctx.all_apm_deps,
            apm_modules_dir=ctx.apm_modules_dir,
            logger=ctx.logger,
            staging_session=staging_session,
        )

    resolver = APMDependencyResolver(
        apm_modules_dir=ctx.apm_modules_dir,
        download_callback=download_callback,
        auth_resolver=ctx.auth_resolver,
        update_refs=update_refs,
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
    seed_ref_resolver_from_lockfile(ctx)
    _resolve_dependencies(ctx, resolution_for_context(ctx))
    if ctx.only_packages:
        _apply_only_filter(ctx)
    _compute_intended_dep_keys(ctx)
