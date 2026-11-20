"""Codex-specific admission after canonical format, target, and executable decisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from apm_cli.codex_plugins.client import CodexNativeClient, CodexNativeError
from apm_cli.codex_plugins.manifest import CodexCompatibilityPlugin
from apm_cli.core.scope import InstallScope
from apm_cli.install.native_plugin_admission import (
    plugin_root_exec_types,
    record_native_exec_status,
)
from apm_cli.models.dependency.subsets import skill_subset_filter_tokens
from apm_cli.security.executables import EXEC_TYPE_HOOKS, EXEC_TYPE_MCP, scan_package_executables
from apm_cli.utils.content_hash import compute_native_bundle_hash

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan


@dataclass(frozen=True)
class CodexNativePlugin:
    """An opaque native bundle admitted by the shared integration gates."""

    dependency_key: str
    name: str
    source_root: Path
    source_hash: str
    mcp_server_names: tuple[str, ...] = ()
    source_commit: str | None = None
    # Unapproved executables: nothing deploys or registers until approved (#4154).
    parked: bool = False
    # The trust owner already reported why (deny, change, missing grant).
    parked_reported: bool = False
    target_name: ClassVar[str] = "codex"

    def source_identity(self) -> dict[str, str | None]:
        """Carry the admitted source identity into registration and read-only replay."""
        return {
            "dependency_key": self.dependency_key,
            "source_hash": self.source_hash,
            "source_commit": self.source_commit,
        }


@dataclass(frozen=True)
class CodexNativeDeployment:
    """A successfully scanned source plan, retained for final native registration."""

    plugin: CodexNativePlugin
    source_plan: DeployableSourcePlan
    source_hash: str


def finalize_codex_native(
    result: dict,
    plugin: CodexNativePlugin,
    source_plan: DeployableSourcePlan,
    reconcile_result: dict | None,
    ctx: InstallContext | None,
    scratch_root: Path | None,
) -> dict:
    """Queue the admitted source identity only after successful loose cleanup."""
    if plugin.parked:
        result["parked_native"] = True
    if plugin.parked and plugin.parked_reported:
        return result
    if plugin.parked:
        from apm_cli.core.scope import is_user_scope
        from apm_cli.security.trust import REASON_UNAPPROVED, parked_plugin_message

        message = parked_plugin_message(
            plugin.dependency_key,
            REASON_UNAPPROVED,
            user_scope=is_user_scope(getattr(ctx, "scope", None)),
        )
        diagnostics = getattr(ctx, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.warn(message, package=plugin.dependency_key)
        return result
    if reconcile_result is not None and reconcile_result.get("errors", 0):
        raise CodexNativeError(
            "Prior loose hooks could not be removed; fix their ownership conflict "
            "and retry before native registration."
        )
    if ctx is not None and scratch_root is None and not ctx.dry_run:
        ctx.codex_native_plugins[plugin.dependency_key] = CodexNativeDeployment(
            plugin,
            source_plan,
            plugin.source_hash,
        )
    result["native_plugin"] = True
    result["codex_source_identity"] = plugin.source_identity()
    return result


def _replays_lock(ctx: Any) -> bool:
    """Whether this install replays the lock (``--frozen``): a park must fail it."""
    if ctx is None:
        return False
    from apm_cli.install.service import InstallService

    return InstallService.frozen_disposition(ctx).replay


def plan_native_codex_plugin(
    package_info: Any,
    dependency_key: str,
    targets: list[Any],
    *,
    scope: InstallScope,
    skill_subset: tuple[str, ...] | None,
    approvals: dict[str, bool],
    ctx: InstallContext | None,
    read_only: bool = False,
    parked_reported: bool = False,
) -> CodexNativePlugin | None:
    """Never normalize or partially project a selected Codex compatibility bundle."""
    package = getattr(package_info, "package", None)
    manifest = getattr(package, "codex_plugin", None)
    if not isinstance(manifest, CodexCompatibilityPlugin):
        return None
    if not read_only:
        CodexNativeClient.require_scope(scope)
    if not read_only and not any(
        target.name == CodexNativePlugin.target_name for target in targets
    ):
        raise CodexNativeError(
            "Codex compatibility plugins require the codex target; other harnesses "
            "cannot load this native bundle. Select '--target codex' and retry."
        )
    if skill_subset_filter_tokens(skill_subset) is not None:
        raise CodexNativeError(
            "Codex native registration requires the complete bundle. "
            "Remove the skill subset and retry; no loose skills were registered."
        )
    root = Path(package_info.install_path).resolve()
    types = set(plugin_root_exec_types(root))
    types.update(scan_package_executables(root, dependency_key, manifest.version).exec_types)
    if manifest.has_mcp:
        types.add(EXEC_TYPE_MCP)
    if manifest.has_hooks:
        types.add(EXEC_TYPE_HOOKS)
    record_native_exec_status(ctx, dependency_key, package_info, tuple(sorted(types)))
    parked = any(not approvals.get(kind, False) for kind in types)
    if parked and (read_only or _replays_lock(ctx)):
        from apm_cli.security.trust import approve_command

        # Codex registration is user-scope only, so the approval is too.
        command = approve_command(dependency_key, user_scope=True)
        raise CodexNativeError(
            "Codex native bundle contains executables not approved in executables.allow. "
            f"Review them, run '{command}', then retry; "
            "APM never deploys part of a native bundle in place of the whole."
        )
    reference = getattr(package_info, "dependency_ref", None)
    resolved = getattr(package_info, "resolved_reference", None)
    source_commit = (
        None
        if reference is not None and reference.is_local
        else getattr(resolved, "resolved_commit", None)
    )
    return CodexNativePlugin(
        dependency_key,
        manifest.name,
        root,
        compute_native_bundle_hash(root),
        manifest.mcp_server_names,
        source_commit,
        parked=parked,
        parked_reported=parked and parked_reported,
    )
