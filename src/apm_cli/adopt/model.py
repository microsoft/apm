"""Data model for brownfield adoption (``apm init --discover``).

Every type here is serialisable to a redacted dictionary. ``Finding`` never
carries file content; ``abs_path`` is kept for the write path but is dropped
from every rendered form.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Scope(str, Enum):
    """Where a harness file was found."""

    PROJECT = "project"
    USER = "user"


class HarnessKind(str, Enum):
    """What a discovered file is, in harness-neutral vocabulary."""

    INSTRUCTION = "instruction"
    RULE = "rule"
    AGENT = "agent"
    PROMPT = "prompt"
    COMMAND = "command"
    SKILL = "skill"
    HOOK = "hook"
    HOOK_SCRIPT = "hook-script"
    MCP_SERVER = "mcp-server"
    ROOT_CONTEXT = "root-context"
    STYLE = "style"
    PLUGIN = "plugin"
    CANVAS = "canvas"
    UNKNOWN = "unknown"


class Importability(str, Enum):
    """How APM can take a discovered file under management."""

    APM_NATIVE = "apm-native"
    CONVERTIBLE = "convertible"
    REFERENCE_ONLY = "reference-only"
    IGNORED = "ignored"


class Ownership(str, Enum):
    """Who authored the file on disk."""

    APM_OWNED = "apm-owned"
    APM_GENERATED = "apm-generated"
    HOST_OWNED = "host-owned"
    AMBIGUOUS = "ambiguous"


class Risk(str, Enum):
    """What the artifact can do once an agent loads it."""

    EXECUTES_CODE = "executes-code"
    NETWORK = "network"
    WRITES_FILES = "writes-files"


def finding_id(tool: str, scope: Scope, kind: HarnessKind, display_path: str) -> str:
    """Return the stable identity of a finding across runs."""
    raw = f"{tool}|{scope.value}|{kind.value}|{display_path}".encode()
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:12]


@dataclass(frozen=True)
class RawFinding:
    """A scanner hit before classification and ownership resolution."""

    tool: str
    scope: Scope
    kind: HarnessKind
    display_path: str
    abs_path: Path | None = None
    size_bytes: int | None = None
    format_id: str | None = None
    primitive: str | None = None
    risk: frozenset[Risk] = frozenset()
    notes: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    ownership: Ownership | None = None
    """Pre-computed ownership from scanners that inspect structure (hooks, MCP)."""
    payload: Any = None
    """Scanner-private data consumed by converters (already redacted where shown)."""


@dataclass(frozen=True)
class Finding:
    """One classified, redacted discovery result."""

    id: str
    tool: str
    scope: Scope
    kind: HarnessKind
    display_path: str
    importability: Importability
    ownership: Ownership
    risk: frozenset[Risk] = frozenset()
    converter_id: str | None = None
    proposed_target: str | None = None
    size_bytes: int | None = None
    notes: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    abs_path: Path | None = field(default=None, compare=False, repr=False)
    format_id: str | None = field(default=None, compare=False, repr=False)
    primitive: str | None = field(default=None, compare=False, repr=False)
    payload: Any = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return the redacted, machine-readable form (no absolute paths)."""
        return {
            "id": self.id,
            "tool": self.tool,
            "scope": self.scope.value,
            "kind": self.kind.value,
            "path": self.display_path,
            "importability": self.importability.value,
            "ownership": self.ownership.value,
            "risk": sorted(r.value for r in self.risk),
            "converter": self.converter_id,
            "proposed_target": self.proposed_target,
            "size_bytes": self.size_bytes,
            "notes": list(self.notes),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class ScanError:
    """A path the scanner saw but could not evaluate."""

    display_path: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class AdoptionReport:
    """The complete result of one discovery run."""

    project_root_display: str
    scopes: tuple[Scope, ...]
    detected_tools: tuple[str, ...]
    findings: tuple[Finding, ...]
    errors: tuple[ScanError, ...]
    proposed_targets: tuple[str, ...]
    proposed_mcp: tuple[dict[str, Any], ...]
    apm_yml_exists: bool
    schema_version: int = 1

    def counts(self) -> dict[str, dict[str, int]]:
        """Return finding counts by importability, ownership, tool and kind."""
        result: dict[str, dict[str, int]] = {
            "importability": {},
            "ownership": {},
            "tool": {},
            "kind": {},
        }
        for finding in self.findings:
            for bucket, key in (
                ("importability", finding.importability.value),
                ("ownership", finding.ownership.value),
                ("tool", finding.tool),
                ("kind", finding.kind.value),
            ):
                result[bucket][key] = result[bucket].get(key, 0) + 1
        return result

    def to_dict(self) -> dict[str, Any]:
        """Return the redacted, machine-readable report."""
        return {
            "schema_version": self.schema_version,
            "project_root": self.project_root_display,
            "scopes": [s.value for s in self.scopes],
            "detected_tools": list(self.detected_tools),
            "apm_yml_exists": self.apm_yml_exists,
            "proposed_targets": list(self.proposed_targets),
            "proposed_mcp": [dict(entry) for entry in self.proposed_mcp],
            "counts": self.counts(),
            "findings": [f.to_dict() for f in self.findings],
            "errors": [e.to_dict() for e in self.errors],
        }
