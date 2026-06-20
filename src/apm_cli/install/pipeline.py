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

Source-vs-deploy root convention
--------------------------------
:class:`InstallContext` carries two roots; phases must pick the
correct one or ``apm install --root`` silently produces wrong paths
(the bug surfaces only when ``project_root != source_root``).

* ``ctx.source_root`` -- read sources here (``apm.yml``, ``.apm/``
  primitives, local-path packages).  Equal to ``$PWD`` regardless of
  ``--root``.
* ``ctx.project_root`` / ``ctx.apm_dir`` -- write deploy artifacts
  here (``apm_modules/``, ``apm.lock.yaml``, ``.claude/``, ``.codex/``,
  etc.).  Becomes the ``--root`` target when set.

Convention: a phase that *reads* an existing project file uses
``source_root``; a phase that *writes* anything uses ``project_root``
(or the helper that already does -- e.g. :func:`get_apm_dir`).  When
a new field is added to :class:`InstallContext`, the source-vs-write
side must be an explicit, documented choice -- not implicit.
"""

from __future__ import annotations

import builtins
import contextlib
import sys
import time
from typing import TYPE_CHECKING

from ..models.results import InstallResult
from ..utils.console import _rich_error
from ..utils.diagnostics import DiagnosticCollector
from ..utils.path_security import PathTraversalError
from .errors import AuthenticationError, DirectDependencyError
from .pipeline_preflight import _preflight_auth_check

if TYPE_CHECKING:
    from ..core.auth import AuthResolver
    from ..core.command_logger import InstallLogger


# CRITICAL: Shadow Python builtins that share names with Click commands.
# The parent ``commands/install`` module does this; we must do the same
# to avoid NameError when using ``set()``, ``list()``, ``dict()`` below.
set = builtins.set
list = builtins.list
dict = builtins.dict


def _run_phase(name: str, phase, ctx):
    """Invoke ``phase.run(ctx)`` with verbose-only timing (F6, #1116).

    Returns whatever ``phase.run(ctx)`` returns (most phases return
    ``None``; ``finalize`` returns the :class:`InstallResult`).

    Best-effort: any failure to render the timing line is swallowed so
    it cannot mask the phase's own exception. The phase exception
    propagates after the timing attempt.

    Verbose mode shows ``[i] Phase: <name> -> 1.234s`` so users (and
    CI logs) can locate the phase responsible for a slow install
    without instrumenting individual sources.
    """
    logger = getattr(ctx, "logger", None)
    verbose = bool(getattr(ctx, "verbose", False))
    if not verbose or logger is None:
        return phase.run(ctx)
    started = time.perf_counter()
    try:
        return phase.run(ctx)
    finally:
        elapsed = time.perf_counter() - started
        with contextlib.suppress(Exception):
            logger.verbose_detail(f"Phase: {name} -> {elapsed:.3f}s")


def _enforce_require_hashes(ctx) -> None:
    """Fail closed when ``security.integrity.require_hashes`` is enabled.

    Reads the freshly-written lockfile and asserts every non-local entry has a
    content hash. No-op when policy is disabled (``--no-policy``), no policy was
    resolved, or the key is off -- preserving today's default behavior. Raises
    :class:`~apm_cli.install.phases.policy_gate.PolicyViolationError` so the
    failure routes through the pipeline's existing policy-violation handling.
    """
    if getattr(ctx, "no_policy", False):
        return
    policy_fetch = getattr(ctx, "policy_fetch", None)
    policy = getattr(policy_fetch, "policy", None) if policy_fetch else None
    if policy is None:
        return
    if not policy.security.integrity.require_hashes:
        return

    from ..deps.lockfile import LockFile, get_lockfile_path
    from .integrity import enforce_require_hashes
    from .phases.policy_gate import PolicyViolationError

    apm_dir = getattr(ctx, "apm_dir", None) or ctx.project_root
    lockfile_path = get_lockfile_path(apm_dir)
    lockfile = LockFile.read(lockfile_path)
    if lockfile is None:
        # Fail closed: require_hashes is on but the freshly-written lockfile is
        # missing or unreadable. Returning here would silently defeat the gate,
        # so surface it as a policy violation instead of letting install pass.
        raise PolicyViolationError(
            "security.integrity.require_hashes is enabled but the lockfile at "
            f"{lockfile_path} could not be read (missing or corrupt); "
            "failing closed. Re-run 'apm install' to regenerate it."
        )
    try:
        enforce_require_hashes(lockfile.get_package_dependencies(), enabled=True)
    except RuntimeError as exc:
        raise PolicyViolationError(str(exc)) from exc


def _write_empty_lockfile_only(apm_dir: Path) -> None:
    """Materialise an empty ``apm.lock.yaml`` for a depless ``apm lock`` run.

    ``apm lock`` promises to always produce a lockfile, even when the
    project declares zero dependencies (mirroring ``cargo
    generate-lockfile``). The write is skipped when an equivalent
    lockfile already exists so repeat runs don't churn ``generated_at``.
    """
    from ..deps.lockfile import LockFile, get_lockfile_path

    lock_path = get_lockfile_path(apm_dir)
    new_lock = LockFile.from_installed_packages([], None)
    existing_lock = LockFile.read(lock_path) if lock_path.exists() else None
    if not (existing_lock and new_lock.is_semantically_equivalent(existing_lock)):
        new_lock.save(lock_path)


def _is_no_work_install(
    *,
    all_apm_deps,
    root_has_local_primitives: bool,
    old_local_deployed,
    has_orphan_deps: bool,
    lockfile_only: bool,
    apm_dir: Path | None,
) -> bool:
    """Return True when there is genuinely no install/cleanup work to do.

    In ``lockfile_only`` mode (``apm lock``) an empty lockfile is written
    before returning so the command always materialises its artefact.
    """
    if all_apm_deps or root_has_local_primitives or old_local_deployed or has_orphan_deps:
        return False
    if lockfile_only and apm_dir:
        _write_empty_lockfile_only(apm_dir)
    return True


def _read_early_lockfile_state(lockfile_cls, get_path, apm_dir):
    """Read prior local-deployed files + orphan-dep flag from the lockfile.

    Returns ``(early_lockfile, old_local_deployed, has_orphan_deps)``.  The
    orphan flag lets the cleanup phase run even when the user removed every
    dependency from apm.yml.
    """
    from apm_cli.deps.lockfile import _SELF_KEY

    old_local_deployed: builtins.list = []
    early_lockfile = lockfile_cls.read(get_path(apm_dir)) if apm_dir else None
    if early_lockfile:
        old_local_deployed = builtins.list(early_lockfile.local_deployed_files)
    has_orphan_deps = bool(
        early_lockfile and any(k != _SELF_KEY for k in early_lockfile.dependencies)
    )
    return early_lockfile, old_local_deployed, has_orphan_deps


def _resolve_managed_files(apm_dir, diagnostics):
    """Seed managed-files set + resolve registry-proxy config.

    Reads the existing lockfile to seed ``managed_files`` for collision
    detection, enforces the PROXY_REGISTRY_ONLY conflict + missing-hash
    rules, and normalises path separators.  Returns
    ``(managed_files, registry_config)``.
    """
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..deps.registry_proxy import RegistryConfig

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
    return managed_files, registry_config


def _run_skill_path_migration(ctx) -> None:
    """Auto-migrate legacy skill deployments (#737); no-op when disabled.

    Mirrors the previous inline block: skips entirely unless a prior
    lockfile exists and skill-path migration is in effect, then either
    reports collisions (error) or executes the migration and summarises
    the result.
    """
    if ctx.legacy_skill_paths or not ctx.existing_lockfile or ctx.dry_run or ctx.lockfile_only:
        return
    from apm_cli.utils.console import _rich_info, _rich_warning

    from .skill_path_migration import (
        COLLISION_HEADER_TEMPLATE,
        COLLISION_HINT,
        MIGRATION_SUMMARY_TEMPLATE,
        check_collisions,
        detect_legacy_skill_deployments,
        execute_migration,
    )

    _migration_plans = detect_legacy_skill_deployments(ctx.existing_lockfile, ctx.project_root)
    if _migration_plans:
        _collisions = check_collisions(_migration_plans, ctx.project_root)
        if _collisions:
            # H2: collision is an error, not a warning.
            _rich_error(
                COLLISION_HEADER_TEMPLATE.format(count=len(_collisions)),
                symbol="error",
            )
            for _c in _collisions:
                _rich_error(f"  {_c}", symbol="error")
            # H5: actionable next-step hint.
            _rich_info(COLLISION_HINT, symbol="info")
            # H2: surface via DiagnosticCollector.
            if ctx.diagnostics:
                for _c in _collisions:
                    ctx.diagnostics.error(
                        f"Skill migration collision: {_c}",
                        package="skill-path-migration",
                    )
        else:
            _migration_result = execute_migration(
                _migration_plans, ctx.existing_lockfile, ctx.project_root
            )
            _total = len(_migration_result.deleted) + len(_migration_result.skipped_no_file)
            if _total > 0:
                # H3: suppress info when quiet.
                if not (ctx.logger and getattr(ctx.logger, "_quiet", False)):
                    _rich_info(
                        MIGRATION_SUMMARY_TEMPLATE.format(count=_total),
                        symbol="info",
                    )
                # H4: enumerate deleted paths when verbose.
                if ctx.verbose and _migration_result.deleted:
                    for _dp in _migration_result.deleted:
                        _rich_info(f"  removed {_dp}", symbol="info")
            if _migration_result.failed:
                _rich_warning(
                    f"  {len(_migration_result.failed)} file(s) could not be deleted (will retry next install)",
                    symbol="warning",
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
    audit_override: str | None = None,
    skill_subset: tuple | None = None,
    skill_subset_from_cli: bool = False,
    legacy_skill_paths: bool = False,
    plan_callback=None,
    refresh: bool = False,
    lockfile_only: bool = False,
    trust_canvas: bool = False,
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

    # Reset process-scoped perf counters and discovery memo so that
    # numbers / cache hits from earlier pipeline runs (tests, REPL,
    # long-lived processes) do not bleed into this install. See #1533.
    from ..primitives.discovery import clear_discovery_cache
    from ..utils import perf_stats as _perf_stats

    _perf_stats.reset()
    clear_discovery_cache()

    from ..core.scope import InstallScope, get_apm_dir, get_deploy_root, get_source_root

    if scope is None:
        scope = InstallScope.PROJECT

    apm_deps = apm_package.get_apm_dependencies()
    dev_apm_deps = apm_package.get_dev_apm_dependencies()
    all_apm_deps = apm_deps + dev_apm_deps

    project_root = get_deploy_root(scope)  # write target
    source_root = get_source_root(scope)  # source reads (apm.yml, .apm/)
    apm_dir = get_apm_dir(scope)

    # Check whether the source root has local .apm/ primitives (#714).
    # Sources resolve from $PWD even when --root redirects writes, so the
    # check uses source_root rather than project_root.
    from apm_cli.install.phases.local_content import _project_has_root_primitives

    _root_has_local_primitives = _project_has_root_primitives(source_root)

    # Read old local deployed files + detect orphan deps from the existing
    # lockfile so the post-deps-local cleanup phase can run even when no
    # current local content exists (e.g. .apm/ deleted but old files remain)
    # or the user removed every dep from apm.yml.
    _early_lockfile, _old_local_deployed, _has_orphan_deps = _read_early_lockfile_state(
        LockFile, get_lockfile_path, apm_dir
    )

    if _is_no_work_install(
        all_apm_deps=all_apm_deps,
        root_has_local_primitives=_root_has_local_primitives,
        old_local_deployed=_old_local_deployed,
        has_orphan_deps=_has_orphan_deps,
        lockfile_only=lockfile_only,
        apm_dir=apm_dir,
    ):
        return InstallResult()

    # ------------------------------------------------------------------
    # Build InstallContext from function args + computed state
    # ------------------------------------------------------------------
    from .context import InstallContext

    ctx = InstallContext(
        project_root=project_root,
        apm_dir=apm_dir,
        source_root=source_root,
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
        audit_override=audit_override,
        skill_subset=skill_subset,
        skill_subset_from_cli=skill_subset_from_cli,
        early_lockfile=_early_lockfile,
        legacy_skill_paths=legacy_skill_paths,
        refresh=refresh,
        lockfile_only=lockfile_only,
        trust_canvas=trust_canvas,
    )

    # ------------------------------------------------------------------
    # Workstream B (#1116): one Live region per major phase boundary.
    # When the controller is disabled (CI, dumb terminal,
    # ``APM_PROGRESS=never``) every method is a no-op so the surrounding
    # phases stay valid without per-call gating.
    # ------------------------------------------------------------------
    from apm_cli.utils.install_tui import InstallTui

    ctx.tui = InstallTui()

    # ------------------------------------------------------------------
    # Phase 1: Resolve dependencies
    # ------------------------------------------------------------------
    from .phases import resolve as _resolve_phase

    ctx.tui.__enter__()
    try:
        ctx.tui.start_phase("resolve", total=len(all_apm_deps) or 1)
        _run_phase("resolve", _resolve_phase, ctx)
    finally:
        ctx.tui.__exit__()

    if not ctx.deps_to_install and not ctx.root_has_local_primitives and not _has_orphan_deps:
        if logger:
            logger.nothing_to_install(
                lockfile_present=_early_lockfile is not None,
                update_mode=update_refs,
            )
        return InstallResult()

    # ------------------------------------------------------------------
    # Plan-gate checkpoint (#1203): show the user what install/update
    # is about to do and let them confirm.  Invoked AFTER resolve so we
    # have ``ctx.deps_to_install`` with resolved refs, BEFORE downloads
    # begin so a "no" answer cancels cleanly without touching the
    # cache.
    #
    # Only ``apm update`` passes a callback today; all other entry
    # points pass ``None`` and the checkpoint is a no-op.  The TUI is
    # already exited (see the ``finally`` above), so callbacks can
    # write directly to stdout / call ``click.confirm`` without
    # collision.
    # ------------------------------------------------------------------
    if plan_callback is not None:
        from .plan import build_update_plan

        plan = build_update_plan(_early_lockfile, ctx.deps_to_install)
        proceed = plan_callback(plan)
        if not proceed:
            return InstallResult()

    ctx.tui.__enter__()
    try:
        # --------------------------------------------------------------
        # Phase 1.5: Policy enforcement gate (#827)
        # Runs after resolve (deps_to_install populated) and before
        # targets (denied deps never reach integration).
        # PolicyViolationError halts the pipeline cleanly.
        # --------------------------------------------------------------

        # Populate direct MCP deps from the manifest so the policy gate
        # can enforce MCP allow/deny rules on them (S2 fix).
        ctx.direct_mcp_deps = apm_package.get_all_mcp_dependencies()

        # Populate direct LSP deps from the manifest for LSP integration.
        ctx.direct_lsp_deps = apm_package.get_lsp_dependencies()

        from .phases import policy_gate as _policy_gate_phase
        from .phases.policy_gate import PolicyViolationError

        try:
            _run_phase("policy_gate", _policy_gate_phase, ctx)
        except PolicyViolationError:
            raise  # re-raise through the outer except -> RuntimeError wrapper

        # --------------------------------------------------------------
        # Phase 2: Target detection + integrator initialization.
        # Skipped in lockfile_only mode -- no primitives are deployed.
        # --------------------------------------------------------------
        if not lockfile_only:
            from .phases import targets as _targets_phase

            _run_phase("targets", _targets_phase, ctx)

        # --------------------------------------------------------------
        # Phase 2.5: Post-targets target-aware policy check (#827)
        # Runs even in lockfile_only mode so that --target policy
        # constraints are enforced during resolution-only runs.
        # --------------------------------------------------------------
        from .phases import policy_target_check as _policy_target_check_phase

        try:
            _run_phase("policy_target_check", _policy_target_check_phase, ctx)
        except PolicyViolationError:
            raise  # re-raise through the outer except -> RuntimeError wrapper

        # --------------------------------------------------------------
        # Phase 1.75: Auth pre-flight for --update mode (#1015)
        # Skipped in lockfile_only mode -- no writes to apm.yml occur.
        # --------------------------------------------------------------
        if update_refs and ctx.deps_to_install and not lockfile_only:
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

        installed_packages: builtins.list[InstalledPackage] = []

        managed_files, registry_config = _resolve_managed_files(apm_dir, diagnostics)

        # --------------------------------------------------------------
        # Phase 4 (#171): Parallel package pre-download
        # --------------------------------------------------------------
        from .phases import download as _download_phase

        ctx.tui.start_phase("download", total=len(ctx.deps_to_install) or 1)
        _run_phase("download", _download_phase, ctx)

        # --------------------------------------------------------------
        # Phase 5: Sequential integration loop + root primitives
        # --------------------------------------------------------------
        # Populate ctx with locals needed by the integrate phase.
        ctx.diagnostics = diagnostics
        ctx.registry_config = registry_config
        ctx.managed_files = managed_files
        ctx.installed_packages = installed_packages

        from .phases import integrate as _integrate_phase

        ctx.tui.start_phase("integrate", total=len(ctx.deps_to_install) or 1)
        _run_phase("integrate", _integrate_phase, ctx)

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

        # Update .gitignore only for project-scoped installs, not in lockfile_only mode.
        if scope == InstallScope.PROJECT and not lockfile_only:
            from apm_cli.commands._helpers import _update_gitignore_for_apm_modules

            _update_gitignore_for_apm_modules(logger=logger)
        elif verbose and logger is not None and not lockfile_only:
            logger.verbose_detail("Skipping .gitignore update (global scope install).")

        # ------------------------------------------------------------------
        # Phase: Orphan cleanup + intra-package stale-file cleanup.
        # Skipped in lockfile_only mode -- no files were deployed.
        # ------------------------------------------------------------------
        if not lockfile_only:
            from .phases import cleanup as _cleanup_phase

            _run_phase("cleanup", _cleanup_phase, ctx)

        # ------------------------------------------------------------------
        # Phase: Skill path auto-migration (#737).
        # Skipped in lockfile_only mode.
        # ------------------------------------------------------------------
        _run_skill_path_migration(ctx)

        # Generate apm.lock for reproducible installs (T4: lockfile generation)
        from .phases.lockfile import LockfileBuilder

        LockfileBuilder(ctx).build_and_save()

        # Fail-closed integrity gate: when security.integrity.require_hashes is
        # on, every non-local lockfile entry must carry a content hash. A
        # missing hash stops the install (the key only asserts hash-presence on
        # the freshly-built lockfile; it does not add a second hashing pass).
        _enforce_require_hashes(ctx)

        # ------------------------------------------------------------------
        # Phase: Post-deps local .apm/ content.
        # Skipped in lockfile_only mode -- no file deployment occurred.
        # ------------------------------------------------------------------
        if not lockfile_only:
            from .phases import post_deps_local as _post_deps_local_phase

            _run_phase("post_deps_local", _post_deps_local_phase, ctx)

        # ------------------------------------------------------------------
        # Phase: Optional install-time content audit (external_scanners flag).
        # Skipped in lockfile_only mode -- no files were deployed.
        # ------------------------------------------------------------------
        if not lockfile_only:
            from .phases import audit as _audit_phase

            try:
                _run_phase("audit", _audit_phase, ctx)
            except PolicyViolationError:
                raise

        # Emit verbose integration stats + bare-success fallback + return result
        from .phases import finalize as _finalize_phase

        _perf_stats.render_summary(logger, project_root=str(ctx.project_root))
        return _run_phase("finalize", _finalize_phase, ctx)

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
    finally:
        ctx.tui.__exit__()
