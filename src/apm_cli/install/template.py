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

from apm_cli.install.helpers.security_scan import _pre_deploy_security_scan
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.install.sources import DependencySource, Materialization

_EFFECTIVE_ALLOW_UNSET = object()


def _effective_allow(ctx) -> dict | None:
    """Return the effective allowExecutables map for the install context.

    Reads the gate opt-in signal from the project ``apm.yml`` via
    ``ctx.apm_package.allow_executables`` and merges it with the
    user-local approvals from ``~/.apm/approvals.yml``.  Returns
    ``None`` when the gate is disabled (backward-compatible behaviour).

    The merged map is memoized on ``ctx`` for the duration of the install
    run: it is read once per dependency during materialization and the
    approvals file does not change mid-run, so re-reading it per dep is
    pure overhead.
    """
    import contextlib

    cached = getattr(ctx, "_effective_allow_cache", _EFFECTIVE_ALLOW_UNSET)
    if cached is not _EFFECTIVE_ALLOW_UNSET:
        return cached

    from apm_cli.security.executables import effective_allow_executables

    project_val = getattr(getattr(ctx, "apm_package", None), "allow_executables", None)
    result = effective_allow_executables(project_val)
    with contextlib.suppress(Exception):
        ctx._effective_allow_cache = result
    return result


def run_integration_template(
    source: DependencySource,
) -> dict[str, int] | None:
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
) -> dict[str, int]:
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
            install_path,
            diagnostics,
            package_name=dep_key,
            force=ctx.force,
            logger=logger,
        ):
            ctx.package_deployed_files[dep_key] = []
            return deltas

        int_result = integrate_package_primitives(
            m.package_info,
            ctx.project_root,
            targets=ctx.targets,
            integrators=IntegratorBundle(
                prompt=ctx.integrators["prompt"],
                agent=ctx.integrators["agent"],
                skill=ctx.integrators["skill"],
                instruction=ctx.integrators["instruction"],
                command=ctx.integrators["command"],
                hook=ctx.integrators["hook"],
                canvas=ctx.integrators.get("canvas"),
            ),
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
                else (tuple(dep_ref.skill_subset) if dep_ref.skill_subset else None)
            ),
            ctx=ctx,
            allow_executables=_effective_allow(ctx),
        )
        mutation_keys = (
            "prompts",
            "agents",
            "skills",
            "sub_skills",
            "instructions",
            "commands",
            "hooks",
            "canvases",
        )
        for k in (*mutation_keys, "links_resolved"):
            deltas[k] = int_result[k]
        # Source-level install deltas are promoted only when primitives changed.
        if any(int_result[k] > 0 for k in mutation_keys):
            deltas["installed"] = 1
        ctx.package_deployed_files[dep_key] = int_result["deployed_files"]
    except Exception as e:
        # Per-source error wording: each DependencySource subclass
        # declares its own INTEGRATE_ERROR_PREFIX (Strategy pattern).
        # Local packages key the diagnostic by local_path; cached/fresh
        # key by dep_key -- a behavioural detail preserved from legacy.
        package_key = dep_ref.local_path if (dep_ref.is_local and dep_ref.local_path) else dep_key
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
            logger.package_inline_warning(f"    [!] {_err_count} integration {noun}")

    return deltas
