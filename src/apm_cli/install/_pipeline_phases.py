"""Phase-execution block extracted from ``install.pipeline``.

Extracted to keep ``run_install_pipeline`` under 400 LOC.
``_run_phases(ctx)`` is called after the plan-gate checkpoint and owns the
second ``ctx.tui.__enter__()`` / try-finally block.
"""

from __future__ import annotations

import builtins
import sys

from ..models.results import InstallResult
from ..utils.console import _rich_error
from ..utils.diagnostics import DiagnosticCollector
from ..utils.path_security import PathTraversalError
from ._phase_runner import _run_phase
from ._preflight import _preflight_auth_check
from .errors import AuthenticationError, DirectDependencyError

# CRITICAL: Shadow Python builtins that share names with Click commands.
# The parent ``commands/install`` module does this; we must do the same
# to avoid NameError when using ``set()``, ``list()``, ``dict()`` below.
set = builtins.set
list = builtins.list
dict = builtins.dict


def _init_diagnostics(ctx) -> DiagnosticCollector:
    """Initialise a DiagnosticCollector and drain transitive failures into it.

    Reuses ``ctx.logger.diagnostics`` when available so that diagnostics
    recorded earlier in the pipeline (e.g. warn-mode policy violations)
    surface in the final install summary.
    """
    diagnostics = (
        ctx.logger.diagnostics
        if ctx.logger is not None
        else DiagnosticCollector(verbose=ctx.verbose)
    )
    for dep_display, fail_msg in ctx.transitive_failures:
        diagnostics.error(fail_msg, package=dep_display)
    return diagnostics


def _check_registry_enforcement(existing_lockfile, registry_config, diagnostics) -> None:
    """Validate registry-proxy constraints against the current lockfile.

    Terminates the process (``sys.exit(1)``) on a hard conflict, and emits
    diagnostic warnings for missing content hashes.
    """
    if not registry_config or not registry_config.enforce_only:
        return
    # Conflict: registry-only mode requires all locked deps to route through
    # the configured proxy. Deps locked to direct VCS sources are incompatible.
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
    # Supply chain warning: registry-proxy entries without a content_hash
    # cannot be verified on re-install.
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


def _build_phase_environment(ctx, diagnostics):
    """Build the lockfile-aware environment needed by download/integrate phases.

    Reads the existing lockfile (if any), populates *managed_files* from it,
    enforces registry-proxy constraints, and returns the three context objects
    used downstream.

    Returns:
        Tuple of ``(managed_files, registry_config, installed_packages)``.
    """
    from ..deps.installed_package import InstalledPackage
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..deps.registry_proxy import RegistryConfig
    from ..integration.base_integrator import BaseIntegrator

    installed_packages: builtins.list[InstalledPackage] = []
    registry_config = RegistryConfig.from_env()
    managed_files: builtins.set[str] = builtins.set()
    existing_lockfile = LockFile.read(get_lockfile_path(ctx.apm_dir)) if ctx.apm_dir else None
    if existing_lockfile:
        for dep in existing_lockfile.dependencies.values():
            managed_files.update(dep.deployed_files)
        _check_registry_enforcement(existing_lockfile, registry_config, diagnostics)

    # Normalize path separators once for O(1) lookups in check_collision
    managed_files = BaseIntegrator.normalize_managed_files(managed_files)
    return managed_files, registry_config, installed_packages


def _run_direct_dep_check(ctx) -> None:
    """Raise DirectDependencyError if any direct dependency failed validation."""
    if not ctx.direct_dep_failed:
        return
    if ctx.diagnostics and ctx.diagnostics.has_diagnostics:
        ctx.diagnostics.render_summary()
    raise DirectDependencyError(
        "One or more direct dependencies failed validation. Run with --verbose for details."
    )


def _maybe_migrate_skill_paths(ctx) -> None:
    """Run skill path auto-migration when conditions are met."""
    if not ctx.legacy_skill_paths and ctx.existing_lockfile and not ctx.dry_run:
        from .skill_path_migration import run_skill_migration

        run_skill_migration(ctx)


