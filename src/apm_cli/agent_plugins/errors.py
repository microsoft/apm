"""Typed failures raised by the Agent Plugins contract loader."""

from __future__ import annotations

from typing import Any

_AGENT_PLUGIN_RECOVERY = (
    "Package authors: use 'apm plugin init --claude-plugin' and "
    "'apm pack --claude-plugin' for explicit legacy compatibility. "
    "Consumers: ask the publisher for a legacy-compatible package."
)
AGENT_PLUGIN_DEPLOYMENT_BLOCKED = (
    "Native Agent Plugin components are not enabled yet, so deployment was blocked. "
    + _AGENT_PLUGIN_RECOVERY
)
AGENT_PLUGIN_IR_MISSING = (
    "Native Agent Plugin canonical IR is missing, so deployment was blocked. "
    + _AGENT_PLUGIN_RECOVERY
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
    """Block every native Agent Plugin deployment until IR integration exists."""
    from apm_cli.bundle.formats import BundleFormat
    from apm_cli.models.validation import PackageType

    if (
        bundle_info is not None
        and getattr(bundle_info, "format", None) == BundleFormat.AGENT_PLUGIN.value
    ):
        raise AgentPluginDeploymentBoundaryError(AGENT_PLUGIN_DEPLOYMENT_BLOCKED)
    if package_info is None:
        return
    if package_info.package_type is not PackageType.AGENT_PLUGIN:
        return
    package = getattr(package_info, "package", None)
    if package is None or getattr(package, "agent_plugin", None) is None:
        raise AgentPluginDeploymentBoundaryError(AGENT_PLUGIN_IR_MISSING)
    raise AgentPluginDeploymentBoundaryError(AGENT_PLUGIN_DEPLOYMENT_BLOCKED)
