"""Integration template -- shared post-acquire flow for all DependencySources.

After ``DependencySource.acquire()`` materialises a package, every source
funnels through the same template:

1. Pre-deploy security gate (``_pre_deploy_security_scan``).
2. Primitive integration (``integrate_package_primitives``).
3. Per-package verbose diagnostics (skip / error counts).

This is the Template Method companion to the Strategy pattern in
``apm_cli.install.sources``.
"""

from __future__ import annotations

from typing import Dict, Optional

from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan
from apm_cli.install.services import integrate_package_primitives
from apm_cli.install.sources import DependencySource, Materialization


def run_integration_template(
    source: DependencySource,
) -> Optional[Dict[str, int]]:
    """Run the shared post-acquire integration flow for one dependency.

    Returns a counter-delta dict for accumulation by the caller, or
    ``None`` if the source declined to acquire (skipped, failed).
    """
    materialization = source.acquire()
    if materialization is None:
        return None

    return _integrate_materialization(source, materialization)


def _integrate_materialization(
    source: DependencySource,
    m: Materialization,
) -> Dict[str, int]:
    """Apply security gate + primitive integration on a materialised package.

    The caller has already populated ``ctx.installed_packages`` /
    ``ctx.package_hashes`` / ``ctx.package_types`` inside ``acquire()``.
    Here we focus on the deployment side: security scan, primitive
    integration, deployed-files tracking, and per-package diagnostics.
    """
    ctx = source.ctx
    dep_ref = source.dep_ref
    deltas = m.deltas
    install_path = m.install_path
    dep_key = m.dep_key
    diagnostics = ctx.diagnostics
    logger = ctx.logger

    # No-op when targets are empty or acquire decided to skip integration
    # (signalled by package_info=None).  Still record an empty deployed
    # list so cleanup phase has a deterministic state.
    if m.package_info is None or not ctx.targets:
        ctx.package_deployed_files[dep_key] = []
        return deltas

    try:
        # Pre-deploy security gate
        if not _pre_deploy_security_scan(
            install_path, diagnostics,
            package_name=dep_key, force=ctx.force,
            logger=logger,
        ):
            ctx.package_deployed_files[dep_key] = []
            return deltas

        int_result = integrate_package_primitives(
            m.package_info, ctx.project_root,
            targets=ctx.targets,
            prompt_integrator=ctx.integrators["prompt"],
            agent_integrator=ctx.integrators["agent"],
            skill_integrator=ctx.integrators["skill"],
            instruction_integrator=ctx.integrators["instruction"],
            command_integrator=ctx.integrators["command"],
            hook_integrator=ctx.integrators["hook"],
            force=ctx.force,
            managed_files=ctx.managed_files,
            diagnostics=diagnostics,
            package_name=dep_key,
            logger=logger,
            scope=ctx.scope,
            # Per-package effective subset: CLI --skill overrides per-entry
            # apm.yml skills:. When CLI is absent (bare reinstall), fall back
            # to the dep_ref's persisted skill_subset.
            # When CLI explicitly provided (even --skill '*'), use ctx value
            # (which is None for '*' = install all).
            skill_subset=(
                ctx.skill_subset
                if ctx.skill_subset_from_cli
                else (
                    tuple(dep_ref.skill_subset) if dep_ref.skill_subset else None
                )
            ),
        )
        for k in (
            "prompts", "agents", "skills", "sub_skills",
            "instructions", "commands", "hooks", "links_resolved",
        ):
            deltas[k] = int_result[k]
        ctx.package_deployed_files[dep_key] = int_result["deployed_files"]
    except Exception as e:
        # Per-source error wording: each DependencySource subclass
        # declares its own INTEGRATE_ERROR_PREFIX (Strategy pattern).
        # Local packages key the diagnostic by local_path; cached/fresh
        # key by dep_key -- a behavioural detail preserved from legacy.
        package_key = (
            dep_ref.local_path
            if (dep_ref.is_local and dep_ref.local_path)
            else dep_key
        )
        diagnostics.error(
            f"{source.INTEGRATE_ERROR_PREFIX}: {e}",
            package=package_key,
        )

    # Verbose: inline skip / error count for this package
    if logger and logger.verbose:
        _skip_count = diagnostics.count_for_package(dep_key, "collision")
        _err_count = diagnostics.count_for_package(dep_key, "error")
        if _skip_count > 0:
            noun = "file" if _skip_count == 1 else "files"
            logger.package_inline_warning(
                f"    [!] {_skip_count} {noun} skipped (local files exist)"
            )
        if _err_count > 0:
            noun = "error" if _err_count == 1 else "errors"
            logger.package_inline_warning(
                f"    [!] {_err_count} integration {noun}"
            )

    return deltas
