"""Scanner whose rules are derived from the deployment registry.

Inverting :data:`apm_cli.integration.targets.KNOWN_TARGETS` gives, for every
target, the directories and suffixes each primitive is deployed to. Reading
those same locations back is how discovery finds hand-authored files.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath

from apm_cli.integration.hook_integrator import _MERGE_HOOK_TARGETS
from apm_cli.integration.targets import (
    RULE_FORMATS,
    PrimitiveMapping,
    TargetProfile,
    apply_legacy_skill_paths,
)

from ..model import HarnessKind, RawFinding, Risk, Scope
from ..registry import ScanContext, ScanRule, findings_for_rule

_PRIMITIVE_KINDS: dict[str, HarnessKind] = {
    "instructions": HarnessKind.INSTRUCTION,
    "agents": HarnessKind.AGENT,
    "prompts": HarnessKind.PROMPT,
    "commands": HarnessKind.COMMAND,
    "skills": HarnessKind.SKILL,
    "hooks": HarnessKind.HOOK,
    "canvas": HarnessKind.CANVAS,
}

# Primitive directories whose authors commonly nest files one or more levels
# deep (Claude namespaced commands, Cursor rule folders, Kiro agent trees).
_RECURSIVE_PRIMITIVES = frozenset({"instructions", "agents", "commands", "prompts"})


def _kind_for(primitive: str, mapping: PrimitiveMapping) -> HarnessKind:
    if primitive == "instructions" and mapping.format_id in RULE_FORMATS:
        return HarnessKind.RULE
    return _PRIMITIVE_KINDS.get(primitive, HarnessKind.UNKNOWN)


def _rule_for_mapping(
    profile: TargetProfile, primitive: str, mapping: PrimitiveMapping
) -> ScanRule | None:
    base = mapping.deploy_root or profile.root_dir
    kind = _kind_for(primitive, mapping)
    if primitive == "hooks":
        if profile.name in _MERGE_HOOK_TARGETS:
            return None  # merged-config hooks are read by the hooks scanner
        return ScanRule(
            tool=profile.name,
            kind=HarnessKind.HOOK,
            relative_glob=f"{base}/{mapping.subdir}/*{mapping.extension}",
            format_id=mapping.format_id,
            primitive=primitive,
            risk=frozenset({Risk.EXECUTES_CODE}),
        )
    if mapping.extension.startswith("/"):
        # Directory-shaped primitive (skills): match the marker file, report the dir.
        marker = mapping.extension.lstrip("/")
        return ScanRule(
            tool=profile.name,
            kind=kind,
            relative_glob=f"{base}/{mapping.subdir}/*/{marker}",
            is_dir_rule=True,
            format_id=mapping.format_id,
            primitive=primitive,
        )
    if primitive == "canvas":
        return ScanRule(
            tool=profile.name,
            kind=HarnessKind.CANVAS,
            relative_glob=f"{base}/{mapping.subdir}/*/*",
            is_dir_rule=True,
            format_id=mapping.format_id,
            primitive=primitive,
        )
    if not mapping.subdir:
        # Root-level single file convention (e.g. Copilot user-scope instructions).
        return ScanRule(
            tool=profile.name,
            kind=HarnessKind.ROOT_CONTEXT,
            relative_glob=f"{base}/*{mapping.extension}",
            format_id=mapping.format_id,
            primitive=primitive,
        )
    return ScanRule(
        tool=profile.name,
        kind=kind,
        relative_glob=f"{base}/{mapping.subdir}/*{mapping.extension}",
        recursive=primitive in _RECURSIVE_PRIMITIVES,
        format_id=mapping.format_id,
        primitive=primitive,
    )


def derive_rules(profile: TargetProfile) -> list[ScanRule]:
    """Return the scan rules implied by one scope-resolved target profile."""
    rules: list[ScanRule] = []
    for primitive, mapping in profile.primitives.items():
        rule = _rule_for_mapping(profile, primitive, mapping)
        if rule is not None:
            rules.append(rule)
    # Legacy per-client skill directories (pre skills-convergence layouts).
    for legacy in apply_legacy_skill_paths([profile]):
        mapping = legacy.primitives.get("skills")
        if mapping is None or mapping.deploy_root is not None:
            continue
        rule = _rule_for_mapping(legacy, "skills", mapping)
        if rule is not None and rule not in rules:
            rules.append(rule)
    for generated in profile.generated_files:
        rules.append(
            ScanRule(
                tool=profile.name,
                kind=HarnessKind.ROOT_CONTEXT,
                relative_glob=f"{profile.root_dir}/{generated}",
                format_id="generated",
                primitive="instructions",
            )
        )
    return rules


def collapse_shared_rules(rules: Iterable[ScanRule]) -> list[ScanRule]:
    """Merge identical globs claimed by several tools into one ``shared`` rule."""
    by_glob: dict[tuple[str, HarnessKind], list[ScanRule]] = {}
    for rule in rules:
        by_glob.setdefault((rule.relative_glob, rule.kind), []).append(rule)
    collapsed: list[ScanRule] = []
    for (glob, kind), group in by_glob.items():
        tools = sorted({rule.tool for rule in group})
        first = group[0]
        if len(tools) == 1:
            collapsed.append(first)
            continue
        collapsed.append(
            ScanRule(
                tool="shared",
                kind=kind,
                relative_glob=glob,
                recursive=first.recursive,
                is_dir_rule=first.is_dir_rule,
                format_id=first.format_id,
                primitive=first.primitive,
                risk=first.risk,
                notes=(f"shared by: {', '.join(tools)}",),
            )
        )
    return collapsed


class ProfileFilesScanner:
    """Read back every file-shaped primitive location known to the registry."""

    name = "profile-files"

    def scan(self, ctx: ScanContext) -> Iterable[RawFinding]:
        rules: list[ScanRule] = []
        for profile in ctx.targets:
            rules.extend(derive_rules(profile))
        collapsed = collapse_shared_rules(rules)
        matched: set[str] = set()
        for rule in collapsed:
            for raw in findings_for_rule(ctx, rule):
                if raw.abs_path is not None:
                    matched.add(raw.abs_path.as_posix())
                yield raw
        yield from unrecognised_files(ctx, collapsed, matched)


def unrecognised_files(
    ctx: ScanContext, rules: Iterable[ScanRule], matched: set[str]
) -> Iterable[RawFinding]:
    """Report files sitting in a known primitive directory that no rule claims.

    These are listed as ``unknown`` so a migration report is complete (for
    example ``.cursor/rules/style.md`` where Cursor expects ``.mdc``).
    """
    seen_dirs: set[str] = set()
    for rule in rules:
        if rule.is_dir_rule or rule.kind is HarnessKind.ROOT_CONTEXT:
            continue
        directory = ctx.root / rule.directory
        if rule.directory in seen_dirs or not directory.is_dir():
            continue
        seen_dirs.add(rule.directory)
        count = 0
        for candidate in sorted(directory.iterdir()):
            if not candidate.is_file() or candidate.is_symlink():
                continue
            if candidate.as_posix() in matched or candidate.name.startswith("."):
                continue
            count += 1
            if count > ctx.limits.max_files_per_rule:
                break
            yield RawFinding(
                tool=rule.tool,
                scope=ctx.scope,
                kind=HarnessKind.UNKNOWN,
                display_path=ctx.display(candidate),
                abs_path=candidate,
                size_bytes=candidate.stat().st_size,
                notes=(
                    f"unrecognised file in a {rule.tool} {rule.primitive or ''} directory".replace(
                        "  ", " "
                    ),
                ),
                evidence=(f"dir:{rule.directory}",),
            )


def rules_for_targets(targets: Iterable[TargetProfile]) -> list[ScanRule]:
    """Public helper (used by tests) mirroring what the scanner evaluates."""
    rules: list[ScanRule] = []
    for profile in targets:
        rules.extend(derive_rules(profile))
    return collapse_shared_rules(rules)


__all__ = [
    "ProfileFilesScanner",
    "collapse_shared_rules",
    "derive_rules",
    "rules_for_targets",
]

_ = (Scope, PurePosixPath)  # re-exported names used by sibling scanners' type hints
