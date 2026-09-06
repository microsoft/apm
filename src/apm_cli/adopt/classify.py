"""Importability classification table for discovered harness files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from apm_cli.integration.targets import RULE_FORMATS

from .model import Finding, HarnessKind, Importability, Ownership, RawFinding, Risk, finding_id

ANY = "*"


@dataclass(frozen=True)
class ClassRule:
    """How one (tool, kind) pair is imported."""

    importability: Importability
    converter: str | None = None
    """Converter id; ``{format}`` expands to the finding's ``format_id``."""
    risk: frozenset[Risk] = frozenset()
    note: str | None = None


_TABLE: dict[tuple[str, HarnessKind], ClassRule] = {
    (ANY, HarnessKind.INSTRUCTION): ClassRule(Importability.APM_NATIVE, "passthrough.instruction"),
    (ANY, HarnessKind.PROMPT): ClassRule(Importability.APM_NATIVE, "passthrough.prompt"),
    (ANY, HarnessKind.AGENT): ClassRule(Importability.CONVERTIBLE, "{format}->agent"),
    ("copilot", HarnessKind.AGENT): ClassRule(Importability.APM_NATIVE, "passthrough.agent"),
    (ANY, HarnessKind.SKILL): ClassRule(Importability.APM_NATIVE, "passthrough.skill_dir"),
    (ANY, HarnessKind.RULE): ClassRule(Importability.CONVERTIBLE, "{format}->instruction"),
    (ANY, HarnessKind.COMMAND): ClassRule(Importability.CONVERTIBLE, "{format}->prompt"),
    (ANY, HarnessKind.HOOK): ClassRule(
        Importability.CONVERTIBLE, "{format}->apm_hooks", frozenset({Risk.EXECUTES_CODE})
    ),
    ("copilot", HarnessKind.HOOK): ClassRule(
        Importability.APM_NATIVE, "passthrough.hook", frozenset({Risk.EXECUTES_CODE})
    ),
    (ANY, HarnessKind.HOOK_SCRIPT): ClassRule(
        Importability.REFERENCE_ONLY,
        None,
        frozenset({Risk.EXECUTES_CODE}),
        "referenced, never executed; use --apply --include-hook-scripts to copy",
    ),
    (ANY, HarnessKind.MCP_SERVER): ClassRule(Importability.CONVERTIBLE, "mcp->dependencies.mcp"),
    (ANY, HarnessKind.ROOT_CONTEXT): ClassRule(
        Importability.CONVERTIBLE,
        "root_context->instruction",
        note="root context file; verify it is agent configuration, not documentation",
    ),
    (ANY, HarnessKind.STYLE): ClassRule(
        Importability.REFERENCE_ONLY, None, note="output style guides have no APM primitive yet"
    ),
    (ANY, HarnessKind.PLUGIN): ClassRule(Importability.REFERENCE_ONLY),
    (ANY, HarnessKind.CANVAS): ClassRule(Importability.REFERENCE_ONLY),
    (ANY, HarnessKind.UNKNOWN): ClassRule(Importability.IGNORED),
}


def register_rule(tool: str, kind: HarnessKind, rule: ClassRule) -> None:
    """Extension point: add or override one classification row."""
    _TABLE[(tool, kind)] = rule


def lookup(tool: str, kind: HarnessKind) -> ClassRule:
    return _TABLE.get((tool, kind)) or _TABLE.get((ANY, kind)) or ClassRule(Importability.IGNORED)


def classification_rows() -> dict[tuple[str, HarnessKind], ClassRule]:
    """Return a copy of the active table (for docs and tests)."""
    return dict(_TABLE)


_DEST_SUBDIR: dict[HarnessKind, tuple[str, str]] = {
    HarnessKind.INSTRUCTION: ("instructions", ".instructions.md"),
    HarnessKind.RULE: ("instructions", ".instructions.md"),
    HarnessKind.ROOT_CONTEXT: ("instructions", ".instructions.md"),
    HarnessKind.AGENT: ("agents", ".agent.md"),
    HarnessKind.PROMPT: ("prompts", ".prompt.md"),
    HarnessKind.COMMAND: ("prompts", ".prompt.md"),
    HarnessKind.SKILL: ("skills", ""),
    HarnessKind.HOOK: ("hooks", ".json"),
}


def _stem(raw: RawFinding) -> str:
    name = PurePosixPath(raw.display_path).name
    for suffix in (".instructions.md", ".agent.md", ".prompt.md", ".mdc", ".toml", ".json", ".md"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def proposed_destination(raw: RawFinding) -> str | None:
    """Return the ``.apm/`` path a convertible finding would land at (pre-collision)."""
    if raw.kind is HarnessKind.MCP_SERVER:
        return "apm.yml#dependencies.mcp"
    if raw.kind is HarnessKind.ROOT_CONTEXT:
        return f".apm/instructions/{raw.tool}-root.instructions.md"
    entry = _DEST_SUBDIR.get(raw.kind)
    if entry is None:
        return None
    subdir, suffix = entry
    if raw.kind is HarnessKind.SKILL:
        return f".apm/skills/{PurePosixPath(raw.display_path).name}/SKILL.md"
    if raw.kind is HarnessKind.HOOK:
        return f".apm/hooks/{raw.tool}-native.json"
    return f".apm/{subdir}/{_stem(raw)}{suffix}"


def classify(raw: RawFinding, ownership: Ownership, evidence: tuple[str, ...] = ()) -> Finding:
    """Combine table lookup with ownership into a final finding."""
    rule = lookup(raw.tool, raw.kind)
    importability = rule.importability
    converter = (
        rule.converter.replace("{format}", raw.format_id or "unknown") if rule.converter else None
    )
    notes = list(raw.notes)
    if rule.note:
        notes.append(rule.note)
    if raw.format_id == "private":
        importability = Importability.IGNORED
        converter = None
        notes.append("private local file; never imported")
    if raw.format_id in RULE_FORMATS and raw.kind is HarnessKind.INSTRUCTION:
        importability = Importability.CONVERTIBLE
    if ownership in (Ownership.APM_OWNED, Ownership.APM_GENERATED):
        importability = Importability.IGNORED
        converter = None
        notes.append("already managed by APM")
    elif ownership is Ownership.AMBIGUOUS:
        notes.append("ambiguous ownership; review before --apply")
    proposed = (
        proposed_destination(raw)
        if importability
        in (
            Importability.APM_NATIVE,
            Importability.CONVERTIBLE,
        )
        else None
    )
    return Finding(
        id=finding_id(raw.tool, raw.scope, raw.kind, raw.display_path),
        tool=raw.tool,
        scope=raw.scope,
        kind=raw.kind,
        display_path=raw.display_path,
        importability=importability,
        ownership=ownership,
        risk=raw.risk | rule.risk,
        converter_id=converter,
        proposed_target=proposed,
        size_bytes=raw.size_bytes,
        notes=tuple(notes),
        evidence=tuple(raw.evidence) + tuple(evidence),
        abs_path=raw.abs_path,
        format_id=raw.format_id,
        primitive=raw.primitive,
        payload=raw.payload,
    )
