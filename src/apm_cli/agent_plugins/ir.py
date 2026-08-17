"""Immutable intermediate representation for Agent Plugins."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from .errors import AgentPluginError

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJsonValue: TypeAlias = (
    JsonScalar | tuple["FrozenJsonValue", ...] | tuple[tuple[str, "FrozenJsonValue"], ...]
)


class DiagnosticSeverity(str, Enum):
    """Stable severity vocabulary for loader diagnostics."""

    WARNING = "warning"
    ERROR = "error"


class McpServerType(str, Enum):
    """Portable MCP transports defined by Agent Plugins v1."""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"
    SSE = "sse"


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    """Source location for one interpreted contract value."""

    path: Path
    json_pointer: str


@dataclass(frozen=True, slots=True)
class AgentPluginDiagnostic:
    """Deterministic diagnostic emitted while loading optional components."""

    code: str
    severity: DiagnosticSeverity
    message: str
    path: str
    component: str | None = None


@dataclass(frozen=True, slots=True)
class AgentPluginIdentity:
    """Portable identity owned exclusively by root plugin.json."""

    name: str
    version: str | None
    description: str | None
    author: tuple[tuple[str, str], ...]
    homepage: str | None
    repository: str | None
    license: str | None
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentPluginSkill:
    """One resolved immediate child skill declaration."""

    directory_name: str
    name: str
    description: str
    root: Path
    manifest: SourceProvenance


@dataclass(frozen=True, slots=True)
class AgentPluginMcpServer:
    """One validated portable MCP server declaration."""

    name: str
    server_type: McpServerType
    command: str | None
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    cwd: str | None
    url: str | None
    headers: tuple[tuple[str, str], ...]
    provenance: SourceProvenance


@dataclass(frozen=True, slots=True)
class AgentPluginComponents:
    """Resolved component declarations from fixed v1 locations."""

    skills: tuple[AgentPluginSkill, ...]
    mcp_servers: tuple[AgentPluginMcpServer, ...]


@dataclass(frozen=True, slots=True)
class ApmExtensionData:
    """Validated com.microsoft.apm manifest extension data."""

    schema_version: str
    values: tuple[tuple[str, FrozenJsonValue], ...]
    provenance: SourceProvenance


@dataclass(frozen=True, slots=True)
class ApmConfiguration:
    """APM-only dependency, policy, and build configuration from apm.yml."""

    values: tuple[tuple[str, FrozenJsonValue], ...]
    provenance: Path


@dataclass(frozen=True, slots=True)
class AgentPlugin:
    """Canonical versioned Agent Plugin contract IR."""

    specification_version: str
    root: Path
    manifest: SourceProvenance
    identity: AgentPluginIdentity
    components: AgentPluginComponents
    apm_extension: ApmExtensionData | None
    apm_configuration: ApmConfiguration | None
    diagnostics: tuple[AgentPluginDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class AgentPluginDetection:
    """Classification result for an Agent Plugins schema-family manifest."""

    manifest_path: Path
    schema_id: str
    plugin: AgentPlugin | None = None
    error: AgentPluginError | None = None
