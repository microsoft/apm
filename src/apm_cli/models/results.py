"""Typed result containers for APM operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.target_detection import EffectiveTargetDecision


class InstallDisposition(str, Enum):
    """Canonical completion state for an install attempt."""

    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial-success"
    VALIDATION_FAILED = "validation-failed"
    CANCELLED = "cancelled"
    DRY_RUN = "dry-run"
    FAILED = "failed"


@dataclass
class InstallResult:
    """Result of an APM install operation."""

    installed_count: int = 0
    prompts_integrated: int = 0
    agents_integrated: int = 0
    diagnostics: object = None  # DiagnosticCollector or None
    package_types: dict[str, str] = field(default_factory=dict)  # dep_key -> type string
    disposition: InstallDisposition = InstallDisposition.SUCCESS
    exit_code: int = 0
    committed: bool = False
    error: BaseException | None = field(default=None, repr=False)
    target_decision: EffectiveTargetDecision | None = None


@dataclass
class PrimitiveCounts:
    """Counts of primitives in a package."""

    prompts: int = 0
    agents: int = 0
    instructions: int = 0
    skills: int = 0
    hooks: int = 0
    commands: int = 0
