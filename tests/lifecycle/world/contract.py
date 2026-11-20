"""The T-0 / T-0W interface contract of the lifecycle world (#4093, #4094)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

KINDS = frozenset({"skill", "other-file", "hook", "mcp", "lsp", "bin", "canvas", "native"})
SOURCES = frozenset(
    {"git", "local-path", "marketplace", "local-bundle", "registry", "native-plugin"}
)


@dataclass(frozen=True)
class PackageRef:
    name: str  # p-skill, p-exec, p-lsp, p-parent, p-parent2, p-child, p-native-*, p-canvas
    spec: str  # dependency string usable in apm.yml / apm install
    kinds: frozenset[str]  # subset of KINDS
    source: str  # one of SOURCES

    def __post_init__(self) -> None:
        assert self.kinds <= KINDS, self.kinds
        assert self.source in SOURCES, self.source


class Services(Protocol):
    git_base_url: str
    marketplace_url: str
    mcp_registry_url: str
    proxy_url: str

    def policy_url(self, variant: str) -> str: ...

    def host_alias(self, shape: str) -> str: ...

    def egress_log(self) -> list[dict]: ...


@dataclass
class World:
    root: Path
    project: Path
    home: Path
    apm_home: Path
    env: dict[str, str]  # complete hermetic CLI env
    packages: dict[str, PackageRef]
    services: Services
    # Optional extra: APM_CACHE_DIR, read by rows.py for policy cache surfaces.
    cache_root: Path | None = None
    # Optional extra: a directory with no apm.yml, the cwd of user-scope commands.
    user_cwd: Path | None = None
