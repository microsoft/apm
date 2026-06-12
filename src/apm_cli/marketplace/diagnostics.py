"""Shared marketplace build diagnostics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BuildDiagnostic:
    """Structured diagnostic emitted during marketplace output composition."""

    level: str  # "warning" | "verbose"
    message: str
