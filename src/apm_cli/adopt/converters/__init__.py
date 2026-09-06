"""Converter contract: vendor-native file -> APM source layout, with loss accounting.

Every converter is a pure function of the source bytes. It writes only under
the destination it is handed, reports every dropped or transformed field by
*path* (never by value), and never touches the source.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from ..model import Finding, HarnessKind, Scope
from ..redact import Redactor
from ..registry import ScanLimits

ChangeAction = Literal["preserved", "transformed", "defaulted", "dropped", "redacted"]


class ConvertError(Exception):
    """A converter could not produce a valid APM file from the source."""


@dataclass(frozen=True)
class FieldChange:
    """One field-level transformation record (field paths only, never values)."""

    path: str
    action: ChangeAction
    reason: str
    severity: Literal["info", "warning"] = "info"

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
            "severity": self.severity,
        }


@dataclass
class ConvertResult:
    """What a converter wrote and what it changed."""

    written: list[Path] = field(default_factory=list)
    changes: list[FieldChange] = field(default_factory=list)
    manifest_fragment: dict[str, Any] | None = None
    skipped_reason: str | None = None

    @property
    def lossy(self) -> bool:
        return any(c.action == "dropped" for c in self.changes)

    def drop(self, path: str, reason: str) -> None:
        self.changes.append(FieldChange(path, "dropped", reason, "warning"))

    def transform(
        self, path: str, reason: str, severity: Literal["info", "warning"] = "info"
    ) -> None:
        self.changes.append(FieldChange(path, "transformed", reason, severity))

    def default(self, path: str, reason: str) -> None:
        self.changes.append(FieldChange(path, "defaulted", reason))

    def keep(self, path: str, reason: str = "") -> None:
        self.changes.append(FieldChange(path, "preserved", reason))

    def redacted(self, path: str, reason: str) -> None:
        self.changes.append(FieldChange(path, "redacted", reason, "warning"))


@dataclass
class ConvertContext:
    """Environment shared by every converter in one write run."""

    project_root: Path
    scope: Scope
    redactor: Redactor
    limits: ScanLimits = field(default_factory=ScanLimits)
    include_hook_scripts: bool = False
    preferred_tools: tuple[str, ...] = ()


@runtime_checkable
class Converter(Protocol):
    """A named transformation from one harness format into APM's layout."""

    id: str

    def handles(self, converter_id: str) -> bool: ...

    def convert(self, finding: Finding, dest: Path, *, ctx: ConvertContext) -> ConvertResult: ...


class ConverterRegistry:
    """Lookup table from converter id to implementation."""

    def __init__(self) -> None:
        self._converters: list[Converter] = []

    def register(self, converter: Converter) -> Converter:
        if any(existing.id == converter.id for existing in self._converters):
            raise ValueError(f"converter {converter.id!r} is already registered")
        self._converters.append(converter)
        return converter

    def get(self, converter_id: str | None) -> Converter | None:
        if not converter_id:
            return None
        for converter in self._converters:
            if converter.id == converter_id:
                return converter
        for converter in self._converters:
            if converter.handles(converter_id):
                return converter
        return None

    def ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self._converters)


CONVERTERS = ConverterRegistry()


def register_converter(converter: Converter) -> Converter:
    """Register *converter* in the global registry (decorator friendly)."""
    return CONVERTERS.register(converter)


def register_builtin_converters(registry: ConverterRegistry = CONVERTERS) -> ConverterRegistry:
    """Populate *registry* with the built-in converters (idempotent)."""
    from . import agents, commands, hooks, mcp, passthrough, root_context, rules, skills

    existing = set(registry.ids())
    for module in (passthrough, rules, root_context, agents, commands, skills, hooks, mcp):
        for converter in module.CONVERTERS:
            if converter.id not in existing:
                registry.register(converter)
                existing.add(converter.id)
    return registry


def summarize_changes(changes: Iterable[FieldChange]) -> str:
    """One-line loss summary for tables."""
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.action] = counts.get(change.action, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "lossless"


__all__ = [
    "CONVERTERS",
    "ConvertContext",
    "ConvertError",
    "ConvertResult",
    "Converter",
    "ConverterRegistry",
    "FieldChange",
    "HarnessKind",
    "register_builtin_converters",
    "register_converter",
    "summarize_changes",
]
