from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import Path


@dataclasses.dataclass(frozen=True, slots=True)
class MCPInstallParams:
    mcp_name: str
    transport: str | None
    url: str | None
    env_pairs: Sequence[str] | None
    header_pairs: Sequence[str] | None
    mcp_version: str | None
    command_argv: Sequence[str] | None
    dev: bool
    force: bool
    runtime: str | None
    exclude: str | None
    verbose: bool
    logger: object
    manifest_path: Path
    apm_dir: Path
    scope: str | None
    registry_url: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class MCPConflictParams:
    mcp_name: str | None
    packages: Sequence[str]
    pre_dash_packages: Sequence[str]
    transport: str | None
    url: str | None
    env: Mapping[str, str]
    headers: Mapping[str, str]
    mcp_version: str | None
    command_argv: Sequence[str] | None
    global_: bool
    only: str | None
    update: bool
    use_ssh: bool
    use_https: bool
    allow_protocol_fallback: bool
    registry_url: str | None = None
