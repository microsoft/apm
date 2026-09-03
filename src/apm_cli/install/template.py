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

from pathlib import Path
from typing import TYPE_CHECKING, cast

from apm_cli.agent_plugins.errors import (
    AgentPluginDeploymentBoundaryError,
    AgentPluginTargetExcludedError,
)
from apm_cli.install.package_resolution import effective_deploy_skill_subset
from apm_cli.install.services import (
    IntegratorBundle,
    enforce_agent_plugin_deployment_boundary,
    integrate_package_primitives,
)
from apm_cli.install.sources import (
    DependencySource,
    Materialization,
)

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext

_MATERIALIZATION_UNSET = object()


def _effective_allow(ctx) -> dict | None:
    """Return the effective (deny-wins) allow-map for the install context.

    Builds the #1873 trust context from three layers and materialises the
    decision map via the shared resolver:

    * org policy -- ``ctx.policy_fetch.policy`` (the deny ceiling, Gap A);
    * project ``apm.yml`` -- the ``executables`` block (or legacy
      ``allowExecutables`` alias) read from disk;
    * user consent -- ``~/.apm/config.json`` (lowest authority).

    Returns ``None`` when the gate is disabled (backward-compatible: every
    executable deploys).
    """
    from apm_cli.security.executables import (
        exec_trust_context_for_project,
        materialize_exec_map,
    )

    if getattr(ctx, "exec_trust_ctx", None) is not None:
        return getattr(ctx, "exec_allow_map", None)

    policy = getattr(getattr(ctx, "policy_fetch", None), "policy", None)
    project_root = getattr(ctx, "project_root", None)
    project_allow = getattr(getattr(ctx, "apm_package", None), "allow_executables", None)
    trust_ctx = exec_trust_context_for_project(
        project_root,
        policy=policy,
        fallback_allow_executables=project_allow,
        logger=getattr(ctx, "logger", None),
    )
    allow_map = materialize_exec_map(trust_ctx)
    # Cache the resolved context and allow map once per install so each
    # dependency uses the same precedence ladder without re-reading policy files.
    if hasattr(ctx, "exec_trust_ctx"):
        ctx.exec_trust_ctx = trust_ctx
    if hasattr(ctx, "exec_allow_map"):
        ctx.exec_allow_map = allow_map
    return allow_map


def run_integration_template(
    source: DependencySource,
    *,
    materialization: Materialization | None | object = _MATERIALIZATION_UNSET,
) -> dict[str, int] | None:
    """Run the shared post-acquire integration flow for one dependency.

    Returns a counter-delta dict for accumulation by the caller, or
    ``None`` if the source declined to acquire (skipped, failed).
    """
    if materialization is _MATERIALIZATION_UNSET:
        materialization, terminal_deltas = prepare_integration_materialization(source)
        if terminal_deltas is not None:
            return terminal_deltas
    if materialization is None:
        return None

    return _integrate_materialization(source, cast(Materialization, materialization))


def prepare_integration_materialization(
    source: DependencySource,
) -> tuple[Materialization | None, dict[str, int] | None]:
    """Acquire one package without deploying it."""
    from apm_cli.deps.plugin_parser import DeclaredPluginComponentError

    try:
        return source.acquire(), None
    except DeclaredPluginComponentError as exc:
        source.ctx.diagnostics.error(str(exc), package=source.dep_key)
        return None, {}


def preflight_agent_plugin_materializations(
    prepared: list[tuple[DependencySource, Materialization]],
) -> None:
    """Reject the batch once, before any package can mutate a target.

    A native Agent Plugin whose effective targets simply do not select
    ``copilot`` is not a failure: it is skipped per-package during integration
    (:func:`_record_agent_plugin_target_skip`), so it must not abort the
    batch -- ``AgentPluginTargetExcludedError`` carries that distinction.
    Anything else raised here (missing canonical IR, the imperative bundle
    route) is a real, actionable failure and aborts the whole batch.
    """
    for _, materialization in prepared:
        try:
            enforce_agent_plugin_deployment_boundary(materialization.package_info)
        except AgentPluginTargetExcludedError:
            continue


def preflight_agent_plugin_dry_run(
    ctx: InstallContext,
    dependencies: list,
    *,
    apm_package,
) -> None:
    """Reject a cached or local native package without mutating its source.

    The native-registration capability is published for the duration of the
    preview so a project whose effective targets exclude ``copilot`` yields
    the SAME precise reason a real install would, instead of the generic
    'no native harness' fallback. Admission never depends on whether a
    Copilot binary exists or which version it reports.

    Target exclusion (``AgentPluginTargetExcludedError``) is never fatal --
    a real install skips that package with one warning and installs the
    rest of the batch, so the dry-run preview must not abort the whole
    preview for the same reason either. Only a genuine structural failure
    (missing canonical IR, the imperative bundle route) aborts here.
    """
    from apm_cli.bundle.local_bundle import route_agent_plugin_package
    from apm_cli.copilot_plugins.capability import native_registration_scope
    from apm_cli.core.scope import get_modules_dir, is_user_scope
    from apm_cli.models.apm_package import PackageInfo, package_target_selection
    from apm_cli.models.validation import validate_apm_package

    source_root = ctx.project_root
    modules_dir = get_modules_dir(ctx.scope)
    explicit_target = ctx.target or package_target_selection(apm_package)
    try:
        from apm_cli.integration.targets import resolve_targets

        targets = resolve_targets(
            source_root,
            user_scope=is_user_scope(ctx.scope),
            explicit_target=explicit_target,
        )
    except Exception:
        targets = getattr(ctx, "targets", None)
    with native_registration_scope(targets):
        for dependency in dependencies:
            if dependency.is_local and dependency.local_path:
                package_path = Path(dependency.local_path).expanduser()
                if not package_path.is_absolute():
                    package_path = (source_root / package_path).resolve()
            else:
                package_path = dependency.get_install_path(modules_dir)
            if not package_path.is_dir():
                continue
            detection = route_agent_plugin_package(package_path)
            if detection is None:
                continue
            if dependency.is_local:
                validation = validate_apm_package(
                    package_path,
                    source_path=package_path,
                    agent_plugin_detection=detection,
                )
            else:
                validation = validate_apm_package(
                    package_path,
                    agent_plugin_detection=detection,
                )
            if validation.is_valid and validation.package is not None:
                try:
                    enforce_agent_plugin_deployment_boundary(
                        PackageInfo(
                            package=validation.package,
                            install_path=package_path,
                            dependency_ref=dependency,
                            package_type=validation.package_type,
                        )
                    )
                except AgentPluginTargetExcludedError:
                    continue


