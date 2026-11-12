"""Immutable intermediate representation for Agent Plugins."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from .errors import AgentPluginError

JsonScalar: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class FrozenJsonArray:
    """Immutable JSON array that remains distinct from an object."""

    values: tuple[FrozenJsonValue, ...]

    def thaw(self) -> list[JsonValue]:
        """Return a mutable JSON-compatible array."""
        return [thaw_frozen_json(value) for value in self.values]


@dataclass(frozen=True, slots=True)
class FrozenJsonObject:
    """Immutable, deterministically ordered JSON object."""

    items: tuple[tuple[str, FrozenJsonValue], ...]

    def __iter__(self) -> Iterator[tuple[str, FrozenJsonValue]]:
        """Iterate deterministic key/value pairs."""
        return iter(self.items)

    def thaw(self) -> dict[str, JsonValue]:
        """Return a mutable JSON-compatible object."""
        return {key: thaw_frozen_json(value) for key, value in self.items}


FrozenJsonValue: TypeAlias = JsonScalar | FrozenJsonArray | FrozenJsonObject
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def thaw_frozen_json(value: FrozenJsonValue) -> JsonValue:
    """Thaw one immutable JSON value without guessing its container type."""
    if isinstance(value, (FrozenJsonArray, FrozenJsonObject)):
        return value.thaw()
    return value


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
class AgentPluginAsset:
    """One safely inventoried regular file beneath the plugin root."""

    path: str
    source: SourceProvenance
    sha256: str
    size: int
    executable_mode: int


@dataclass(frozen=True, slots=True)
class AgentPluginExecutable:
    """One executable or script reference from a validated declaration."""

    declaration: str
    plugin_relative_path: str | None
    asset: AgentPluginAsset | None
    provenance: SourceProvenance


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
    assets: tuple[AgentPluginAsset, ...] = ()


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
    executables: tuple[AgentPluginExecutable, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentPluginComponents:
    """Resolved component declarations from fixed v1 locations."""

    skills: tuple[AgentPluginSkill, ...]
    mcp_servers: tuple[AgentPluginMcpServer, ...]


@dataclass(frozen=True, slots=True)
class ApmExtensionData:
    """Validated com.microsoft.apm manifest extension data."""

    schema_version: str
    values: FrozenJsonObject
    provenance: SourceProvenance


@dataclass(frozen=True, slots=True)
class ApmConfiguration:
    """APM-only dependency, policy, and build configuration from apm.yml."""

    values: FrozenJsonObject
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
