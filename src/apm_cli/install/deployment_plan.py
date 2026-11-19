"""Authorized package decisions shared by preflight and native deployment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .exec_gate import check_executable_approval, plugin_bin_deployable, resolve_bin_skip
from .target_filter import log_package_target_restriction, resolve_effective_package_targets

if TYPE_CHECKING:
    from apm_cli.codex_plugins.admission import CodexNativePlugin
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.context import InstallContext
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.install.target_filter import EffectivePackageTargets
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector


@dataclass(frozen=True)
class PackageDeploymentPlan:
    """Authorized package selection shared by batch preflight and deployment."""

    target_selection: EffectivePackageTargets
    approvals: tuple[bool, bool, bool, bool, bool] = (False, False, False, False, False)
    skip_bin: bool = False
    bin_skip_reason: str | None = None
    codex_native: CodexNativePlugin | None = None
    source_plan: DeployableSourcePlan | None = None

    @property
    def projection_targets(self) -> list[TargetProfile]:
        """Keep native Codex bundles opaque to loose primitive adapters."""
        return [] if self.codex_native is not None else list(self.target_selection.targets)


def prepare_package_deployment(  # noqa: PLR0913
    package_info: Any,
    project_root: Path,
    *,
    targets: Any,
    diagnostics: DiagnosticCollector | None,
    package_name: str = "",
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    skill_subset: tuple | None,
    ctx: InstallContext | None = None,
    policy: Any = None,
    is_first_party: bool = False,
    allow_executables: dict[str, dict[str, bool]] | None = None,
    dep_target_subset: list[str] | None = None,
    trust_bin: bool | None = None,
    bin_skip_reason_override: str | None = None,
    read_only: bool = False,
) -> PackageDeploymentPlan:
    """Resolve deployment authorization without writing native target content."""
    import sys

    from apm_cli.codex_plugins.admission import plan_native_codex_plugin
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan

    selection = resolve_effective_package_targets(
        targets, dep_target_subset, package_info, diagnostics, package_name
    )
    log_package_target_restriction(logger, selection)
    if not selection.targets:
        return PackageDeploymentPlan(selection)
    approvals = check_executable_approval(package_name, package_info, allow_executables, ctx=ctx)
    hooks, bin_approved, mcp, canvas, lsp = approvals
    from .exec_gate import hook_file_admission

    hook_filter = hook_file_admission(package_name, package_info, allow_executables)
    native_codex_hooks = hooks
    if hook_filter is not None:
        from apm_cli.install.native_plugin_admission import native_hook_approval

        native_codex_hooks = native_hook_approval(
            hooks, allow_executables, package_info, package_name, None
        )
    skip_bin, reason = resolve_bin_skip(
        bin_approved, trust_bin, non_interactive=not sys.stdout.isatty()
    )
    if skip_bin and bin_skip_reason_override is not None:
        reason = bin_skip_reason_override
    codex_native = plan_native_codex_plugin(
        package_info,
        package_name,
        list(selection.targets),
        scope=scope,
        skill_subset=skill_subset,
        approvals={
            # A native plugin loads every hook file live: each must match its pin.
            "hooks": native_codex_hooks,
            "mcp": mcp,
            "bin": bin_approved and not skip_bin,
            "canvas": canvas,
            "lsp": lsp,
        },
        ctx=ctx,
        read_only=read_only,
    )
    projection_targets = [] if codex_native is not None else list(selection.targets)
    source_plan = DeployableSourcePlan.create(
        package_info,
        projection_targets,
        skill_subset=skill_subset,
        hooks_approved=hooks,
        hook_file_admitted=hook_filter,
        canvas_approved=canvas or is_first_party,
        skip_bin=skip_bin,
        diagnostics=diagnostics,
        package_name=package_name,
        native_plugin_root=codex_native.source_root if codex_native else None,
        plugin_bin_deployable=plugin_bin_deployable(
            package_info,
            projection_targets,
            project_root=project_root,
            scope=scope,
            policy=policy,
            skip_bin=skip_bin,
        ),
    )
    if hook_filter is not None and hook_filter.parked:
        from apm_cli.security.executables import _strip_version
        from apm_cli.security.trust import parked_hook_message

        from .exec_gate import resolve_package_key

        package = _strip_version(resolve_package_key(package_info, package_name))
        for item, parked_reason in hook_filter.parked:
            message = parked_hook_message(package, item, parked_reason)
            if diagnostics is not None:
                diagnostics.warn(message)
            elif logger is not None:
                logger.warning(message)
    return PackageDeploymentPlan(selection, approvals, skip_bin, reason, codex_native, source_plan)