def _record_agent_plugin_boundary_diagnostic(
    diagnostics,
    *,
    package_key: str,
    prefix: str,
    error: AgentPluginDeploymentBoundaryError,
) -> None:
    """Record one package-attributed native deployment failure."""
    diagnostics.error(f"{prefix}: {error}", package=package_key)


def _record_agent_plugin_boundary_failure(
    source: DependencySource,
    materialization: Materialization,
    error: AgentPluginDeploymentBoundaryError,
) -> dict[str, int]:
    """Record one typed boundary failure as a canonical install diagnostic."""
    ctx = source.ctx
    deltas = materialization.deltas
    dep_ref = source.dep_ref
    dep_key = materialization.dep_key
    deltas["installed"] = 0
    ctx.package_deployed_files[dep_key] = []
    package_key = dep_ref.local_path if (dep_ref.is_local and dep_ref.local_path) else dep_key
    _record_agent_plugin_boundary_diagnostic(
        ctx.diagnostics,
        package_key=package_key,
        prefix=source.INTEGRATE_ERROR_PREFIX,
        error=error,
    )
    return deltas


def _record_agent_plugin_target_skip(
    source: DependencySource,
    materialization: Materialization,
    error: AgentPluginTargetExcludedError,
) -> dict[str, int]:
    """Record a non-fatal skip for a package this project does not target at copilot.

    Mirrors the per-dependency ``targets:`` subset already handled in
    ``finalize_native_plugin``: native registration is skipped, ONE warning
    names the package, and the rest of the batch installs. This is a warning,
    not an error, so the install still exits 0.
    """
    ctx = source.ctx
    deltas = materialization.deltas
    dep_ref = source.dep_ref
    dep_key = materialization.dep_key
    deltas["installed"] = 0
    ctx.package_deployed_files[dep_key] = []
    package_key = dep_ref.local_path if (dep_ref.is_local and dep_ref.local_path) else dep_key
    ctx.diagnostics.warn(str(error), package=package_key)
    return deltas


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
    dep_key = m.dep_key
    diagnostics = ctx.diagnostics
    logger = ctx.logger

    try:
        enforce_agent_plugin_deployment_boundary(m.package_info)
    except AgentPluginTargetExcludedError as exc:
        return _record_agent_plugin_target_skip(source, m, exc)
    except AgentPluginDeploymentBoundaryError as exc:
        return _record_agent_plugin_boundary_failure(source, m, exc)

    if ctx.skill_subset_from_cli and ctx.skill_subset:
        from apm_cli.install.outcome import require_requested_components
        from apm_cli.integration.skill_integrator import SkillIntegrator

        available_skills = SkillIntegrator.available_skill_names(m.package_info)
        if available_skills is not None and not require_requested_components(
            diagnostics,
            option="--skill",
            component="skill",
            requested=ctx.skill_subset,
            available=available_skills,
            package=dep_key,
        ):
            ctx.package_deployed_files[dep_key] = []
            return deltas

    # No-op when targets are empty or acquire decided to skip integration
    # (signalled by package_info=None). Leave this dependency absent from the
    # integration outcome so cleanup does not treat every prior file as stale.
    if m.package_info is None or not ctx.targets:
        return deltas

    try:
        # Per-package effective subset: ``--skill`` is additive (issue
        # #1786), so deploy the UNION of the persisted apm.yml ``skills:``
        # and the current CLI ``--skill`` values -- a targeted ``--skill``
        # install lands on top of previously pinned skills instead of
        # erasing them. ``--skill '*'`` resets to the full bundle (None).
        effective_skill_subset = effective_deploy_skill_subset(
            skill_subset_from_cli=ctx.skill_subset_from_cli,
            cli_subset=ctx.skill_subset,
            persisted_subset=dep_ref.skill_subset,
        )
        # When the additive union deploys more skills than the user named on
        # this invocation, name the retained pins so the deployed set is not
        # a silent surprise (verbose only -- the count already renders).
        if logger and ctx.skill_subset and effective_skill_subset:
            retained = sorted(set(effective_skill_subset) - set(ctx.skill_subset))
            if retained:
                logger.verbose_detail(
                    f"    [i] {dep_key}: retaining previously pinned "
                    f"skill(s): {', '.join(retained)}"
                )
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
            skill_subset=effective_skill_subset,
            dep_target_subset=dep_ref.target_subset,
            ctx=ctx,
            allow_executables=_effective_allow(ctx),
            trust_bin=getattr(ctx, "trust_bin", None),
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
        # A natively registered Agent Plugin deploys no primitives by design --
        # Copilot loads the whole unit live -- but it is still an install.
        if int_result.get("native_plugin"):
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
