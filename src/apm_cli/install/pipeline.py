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
* ``_run_phase`` and ``_preflight_auth_check`` live in private sibling
  modules but are re-exported here so existing imports and patches
  (``from apm_cli.install.pipeline import _run_phase``) continue to work.
"""

from __future__ import annotations

import builtins
import sys
from typing import TYPE_CHECKING

from ..models.results import InstallResult
from ..utils.console import _rich_error
from ..utils.diagnostics import DiagnosticCollector
from ..utils.path_security import PathTraversalError
from . import _pipeline_phases
from ._phase_runner import _run_phase  # re-exported for backward compat
from ._preflight import _preflight_auth_check  # re-exported for backward compat
from .errors import AuthenticationError, DirectDependencyError, PolicyViolationError

if TYPE_CHECKING:
    pass


# CRITICAL: Shadow Python builtins that share names with Click commands.
# The parent ``commands/install`` module does this; we must do the same
# to avoid NameError when using ``set()``, ``list()``, ``dict()`` below.
set = builtins.set
list = builtins.list
dict = builtins.dict


def run_install_pipeline(apm_package: APMPackage, **params: object):
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

    update_refs = params.get("update_refs", False)
    verbose = params.get("verbose", False)
    only_packages = params.get("only_packages")
    force = params.get("force", False)
    parallel_downloads = params.get("parallel_downloads", 4)
    logger = params.get("logger")
    scope = params.get("scope")
    auth_resolver = params.get("auth_resolver")
    target = params.get("target")
    allow_insecure = params.get("allow_insecure", False)
    allow_insecure_hosts = params.get("allow_insecure_hosts", ())
    marketplace_provenance = params.get("marketplace_provenance")
    protocol_pref = params.get("protocol_pref")
    allow_protocol_fallback = params.get("allow_protocol_fallback")
    no_policy = params.get("no_policy", False)
    skill_subset = params.get("skill_subset")
    skill_subset_from_cli = params.get("skill_subset_from_cli", False)
    legacy_skill_paths = params.get("legacy_skill_paths", False)
    plan_callback = params.get("plan_callback")
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

    # Detect orphan APM dependencies in the previous lockfile so we don't
    # short-circuit cleanup when the user removed every dep from apm.yml.
    # Without this check, deleting all deps would leave their deployed files
    # behind because the cleanup phase never runs.
    from apm_cli.deps.lockfile import _SELF_KEY

    _has_orphan_deps = bool(
        _early_lockfile and any(k != _SELF_KEY for k in _early_lockfile.dependencies)
    )

    if (
        not all_apm_deps
        and not _root_has_local_primitives
        and not _old_local_deployed
        and not _has_orphan_deps
    ):
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
        legacy_skill_paths=legacy_skill_paths,
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

    try:
        return _pipeline_phases._run_phases(ctx)
    except AuthenticationError:
        raise
    except PolicyViolationError:
        raise
    except DirectDependencyError:
        raise
    except PathTraversalError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to resolve APM dependencies: {e}")  # noqa: B904
