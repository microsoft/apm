"""Codex-specific admission after canonical format, target, and executable decisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from apm_cli.codex_plugins.client import CodexNativeClient, CodexNativeError
from apm_cli.codex_plugins.manifest import (
    CodexCompatibilityPlugin,
    load_codex_compatibility_manifest,
)
from apm_cli.core.scope import InstallScope
from apm_cli.install.native_plugin_admission import (
    plugin_root_exec_types,
    record_native_exec_status,
)
from apm_cli.models.dependency.subsets import has_skill_selection
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


def _meet_trust_transitive(
    manifest: CodexCompatibilityPlugin, dependency_key: str, policy: Any, ctx: Any
) -> None:
    """Refuse (block) or warn (warn) for servers org ``mcp.allow`` does not list."""
    from apm_cli.security.trust import unlisted_transitive_servers

    unlisted = unlisted_transitive_servers(manifest.mcp_server_names, policy)
    if not unlisted:
        return
    names = ", ".join(f"'{name}'" for name in unlisted)
    message = (
        f"Codex native plugin {dependency_key}: transitive MCP server(s) {names} "
        "not in org mcp.allow (mcp.trust_transitive: false)"
    )
    if policy.enforcement == "block":
        raise CodexNativeError(
            f"{message}. Ask your org policy owner to allow them, or remove this dependency."
        )
    diagnostics = getattr(ctx, "diagnostics", None)
    if diagnostics is not None:
        diagnostics.warn(message, package=dependency_key)


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
    policy: Any = None,
    transitive: bool = False,
) -> CodexNativePlugin | None:
    """Never normalize or partially project a selected Codex compatibility bundle.

    A *transitive* bundle's servers meet org ``mcp.trust_transitive`` as a
    loose transitive server does (req-sc-016): Codex loads them itself, so the
    merged-set preflight never sees them.
    """
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
    if has_skill_selection(skill_subset):
        raise CodexNativeError(
            "Codex native registration requires the complete bundle. "
            "Remove the skill subset and retry; no loose skills were registered."
        )
    if transitive:
        _meet_trust_transitive(manifest, dependency_key, policy, ctx)
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


def locked_transitive_codex_refusals(lockfile: Any, modules_dir: Path, policy: Any) -> list[str]:
    """Subjects of each deployed transitive Codex plugin org ``mcp.trust_transitive`` withholds.

    The audit counterpart of :func:`_meet_trust_transitive`, for a policy applied
    after deployment: a server ``mcp.allow`` does not list, and a manifest APM
    cannot read. The Codex ownership journal names what is deployed and the root
    Codex loads; without a readable journal every transitive package that
    selects the Codex format fails closed.
    """
    from apm_cli.codex_plugins.registrar import deployed_codex_roots, ledger_path_for
    from apm_cli.security.trust import unlisted_transitive_servers, withholds_transitive

    if not withholds_transitive(policy):
        return []
    try:
        deployed = deployed_codex_roots(modules_dir)
    except (OSError, ValueError):
        deployed = None
    blind = f"Codex ownership journal {ledger_path_for(modules_dir)} is missing or unreadable"
    refused: list[str] = []
    for key, dep in (getattr(lockfile, "dependencies", None) or {}).items():
        if key == "." or (dep.depth or 1) <= 1:
            continue
        if deployed is None:
            source = dep.to_dependency_ref().get_install_path(modules_dir)
            if dep.native_plugin is None and _selects_codex_format(source):
                refused.append(f"{key}: MCP cannot be checked ({blind})")
            continue
        if key not in deployed:
            continue
        root = deployed[key]
        try:
            if root is None:
                raise CodexNativeError("its deployed Codex root is not recorded")
            names = load_codex_compatibility_manifest(root).mcp_server_names
        except (OSError, ValueError) as exc:
            refused.append(f"{key}: MCP cannot be checked ({exc})")
            continue
        refused.extend(unlisted_transitive_servers(names, policy))
    return refused


def _selects_codex_format(root: Path) -> bool:
    """Whether canonical format selection picks *root*'s Codex manifest.

    A root whose selection cannot be read counts when it carries one.
    """
    from apm_cli.codex_plugins.manifest import (
        CODEX_COMPAT_MANIFEST,
        is_codex_compatibility_manifest,
    )
    from apm_cli.models.validation import detect_package_type

    if not (root / CODEX_COMPAT_MANIFEST).is_file():
        return False
    try:
        return is_codex_compatibility_manifest(root, detect_package_type(root)[1])
    except (OSError, ValueError):
        return True
