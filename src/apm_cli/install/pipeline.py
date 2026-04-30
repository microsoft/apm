"""Install pipeline orchestrator.

Extracted from ``apm_cli.commands.install._install_apm_dependencies``
(refactor F2) to keep the Click command module under ~1 000 LOC and
concentrate the phase-call sequence in one import-safe module.

The function ``run_install_pipeline(...)`` is the public entry point.
``commands/install.py`` re-exports it as ``_install_apm_dependencies``
so that every existing ``@patch("apm_cli.commands.install._install_apm_dependencies")``
keeps working without test changes.

Design notes
------------
* Each phase is called via its ``run(ctx)`` entry point.
* Diagnostics, registry config, and managed_files are set up here and
  attached to :class:`InstallContext` *before* the phases that need them.
* Symbols on the ``commands/install`` module that phases access via
  ``_install_mod.X`` stay as re-exports there -- this module does NOT
  duplicate those re-exports.
"""

from __future__ import annotations

import builtins
import sys
from typing import TYPE_CHECKING, List, Optional  # noqa: F401, UP035

from ..models.results import InstallResult
from ..utils.console import _rich_error
from ..utils.diagnostics import DiagnosticCollector
from ..utils.path_security import PathTraversalError
from .errors import AuthenticationError, DirectDependencyError, PolicyViolationError  # noqa: F401

if TYPE_CHECKING:
    from ..core.auth import AuthResolver
    from ..core.command_logger import InstallLogger


# CRITICAL: Shadow Python builtins that share names with Click commands.
# The parent ``commands/install`` module does this; we must do the same
# to avoid NameError when using ``set()``, ``list()``, ``dict()`` below.
set = builtins.set
list = builtins.list
dict = builtins.dict


def _preflight_auth_check(ctx, auth_resolver, verbose: bool) -> None:
    """Verify auth for every distinct (host, org) before write phases.

    Called only when ``update_refs`` is set, so we know the pipeline is
    about to overwrite ``apm.yml``, ``apm.lock.yaml``, and
    ``apm_modules/``.  A single ``git ls-remote`` per cluster catches
    stale tokens before any file is touched.

    Raises :class:`AuthenticationError` (with ``build_error_context``
    payload) on the first auth failure.
    """
    import os
    import subprocess as _sp

    from ..utils.github_host import is_github_hostname

    seen: builtins.set = builtins.set()
    for dep in ctx.deps_to_install:
        host = dep.host
        if not host or is_github_hostname(host):
            continue  # github.com uses API probe with unauth fallback
        org = dep.repo_url.split("/")[0] if dep.repo_url and "/" in dep.repo_url else None
        key = (host, org)
        if key in seen:
            continue
        seen.add(key)

        dep_ctx = auth_resolver.resolve_for_dep(dep)
        _auth_scheme = getattr(dep_ctx, "auth_scheme", "basic") or "basic"

        from ..deps.github_downloader import GitHubPackageDownloader

        _dl = GitHubPackageDownloader(auth_resolver=auth_resolver)
        _dl.github_host = host
        probe_url = _dl._build_repo_url(
            dep.repo_url,
            use_ssh=False,
            dep_ref=dep,
            token=dep_ctx.token,
            auth_scheme=_auth_scheme,
        )
        _ctx_env = getattr(dep_ctx, "git_env", {}) or {}
        probe_env = {**os.environ, **_dl.git_env, **_ctx_env}

        try:
            result = _sp.run(
                ["git", "ls-remote", "--heads", "--exit-code", probe_url],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                env=probe_env,
            )
        except _sp.TimeoutExpired:
            continue  # network timeout is not auth -- let the real phase handle it

        if result.returncode != 0:
            _stderr = (result.stderr or "").lower()
            _auth_signals = (
                "401" in _stderr
                or "403" in _stderr
                or "authentication failed" in _stderr
                or "unauthorized" in _stderr
                or "could not read username" in _stderr
            )
            if _auth_signals:
                _diag = auth_resolver.build_error_context(
                    host,
                    "install --update",
                    org=org,
                    dep_url=dep.repo_url,
                )
                raise AuthenticationError(
                    f"Authentication failed for {host}",
                    diagnostic_context=(
                        _diag
                        + "\n\n    No files were modified."
                        + "\n    apm.yml, apm.lock.yaml, and apm_modules/ are unchanged."
                    ),
                )


