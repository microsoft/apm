"""Canonical declaration-first primitive classification."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from apm_cli.agent_plugins.constants import PLUGIN_SCHEMA_ID
from apm_cli.agent_plugins.errors import AgentPluginManifestError
from apm_cli.utils.diagnostics import printable_ascii_text
from apm_cli.utils.paths import portable_relpath
from apm_cli.utils.yaml_io import loads_frontmatter


class PluginSchemaRoute(Enum):
    """Plugin manifest route selected by declaration before structure fallback."""

    LEGACY = "legacy"
    AGENT_PLUGIN = "agent_plugin"


class PrimitiveKind(Enum):
    """Deployable primitive kind selected by the canonical resolver."""

    AGENT = "agent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PluginManifestClassification:
    """Declaration-first classification of one plugin manifest."""

    route: PluginSchemaRoute
    schema_id: str | None = None
    warning: str | None = None


@dataclass(frozen=True)
class AgentSourceClassification:
    """Classification result for a file found below an agent source root."""

    kind: PrimitiveKind
    warning: str | None = None


_AGENT_DECLARATION_FIELDS = frozenset(
    {
        "allowed-tools",
        "argument-hint",
        "color",
        "description",
        "input",
        "mcp_servers",
        "model",
        "name",
        "tools",
    }
)
_DOCUMENT_DECLARATION_FIELDS = frozenset(
    {
        "draft",
        "sidebar",
        "sidebar_position",
        "tags",
        "title",
    }
)


def classify_plugin_manifest(document: Mapping[str, Any]) -> PluginManifestClassification:
    """Classify a plugin manifest by declaration, then allow structure fallback.

    Unknown ``$schema`` values are identification misses, not validation
    rejections. Callers should fall back to structural Claude plugin handling and
    surface ``warning`` to the user when present.
    """
    if "$schema" not in document:
        return PluginManifestClassification(PluginSchemaRoute.LEGACY)

    schema_id = document["$schema"]
    if not isinstance(schema_id, str):
        raise AgentPluginManifestError("Invalid root plugin.json: $schema must be a string")
    if schema_id == PLUGIN_SCHEMA_ID:
        return PluginManifestClassification(
            PluginSchemaRoute.AGENT_PLUGIN,
            schema_id=schema_id,
        )
    return PluginManifestClassification(
        PluginSchemaRoute.LEGACY,
        schema_id=schema_id,
        warning=(
            f"Unrecognized plugin manifest $schema: {printable_ascii_text(schema_id)}. "
            "APM classified the plugin by structure instead. Action: keep the "
            "legacy plugin fields valid for the target harness, or update APM when "
            "native support exists."
        ),
    )


def classify_plugin_manifest_schema(document: Mapping[str, Any]) -> PluginSchemaRoute:
    """Return only the route for callers that do not render diagnostics."""
    return classify_plugin_manifest(document).route


def plugin_manifest_schema_warning(document: Mapping[str, Any]) -> str | None:
    """Return the non-fatal warning for an unrecognized schema declaration."""
    return classify_plugin_manifest(document).warning


def warn_unrecognized_plugin_schema(diagnostics, package_key: str, install_path: Path) -> None:
    """Surface non-fatal plugin schema identification misses."""
    if diagnostics is None:
        return
    from apm_cli.agent_plugins.io import read_json_document
    from apm_cli.utils.helpers import find_plugin_json

    plugin_json_path = find_plugin_json(install_path)
    if plugin_json_path is None or not plugin_json_path.is_file():
        return
    try:
        document = read_json_document(plugin_json_path)
    except (OSError, ValueError):
        return
    if not isinstance(document, dict):
        return
    warning = plugin_manifest_schema_warning(document)
    if warning:
        diagnostics.warn(warning, package=package_key)


def classify_agent_source_file(source: Path, source_root: Path) -> AgentSourceClassification:
    """Classify one source-tree file as an agent or a non-deployable miss.

    Precedence is declaration first, then Markdown structure, then the caller's
    source-location and extension signal. The final fallback preserves legacy
    ``.apm/agents/*.md`` behavior only when the file has no contrary
    declaration.
    """
    if not source.is_file():
        return AgentSourceClassification(PrimitiveKind.UNKNOWN)
    if not _is_markdown(source):
        return AgentSourceClassification(
            PrimitiveKind.UNKNOWN,
            warning=_skipped_asset_warning(source, source_root),
        )

    try:
        content = source.read_text(encoding="utf-8")
    except OSError:
        return AgentSourceClassification(
            PrimitiveKind.UNKNOWN,
            warning=_skipped_markdown_warning(
                source,
                source_root,
                "could not be read",
                "fix the file permissions, then rerun 'apm install'",
            ),
        )

    try:
        post = loads_frontmatter(content)
    except yaml.YAMLError:
        return AgentSourceClassification(PrimitiveKind.AGENT)

    metadata = getattr(post, "metadata", None)
    if isinstance(metadata, dict) and metadata:
        schema = metadata.get("$schema")
        schema_warning = None
        if "$schema" in metadata:
            if not isinstance(schema, str):
                return AgentSourceClassification(
                    PrimitiveKind.UNKNOWN,
                    warning=_skipped_markdown_warning(
                        source,
                        source_root,
                        "declares a non-string $schema",
                        "use a string $schema or move the file outside the agents source tree",
                    ),
                )
            schema_warning = _unknown_agent_schema_warning(source, source_root, schema)

        keys = frozenset(str(key) for key in metadata)
        if keys & _AGENT_DECLARATION_FIELDS:
            return AgentSourceClassification(PrimitiveKind.AGENT, warning=schema_warning)
        if keys & _DOCUMENT_DECLARATION_FIELDS:
            return AgentSourceClassification(
                PrimitiveKind.UNKNOWN,
                warning=_skipped_markdown_warning(
                    source,
                    source_root,
                    "declares document frontmatter but no agent fields",
                    "move documentation outside the agents source tree or add agent "
                    "description frontmatter if this file is an agent",
                ),
            )
        return AgentSourceClassification(
            PrimitiveKind.UNKNOWN,
            warning=_skipped_markdown_warning(
                source,
                source_root,
                "declares frontmatter but no recognized agent fields",
                "add description/name frontmatter or move the file outside the agents source tree",
            ),
        )

    return AgentSourceClassification(PrimitiveKind.AGENT)


def _is_markdown(path: Path) -> bool:
    return path.name.endswith(".agent.md") or path.suffix == ".md"


def _display_path(path: Path, source_root: Path) -> str:
    try:
        return printable_ascii_text(portable_relpath(path, source_root))
    except ValueError:
        return printable_ascii_text(path.name)


def _skipped_asset_warning(source: Path, source_root: Path) -> str:
    return (
        f"Skipped non-agent asset in agents source tree: {_display_path(source, source_root)}. "
        "APM deploys agent sources as flat Markdown files for this target, so sibling "
        "assets would not be readable at runtime. Action: move the asset to a supported "
        "package surface or inline the content, then rerun 'apm install'."
    )


def _skipped_markdown_warning(source: Path, source_root: Path, reason: str, action: str) -> str:
    return (
        f"Skipped non-agent Markdown in agents source tree: {_display_path(source, source_root)} "
        f"{reason}. Action: {action}."
    )


def _unknown_agent_schema_warning(source: Path, source_root: Path, schema: str) -> str:
    return (
        f"Unrecognized agent source $schema in {_display_path(source, source_root)}: "
        f"{printable_ascii_text(schema)}. APM classified the file by structure instead."
    )
