"""Authorized package decisions shared by preflight and native deployment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core.scope import is_user_scope

from .exec_gate import check_executable_approval, plugin_bin_deployable, resolve_bin_skip
from .target_filter import log_package_target_restriction, resolve_effective_package_targets

if TYPE_CHECKING:
    from apm_cli.claude_plugins.admission import ClaudeNativePlugin
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
    claude_native: ClaudeNativePlugin | None = None

    @property
    def projection_targets(self) -> list[TargetProfile]:
        """Keep native bundles out of their unauthorized loose projections."""
        if self.codex_native is not None:
            return []
        targets = list(self.target_selection.targets)
        native_target_names = self.claude_native.harnesses if self.claude_native else set()
        return [target for target in targets if target.name not in native_target_names]


def _native_skill_selection(
    skill_subset: tuple | None, skill_pairing: Any, targets: Any
) -> tuple | None:
    """The skill selection a whole-plugin load must honor.

    A per-target pairing (req-rs-018) can restrict one target while the flat
    subset is ``None`` (all); a native load cannot honor that, so any target
    restriction counts as a selection.
    """
    if skill_pairing is None:
        return skill_subset
    restricted = {
        skill
        for target in targets
        if skill_pairing.selects_target(target.name)
        for skill in skill_pairing.skills_for_target(target.name) or ()
    }
    return tuple(sorted(restricted)) or skill_subset


def _warn_parked_plugin(package_name, package_info, allow_executables, diagnostics, logger, ctx):
    """Name why the trust owner parked a native plugin, with the next step.

    A plugin that is merely not approved yet is reported by the registrar's
    own refusal, so only changed, denied, transitive, APM-owned-path and
    incomplete-grant parking is named here.
    """
    from .exec_gate import plugin_admission, resolve_package_key

    verdict = plugin_admission(package_name, package_info, allow_executables, ctx=ctx)
    if (
        verdict is None
        or verdict.allowed
        or (verdict.reason == "unapproved-executables" and not verdict.detail)
    ):
        return verdict
    from apm_cli.security.executables import _strip_version
    from apm_cli.security.trust import parked_plugin_message

    package = _strip_version(resolve_package_key(package_info, package_name))
    message = parked_plugin_message(
        package,
        str(verdict.reason),
        verdict.detail,
        user_scope=is_user_scope(getattr(ctx, "scope", None)),
    )
    if diagnostics is not None:
        diagnostics.warn(message)
    elif logger is not None:
        logger.warning(message)
    return verdict


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
    skill_pairing: Any = None,
    ctx: InstallContext | None = None,
    policy: Any = None,
    is_first_party: bool = False,
    allow_executables: dict[str, dict[str, bool]] | None = None,
    dep_target_subset: list[str] | None = None,
    trust_bin: bool | None = None,
    bin_skip_reason_override: str | None = None,
    read_only: bool = False,
    native_claude_user_scope: bool = False,
) -> PackageDeploymentPlan:
    """Resolve deployment authorization without writing native target content."""
    import sys

    from apm_cli.claude_plugins.admission import plan_native_claude_plugin
    from apm_cli.codex_plugins.admission import plan_native_codex_plugin
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan

    selection = resolve_effective_package_targets(
        targets, dep_target_subset, package_info, diagnostics, package_name
    )
    log_package_target_restriction(logger, selection, package_name)
    if not selection.targets:
        return PackageDeploymentPlan(selection)
    native_subset = _native_skill_selection(skill_subset, skill_pairing, selection.targets)
    approvals = check_executable_approval(package_name, package_info, allow_executables, ctx=ctx)
    hooks, bin_approved, mcp, canvas, lsp = approvals
    from .exec_gate import bin_deploy_denial, hook_file_admission, native_plugin_verdict

    hook_filter = hook_file_admission(package_name, package_info, allow_executables)
    verdict = _warn_parked_plugin(
        package_name, package_info, allow_executables, diagnostics, logger, ctx
    )
    # The trust owner already named a deny, change or missing grant (#4154).
    parked_reported = bool(
        verdict is not None
        and not verdict.allowed
        and (verdict.reason != "unapproved-executables" or verdict.detail)
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
        skill_subset=native_subset,
        approvals={
            # A native plugin loads every hook file live: per-file pins never
            # admit it; a whole-plugin pin (or a type-level grant) does.
            "hooks": hooks,
            "mcp": mcp,
            "bin": bin_approved and not skip_bin,
            "canvas": canvas,
            "lsp": lsp,
        },
        ctx=ctx,
        read_only=read_only,
        parked_reported=parked_reported,
    )
    # The org policy the install gate fetched; callers that pass none
    # (preflight) defer to it, as effective_allow_map does for trust.
    org_policy = (
        policy
        if policy is not None
        else getattr(getattr(ctx, "policy_fetch", None), "policy", None)
    )
    # The project's own content is never deployed as one of its native plugins.
    native_claude_plugin = (
        None
        if codex_native is not None or is_first_party
        else plan_native_claude_plugin(
            package_info,
            package_name=package_name,
            targets=list(selection.targets),
            scope=InstallScope.USER if native_claude_user_scope else scope,
            skill_subset=native_subset,
            admit=lambda: native_plugin_verdict(
                package_name, package_info, allow_executables, ctx=ctx
            ),
            # The whole-plugin verdict approves bin/; only the run's posture applies.
            skip_bin=resolve_bin_skip(True, trust_bin, non_interactive=not sys.stdout.isatty())[0],
            policy=org_policy,
            bin_denied=bin_deploy_denial(package_info, org_policy),
        )
    )
    projection_targets = PackageDeploymentPlan(
        selection, codex_native=codex_native, claude_native=native_claude_plugin
    ).projection_targets
    # A parked plugin deploys nothing natively; only its loose targets are planned.
    native_plugin = codex_native or (
        native_claude_plugin
        if native_claude_plugin is not None and not native_claude_plugin.parked
        else None
    )
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
        native_plugin_root=native_plugin.source_root if native_plugin else None,
        plugin_bin_deployable=plugin_bin_deployable(
            package_info,
            projection_targets,
            project_root=project_root,
            scope=scope,
            policy=policy,
            skip_bin=skip_bin,
        ),
    )
    if hook_filter is not None and not hooks:
        # No pinned hook file was admitted, so the source plan never asks the
        # filter: name the parked files here. A deny is named by the skip line.
        hook_filter.admits_any()
    if hook_filter is not None and hook_filter.parked:
        from apm_cli.security.executables import _strip_version
        from apm_cli.security.trust import REASON_DENIED, parked_hook_message

        from .exec_gate import resolve_package_key

        package = _strip_version(resolve_package_key(package_info, package_name))
        for item, parked_reason in hook_filter.parked:
            if not hooks and parked_reason == REASON_DENIED:
                continue
            message = parked_hook_message(
                package, item, parked_reason, user_scope=is_user_scope(scope)
            )
            if diagnostics is not None:
                diagnostics.warn(message)
            elif logger is not None:
                logger.warning(message)
    return PackageDeploymentPlan(
        selection, approvals, skip_bin, reason, codex_native, source_plan, native_claude_plugin
    )
