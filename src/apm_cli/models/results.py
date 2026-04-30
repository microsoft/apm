"""Typed result containers for APM operations."""

from dataclasses import dataclass, field
from typing import Dict  # noqa: F401, UP035


@dataclass
class InstallResult:
    """Result of an APM install operation."""

    installed_count: int = 0
    prompts_integrated: int = 0
    agents_integrated: int = 0
    diagnostics: object = None  # DiagnosticCollector or None
    package_types: dict[str, str] = field(default_factory=dict)  # dep_key -> type string


@dataclass
class PrimitiveCounts:
    """Counts of primitives in a package."""

    prompts: int = 0
    agents: int = 0
    instructions: int = 0
    skills: int = 0
    hooks: int = 0
    commands: int = 0
