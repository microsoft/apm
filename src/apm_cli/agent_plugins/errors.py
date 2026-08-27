"""Typed failures raised by the Agent Plugins contract loader."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apm_cli.models.apm_package import DependencyReference, PackageInfo

AGENT_PLUGIN_RECOVERY = (
    "Use 'apm pack --claude-plugin' or ask the publisher for a legacy-compatible package."
)
AGENT_PLUGIN_DEPLOYMENT_BLOCKED = (
    "Agent Plugins v1.0.0 deployment is blocked because no native harness "
    "is binary-qualified. " + AGENT_PLUGIN_RECOVERY
)
AGENT_PLUGIN_BUNDLE_ROUTE_BLOCKED = (
    "Agent Plugins v1.0.0 packages cannot be installed through the imperative "
    "local-bundle route, which deploys loose primitives and owns no lockfile "
    "row. Declare the plugin as a dependency in apm.yml (a local path works) "
    "and run 'apm install --target copilot' so APM materializes it under "
    "apm_modules and registers it natively with GitHub Copilot."
)
AGENT_PLUGIN_IR_MISSING = (
    "Native Agent Plugin canonical IR is missing, so deployment was blocked. "
    + AGENT_PLUGIN_RECOVERY
)


class AgentPluginError(ValueError):
    """Base class for fail-closed Agent Plugin contract failures."""


class NotAgentPluginError(AgentPluginError):
    """Raised when a directory does not declare an Agent Plugins schema."""


class AgentPluginManifestError(AgentPluginError):
    """Raised when root plugin.json violates the selected contract."""


class UnsupportedAgentPluginVersionError(AgentPluginManifestError):
    """Raised when plugin.json selects an unsupported Agent Plugins version."""


class AgentPluginManifestAuthorityError(AgentPluginManifestError):
    """Raised when apm.yml attempts to override portable plugin identity."""


class AgentPluginLegacyBoundaryError(AgentPluginError):
    """Raised when native Agent Plugin input reaches Claude normalization."""


class AgentPluginDeploymentBoundaryError(AgentPluginError):
    """Raised when native Agent Plugin content reaches a deployment boundary."""


def enforce_agent_plugin_deployment_boundary(
    package_info: Any | None = None,
    *,
    bundle_info: Any | None = None,
) -> None:
    """Block native Agent Plugin deployment unless a qualified harness exists.

    A native Agent Plugin is admitted only when the canonical capability owner
    (:mod:`apm_cli.copilot_plugins.capability`) reports that GitHub Copilot can
    load it live from APM's materialization. Everything else -- an unqualified
    client, a non-Copilot target, a legacy bundle route, or missing canonical
    IR -- stays fail-closed with a precise reason.
    """
    from apm_cli.bundle.formats import BundleFormat
    from apm_cli.copilot_plugins.capability import current_native_registration
    from apm_cli.models.validation import PackageType

    if (
        bundle_info is not None
        and getattr(bundle_info, "format", None) == BundleFormat.AGENT_PLUGIN.value
    ):
        raise AgentPluginDeploymentBoundaryError(AGENT_PLUGIN_BUNDLE_ROUTE_BLOCKED)
    if package_info is None:
        return
    if package_info.package_type is not PackageType.AGENT_PLUGIN:
        return
    package = getattr(package_info, "package", None)
    if package is None or getattr(package, "agent_plugin", None) is None:
        raise AgentPluginDeploymentBoundaryError(AGENT_PLUGIN_IR_MISSING)
    capability = current_native_registration()
    if capability is not None and capability.supported:
        return
    if capability is not None and capability.reason:
        raise AgentPluginDeploymentBoundaryError(capability.reason)
    raise AgentPluginDeploymentBoundaryError(AGENT_PLUGIN_DEPLOYMENT_BLOCKED)


def preflight_reintegration_survivors(
    dependencies: Iterable[DependencyReference],
    modules_dir: Path,
    *,
    require_valid_installed: bool = False,
) -> list[tuple[DependencyReference, PackageInfo]]:
    """Validate installed survivors before clear-then-rebuild integration."""
    from apm_cli.models.apm_package import build_installed_package_info

    plan = []
    seen: set[str] = set()
    for dependency in dependencies:
        dep_key = dependency.get_unique_key()
        if dep_key in seen:
            continue
        seen.add(dep_key)
        try:
            install_path = dependency.get_install_path(modules_dir)
        except ValueError:
            if require_valid_installed:
                raise
            continue
        package_info = build_installed_package_info(dependency, modules_dir)
        if package_info is None:
            if require_valid_installed and install_path.exists():
                raise ValueError(
                    "Cannot validate surviving package before integration rebuild: "
                    f"{dependency.get_identity()}"
                )
            continue
        enforce_agent_plugin_deployment_boundary(package_info)
        plan.append((dependency, package_info))
    return plan
