"""Vendor-neutral intermediate representation for executable hook intent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HookHandler:
    """One portable command handler."""

    command: str | None
    platform: str = "all"
    timeout_seconds: float | None = None
    provenance: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookBinding:
    """Handlers bound to one event and optional matcher."""

    event: str
    handlers: tuple[HookHandler, ...]
    matcher: str | None = None
    provenance: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HookDocument:
    """Portable hook bindings translated only by native edge adapters."""

    bindings: tuple[HookBinding, ...]
