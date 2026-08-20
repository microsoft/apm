"""Vendor-neutral intermediate representation for executable hook intent."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


def _freeze(value: Any) -> Any:
    """Recursively snapshot portable metadata."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class HookHandler:
    """One portable command handler."""

    command: str | None
    platform: str = "all"
    timeout_seconds: float | None = None
    provenance: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class HookBinding:
    """Handlers bound to one event and optional matcher."""

    event: str
    handlers: tuple[HookHandler, ...]
    matcher: str | None = None
    provenance: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class HookDocument:
    """Portable hook bindings translated only by native edge adapters."""

    bindings: tuple[HookBinding, ...]
