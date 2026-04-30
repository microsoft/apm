"""Frozen dataclasses modeling the full apm-policy.yml schema.

Every field maps 1:1 to a concrete ``apm audit`` check.

Allow-list semantics:
  * ``None``  -- "no opinion" (transparent during inheritance merge).
  * ``()``    -- "explicitly empty" (after merge: nothing is allowed).
  * ``(...)`` -- "allow only matching patterns".

Deny/require lists use ``Tuple[str, ...]`` (never ``None``) because
they accumulate via union -- an absent key simply means "nothing to add".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple  # noqa: F401, UP035


@dataclass(frozen=True)
class PolicyCache:
    """Cache configuration for remote policy resolution."""

    ttl: int = 3600  # seconds, default 1 hour


@dataclass(frozen=True)
class DependencyPolicy:
    """Rules governing which APM dependencies are permitted."""

    allow: tuple[str, ...] | None = None
    deny: tuple[str, ...] = ()
    require: tuple[str, ...] = ()
    require_resolution: str = "project-wins"  # project-wins | policy-wins | block
    max_depth: int = 50


@dataclass(frozen=True)
class McpTransportPolicy:
    """Allowed MCP transport protocols."""

    allow: tuple[str, ...] | None = None  # stdio, sse, http, streamable-http


@dataclass(frozen=True)
class McpPolicy:
    """Rules governing MCP server references."""

    allow: tuple[str, ...] | None = None
    deny: tuple[str, ...] = ()
    transport: McpTransportPolicy = field(default_factory=McpTransportPolicy)
    self_defined: str = "warn"  # deny | warn | allow
    trust_transitive: bool = False


@dataclass(frozen=True)
class CompilationTargetPolicy:
    """Allowed compilation targets."""

    allow: tuple[str, ...] | None = None  # vscode, claude, all
    enforce: str | None = None


@dataclass(frozen=True)
class CompilationStrategyPolicy:
    """Compilation strategy constraints."""

    enforce: str | None = None  # distributed | single-file


@dataclass(frozen=True)
class CompilationPolicy:
    """Rules governing prompt compilation."""

    target: CompilationTargetPolicy = field(default_factory=CompilationTargetPolicy)
    strategy: CompilationStrategyPolicy = field(default_factory=CompilationStrategyPolicy)
    source_attribution: bool = False


@dataclass(frozen=True)
class ManifestPolicy:
    """Rules governing apm.yml manifest content."""

    required_fields: tuple[str, ...] = ()
    scripts: str = "allow"  # allow | deny
    content_types: dict | None = None  # {"allow": [...]}
    require_explicit_includes: bool = False


@dataclass(frozen=True)
class UnmanagedFilesPolicy:
    """Rules for files not tracked in apm.lock."""

    action: str = "ignore"  # ignore | warn | deny
    directories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApmPolicy:
    """Top-level APM policy model."""

    name: str = ""
    version: str = ""
    extends: str | None = None  # "org", "<owner>/<repo>", or URL
    enforcement: str = "warn"  # warn | block | off
    fetch_failure: str = "warn"  # warn | block (closes #829)
    cache: PolicyCache = field(default_factory=PolicyCache)
    dependencies: DependencyPolicy = field(default_factory=DependencyPolicy)
    mcp: McpPolicy = field(default_factory=McpPolicy)
    compilation: CompilationPolicy = field(default_factory=CompilationPolicy)
    manifest: ManifestPolicy = field(default_factory=ManifestPolicy)
    unmanaged_files: UnmanagedFilesPolicy = field(default_factory=UnmanagedFilesPolicy)
