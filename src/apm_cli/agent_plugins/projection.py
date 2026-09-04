"""Compatibility projection from canonical Agent Plugin IR to APMPackage."""

from __future__ import annotations

from typing import Any

from ..core.errors import TargetResolutionError
from ..models.apm_package import APMPackage
from .errors import AgentPluginManifestAuthorityError
from .ir import AgentPlugin, thaw_frozen_json


def project_agent_plugin_package(plugin: AgentPlugin) -> APMPackage:
    """Project canonical Agent Plugin facts into the compatibility package model.

    Portable identity comes only from ``plugin.identity``. APM-owned manifest
    fields come only from ``plugin.apm_configuration``. Component facts remain
    attached through the canonical frozen IR rather than being reparsed from
    ``plugin.json``, ``mcp.json``, or the package filesystem.
    """
    data = _project_apm_configuration(plugin)
    identity = plugin.identity
    author = dict(identity.author).get("name")
    data.update(
        {
            "name": identity.name,
            "version": identity.version or "0.0.0",
            "description": identity.description,
            "author": author,
            "license": identity.license,
        }
    )
    manifest_path = (
        plugin.apm_configuration.provenance
        if plugin.apm_configuration is not None
        else plugin.manifest.path
    )
    try:
        package = APMPackage.from_mapping(
            data,
            package_path=plugin.root,
            source_path=plugin.root,
            manifest_path=manifest_path,
        )
    except (TargetResolutionError, ValueError) as exc:
        raise AgentPluginManifestAuthorityError(
            f"Invalid Agent Plugin APM configuration: {exc}"
        ) from exc
    package.agent_plugin = plugin
    return package


def _project_apm_configuration(plugin: AgentPlugin) -> dict[str, Any]:
    """Thaw APM-owned configuration without consulting source documents."""
    configuration = plugin.apm_configuration
    if configuration is None:
        return {}
    projected = thaw_frozen_json(configuration.values)
    if not isinstance(projected, dict):
        raise AgentPluginManifestAuthorityError("Agent Plugin APM configuration must be a mapping")
    return projected