def _run_phases(ctx) -> InstallResult:
    """Execute all install phases after the plan-gate checkpoint.

    Owns the second ``ctx.tui.__enter__()`` block and its try/finally.
    All outer-scope locals are read from ``ctx`` (apm_package, update_refs,
    verbose, logger, apm_dir).

    Args:
        ctx: :class:`~apm_cli.install.context.InstallContext` already
             populated by the resolve phase.
    """
    ctx.tui.__enter__()
    try:
        # Phase 1.5: Policy enforcement gate (#827)
        # Runs after resolve (deps_to_install populated) and before targets.
        ctx.direct_mcp_deps = ctx.apm_package.get_mcp_dependencies()
        from .phases import policy_gate as _policy_gate_phase
        from .phases.policy_gate import PolicyViolationError

        try:
            _run_phase("policy_gate", _policy_gate_phase, ctx)
        except PolicyViolationError:
            raise  # re-raise through the outer except -> RuntimeError wrapper

        # Phase 2: Target detection + integrator initialization
        from .phases import targets as _targets_phase

        _run_phase("targets", _targets_phase, ctx)

        # Phase 2.5: Post-targets target-aware policy check (#827)
        from .phases import policy_target_check as _policy_target_check_phase

        try:
            _run_phase("policy_target_check", _policy_target_check_phase, ctx)
        except PolicyViolationError:
            raise  # re-raise through the outer except -> RuntimeError wrapper

        # Phase 1.75: Auth pre-flight for --update mode (#1015)
        if ctx.update_refs and ctx.deps_to_install:
            _preflight_auth_check(ctx, ctx.auth_resolver, ctx.verbose)

        # Seam: initialise diagnostics + lockfile-aware environment
        diagnostics = _init_diagnostics(ctx)
        managed_files, registry_config, installed_packages = _build_phase_environment(
            ctx, diagnostics
        )

        # Phase 4 (#171): Parallel package pre-download
        from .phases import download as _download_phase

        ctx.tui.start_phase("download", total=len(ctx.deps_to_install) or 1)
        _run_phase("download", _download_phase, ctx)

        # Phase 5: Sequential integration loop + root primitives
        ctx.diagnostics = diagnostics
        ctx.registry_config = registry_config
        ctx.managed_files = managed_files
        ctx.installed_packages = installed_packages
        from .phases import integrate as _integrate_phase

        ctx.tui.start_phase("integrate", total=len(ctx.deps_to_install) or 1)
        _run_phase("integrate", _integrate_phase, ctx)

        # Fail-loud: if any direct dependency failed validation or download,
        # render the diagnostic summary and raise so the caller exits non-zero.
        _run_direct_dep_check(ctx)

        # Update .gitignore
        from apm_cli.commands._helpers import _update_gitignore_for_apm_modules

        _update_gitignore_for_apm_modules(logger=ctx.logger)

        # Phase: Orphan cleanup + intra-package stale-file cleanup (#762)
        from .phases import cleanup as _cleanup_phase

        _run_phase("cleanup", _cleanup_phase, ctx)

        # Phase: Skill path auto-migration (#737) — skip when opt-out flag set
        _maybe_migrate_skill_paths(ctx)

        # Generate apm.lock for reproducible installs (T4: lockfile generation)
        from .phases.lockfile import LockfileBuilder

        LockfileBuilder(ctx).build_and_save()

        # Phase: Post-deps local .apm/ content (#762)
        from .phases import post_deps_local as _post_deps_local_phase

        _run_phase("post_deps_local", _post_deps_local_phase, ctx)

        # Emit verbose integration stats + bare-success fallback + return result
        from .phases import finalize as _finalize_phase

        return _run_phase("finalize", _finalize_phase, ctx)

    except (AuthenticationError, PolicyViolationError, DirectDependencyError, PathTraversalError):
        # Surface typed exceptions as-is so callers render the right message
        # instead of double-wrapping via the generic RuntimeError below.
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to resolve APM dependencies: {e}")  # noqa: B904
    finally:
        ctx.tui.__exit__()