def run_install_pipeline(  # noqa: PLR0913, RUF100
    apm_package: APMPackage,
    update_refs: bool = False,
    verbose: bool = False,
    only_packages: builtins.list = None,  # noqa: RUF013
    force: bool = False,
    parallel_downloads: int = 4,
    logger: InstallLogger = None,
    scope=None,
    auth_resolver: AuthResolver = None,
    target: str = None,  # noqa: RUF013
    allow_insecure: bool = False,
    allow_insecure_hosts=(),
    marketplace_provenance: dict = None,
    protocol_pref=None,
    allow_protocol_fallback: bool | None = None,
    no_policy: bool = False,
    skill_subset: tuple | None = None,
    skill_subset_from_cli: bool = False,
):
    """Install APM package dependencies.

    This is the main orchestrator for the install pipeline.  It builds an
    :class:`InstallContext`, then calls each phase module in order:

    1. **resolve** -- dependency resolution + lockfile check
    2. **targets** -- target detection + integrator initialization
    3. **download** -- parallel package pre-download
    4. **integrate** -- sequential integration loop + root primitives
    5. **cleanup** -- orphan cleanup + intra-package stale-file removal
    6. **lockfile** -- generate ``apm.lock``
    7. **finalize** -- emit stats, return :class:`InstallResult`

    Args:
        apm_package: Parsed APM package with dependencies
        update_refs: Whether to update existing packages to latest refs
        verbose: Show detailed installation information
        only_packages: If provided, only install these specific packages
        force: Whether to overwrite locally-authored files on collision
        parallel_downloads: Max concurrent downloads (0 disables parallelism)
        logger: InstallLogger for structured output
        scope: InstallScope controlling project vs user deployment
        auth_resolver: Shared auth resolver for caching credentials
        target: Explicit target override from --target CLI flag
        allow_insecure: Whether direct HTTP dependencies are approved
        allow_insecure_hosts: Extra approved hosts for transitive HTTP dependencies
        marketplace_provenance: Marketplace provenance data for packages
    """
    # Late import: the ``APM_DEPS_AVAILABLE`` guard in commands/install.py
    # already prevents callers from reaching here when deps are missing, but
    # keep the check as a defensive belt-and-suspenders measure.
    try:
        from ..deps.lockfile import LockFile, get_lockfile_path
    except ImportError:
        raise RuntimeError("APM dependency system not available")  # noqa: B904

    from ..core.scope import InstallScope, get_apm_dir, get_deploy_root

    if scope is None:
        scope = InstallScope.PROJECT

    apm_deps = apm_package.get_apm_dependencies()
    dev_apm_deps = apm_package.get_dev_apm_dependencies()
    all_apm_deps = apm_deps + dev_apm_deps

    project_root = get_deploy_root(scope)
    apm_dir = get_apm_dir(scope)

    # Check whether the project root itself has local .apm/ primitives (#714).
    from apm_cli.install.phases.local_content import _project_has_root_primitives

    _root_has_local_primitives = _project_has_root_primitives(project_root)

    # Read old local deployed files from the existing lockfile so the
    # post-deps-local phase can run stale cleanup even when no current
    # local content exists (e.g. .apm/ was deleted but old files remain).
    _old_local_deployed: builtins.list = []
    _early_lockfile = LockFile.read(get_lockfile_path(apm_dir)) if apm_dir else None
    if _early_lockfile:
        _old_local_deployed = builtins.list(_early_lockfile.local_deployed_files)

    if not all_apm_deps and not _root_has_local_primitives and not _old_local_deployed:
        return InstallResult()

    # ------------------------------------------------------------------
    # Build InstallContext from function args + computed state
    # ------------------------------------------------------------------
    from .context import InstallContext

    ctx = InstallContext(
        project_root=project_root,
        apm_dir=apm_dir,
        apm_package=apm_package,
        update_refs=update_refs,
        verbose=verbose,
        only_packages=only_packages,
        force=force,
        parallel_downloads=parallel_downloads,
        logger=logger,
        scope=scope,
        auth_resolver=auth_resolver,
        target_override=target,
        allow_insecure=allow_insecure,
        allow_insecure_hosts=allow_insecure_hosts,
        marketplace_provenance=marketplace_provenance,
        protocol_pref=protocol_pref,
        allow_protocol_fallback=allow_protocol_fallback,
        all_apm_deps=all_apm_deps,
        root_has_local_primitives=_root_has_local_primitives,
        old_local_deployed=_old_local_deployed,
        no_policy=no_policy,
        skill_subset=skill_subset,
        skill_subset_from_cli=skill_subset_from_cli,
        early_lockfile=_early_lockfile,
    )

    # ------------------------------------------------------------------
    # Phase 1: Resolve dependencies
    # ------------------------------------------------------------------
    from .phases import resolve as _resolve_phase

    _resolve_phase.run(ctx)

    if not ctx.deps_to_install and not ctx.root_has_local_primitives:
        if logger:
            logger.nothing_to_install()
        return InstallResult()

    try:
        # --------------------------------------------------------------
        # Phase 1.5: Policy enforcement gate (#827)
        # Runs after resolve (deps_to_install populated) and before
        # targets (denied deps never reach integration).
        # PolicyViolationError halts the pipeline cleanly.
        # --------------------------------------------------------------

        # Populate direct MCP deps from the manifest so the policy gate
        # can enforce MCP allow/deny rules on them (S2 fix).
        ctx.direct_mcp_deps = apm_package.get_mcp_dependencies()

        from .phases import policy_gate as _policy_gate_phase
        from .phases.policy_gate import PolicyViolationError

        try:
            _policy_gate_phase.run(ctx)
        except PolicyViolationError:
            raise  # re-raise through the outer except -> RuntimeError wrapper

        # --------------------------------------------------------------
        # Phase 2: Target detection + integrator initialization
        # --------------------------------------------------------------
        from .phases import targets as _targets_phase

        _targets_phase.run(ctx)

        # --------------------------------------------------------------
        # Phase 2.5: Post-targets target-aware policy check (#827)
        # Target/compilation policy rules need the effective target
        # which is only known after targets.run().  Dependency checks
        # already ran in policy_gate; this phase filters to
        # compilation-target checks only.
        # PolicyViolationError halts the pipeline cleanly.
        # --------------------------------------------------------------
        from .phases import policy_target_check as _policy_target_check_phase

        try:
            _policy_target_check_phase.run(ctx)
        except PolicyViolationError:
            raise  # re-raise through the outer except -> RuntimeError wrapper

        # --------------------------------------------------------------
        # Phase 1.75: Auth pre-flight for --update mode (#1015)
        # When update_refs is set we are about to overwrite apm.yml,
        # apm.lock.yaml, and apm_modules/. If any remote host rejects
        # auth we must abort BEFORE any write phase to avoid partial
        # file corruption. One git ls-remote per distinct (host, org).
        # --------------------------------------------------------------
        if update_refs and ctx.deps_to_install:
            # Use ctx.auth_resolver: resolve phase guarantees it is set
            # (resolve.py:91-92), whereas the local ``auth_resolver``
            # parameter can still be None for callers that omit it.
            _preflight_auth_check(ctx, ctx.auth_resolver, verbose)

        # --------------------------------------------------------------
        # Seam: read phase outputs into locals for remaining code.
        # This minimises diff below -- subsequent phases (download,
        # integrate, cleanup, lockfile) continue using bare-name locals.
        # Future S-phases will fold them into the context one by one.
        # --------------------------------------------------------------
        transitive_failures = ctx.transitive_failures

        # Reuse the logger's DiagnosticCollector when available so that
        # diagnostics recorded earlier in the pipeline (e.g. warn-mode
        # policy violations pushed by ``logger.policy_violation()`` from
        # the policy_gate phase, which runs BEFORE this point) surface
        # in the final install summary.  Block-mode violations also flow
        # through here, but the pipeline aborts via PolicyViolationError
        # before render_summary() runs, so the inline ``[x]`` print is
        # what users see -- no duplication.
        diagnostics = (
            logger.diagnostics if logger is not None else DiagnosticCollector(verbose=verbose)
        )

        # Drain transitive failures collected during resolution into diagnostics
        for dep_display, fail_msg in transitive_failures:
            diagnostics.error(fail_msg, package=dep_display)

        # Collect installed packages for lockfile generation
        from ..deps.installed_package import InstalledPackage
        from ..deps.lockfile import LockFile, get_lockfile_path
        from ..deps.registry_proxy import RegistryConfig

        installed_packages: builtins.list[InstalledPackage] = []

        # Resolve registry proxy configuration once for this install session.
        registry_config = RegistryConfig.from_env()

        # Build managed_files from existing lockfile for collision detection
        managed_files = builtins.set()
        existing_lockfile = LockFile.read(get_lockfile_path(apm_dir)) if apm_dir else None
        if existing_lockfile:
            for dep in existing_lockfile.dependencies.values():
                managed_files.update(dep.deployed_files)

            # Conflict: registry-only mode requires all locked deps to route
            # through the configured proxy. Deps locked to direct VCS sources
            # (github.com, GHE Cloud, GHES) are incompatible.
            if registry_config and registry_config.enforce_only:
                conflicts = registry_config.validate_lockfile_deps(
                    builtins.list(existing_lockfile.dependencies.values())
                )
                if conflicts:
                    _rich_error(
                        "PROXY_REGISTRY_ONLY is set but the lockfile contains "
                        "dependencies locked to direct VCS sources:"
                    )
                    for dep in conflicts[:10]:
                        host = dep.host or "github.com"
                        name = dep.repo_url
                        if dep.virtual_path:
                            name = f"{name}/{dep.virtual_path}"
                        _rich_error(f"  - {name} (host: {host})")
                    _rich_error(
                        "Re-run with 'apm install --update' to re-resolve "
                        "through the registry, or unset PROXY_REGISTRY_ONLY."
                    )
                    sys.exit(1)

            # Supply chain warning: registry-proxy entries without a
            # content_hash cannot be verified on re-install.
            if registry_config and registry_config.enforce_only:
                missing = registry_config.find_missing_hashes(
                    builtins.list(existing_lockfile.dependencies.values())
                )
                if missing:
                    diagnostics.warn(
                        "The following registry-proxy dependencies have no "
                        "content_hash in the lockfile. Run 'apm install "
                        "--update' to populate hashes for tamper detection.",
                        package="lockfile",
                    )
                    for dep in missing[:10]:
                        name = dep.repo_url
                        if dep.virtual_path:
                            name = f"{name}/{dep.virtual_path}"
                        diagnostics.warn(
                            f"  - {name} (host: {dep.host})",
                            package="lockfile",
                        )

        # Normalize path separators once for O(1) lookups in check_collision
        from ..integration.base_integrator import BaseIntegrator

        managed_files = BaseIntegrator.normalize_managed_files(managed_files)

        # --------------------------------------------------------------
        # Phase 4 (#171): Parallel package pre-download
        # --------------------------------------------------------------
        from .phases import download as _download_phase

        _download_phase.run(ctx)

        # --------------------------------------------------------------
        # Phase 5: Sequential integration loop + root primitives
        # --------------------------------------------------------------
        # Populate ctx with locals needed by the integrate phase.
        ctx.diagnostics = diagnostics
        ctx.registry_config = registry_config
        ctx.managed_files = managed_files
        ctx.installed_packages = installed_packages

        from .phases import integrate as _integrate_phase

        _integrate_phase.run(ctx)

        # Fail-loud: if any direct dependency failed validation or
        # download, render the diagnostic summary and raise so the
        # caller exits non-zero immediately.  Transitive failures
        # are allowed to proceed (log + continue).
        if ctx.direct_dep_failed:
            if ctx.diagnostics and ctx.diagnostics.has_diagnostics:
                ctx.diagnostics.render_summary()
            raise DirectDependencyError(
                "One or more direct dependencies failed validation. Run with --verbose for details."
            )

        # Update .gitignore
        from apm_cli.commands._helpers import _update_gitignore_for_apm_modules

        _update_gitignore_for_apm_modules(logger=logger)

        # ------------------------------------------------------------------
        # Phase: Orphan cleanup + intra-package stale-file cleanup
        # All deletions routed through integration/cleanup.py (#762).
        # ------------------------------------------------------------------
        from .phases import cleanup as _cleanup_phase

        _cleanup_phase.run(ctx)

        # Generate apm.lock for reproducible installs (T4: lockfile generation)
        from .phases.lockfile import LockfileBuilder

        LockfileBuilder(ctx).build_and_save()

        # ------------------------------------------------------------------
        # Phase: Post-deps local .apm/ content -- stale cleanup +
        # lockfile persistence for the project's own .apm/ primitives.
        # Runs after the dep lockfile so it can read-modify-write the
        # lockfile with local_deployed_files / hashes.  All deletions
        # routed through integration/cleanup.py (#762).
        # ------------------------------------------------------------------
        from .phases import post_deps_local as _post_deps_local_phase

        _post_deps_local_phase.run(ctx)

        # Emit verbose integration stats + bare-success fallback + return result
        from .phases import finalize as _finalize_phase

        return _finalize_phase.run(ctx)

    except AuthenticationError:
        # #1015: surface auth failures cleanly to the user. Same
        # pattern as PolicyViolationError -- re-raise so the typed
        # exception reaches commands/install.py for rendering with
        # build_error_context diagnostics instead of being wrapped
        # into "Failed to resolve APM dependencies: ...".
        raise
    except PolicyViolationError:
        # #832: surface policy violations cleanly to the user.  The
        # outer ``except Exception`` below would otherwise wrap the
        # message into ``RuntimeError("Failed to resolve APM dependencies:
        # Install blocked by org policy ...")`` and the caller in
        # ``commands/install.py`` would wrap it AGAIN as
        # ``"Failed to install APM dependencies: Failed to resolve APM
        # dependencies: Install blocked by org policy ..."``.  Re-raising
        # the typed exception lets the caller render the policy message
        # as-is.
        raise
    except DirectDependencyError:
        # #946: same pattern -- surface the message as-is instead of
        # double-wrapping it through the generic RuntimeError below.
        raise
    except PathTraversalError:
        # Path-safety violation in SKILL_BUNDLE or other nested
        # resolution -- surface as-is for actionable user guidance.
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to resolve APM dependencies: {e}")  # noqa: B904
