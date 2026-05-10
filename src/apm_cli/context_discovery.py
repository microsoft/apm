"""Brownfield agent context discovery for ``apm init --discover``."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from .constants import DEFAULT_SKIP_DIRS
from .core.target_detection import detect_target
from .primitives.discovery import _glob_match
from .utils.paths import portable_relpath
from .utils.yaml_io import load_yaml, yaml_to_str

APM_YML = "apm.yml"

IMPORT_APM_NATIVE = "apm-native"
IMPORT_CONVERTIBLE = "convertible"
IMPORT_REFERENCE_ONLY = "reference-only"
IMPORT_IGNORED = "ignored"

_TOKEN_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{10,}\b")
_URL_USERINFO_RE = re.compile(r"\b(https?://)[^/\s@]+@([^/\s]+)")

# ---------------------------------------------------------------------------
# Migration plan: convertible findings -> .apm/ primitives
# ---------------------------------------------------------------------------

# (tool, kind) -> (target subdir relative to project root, target extension or None=keep original)
_MIGRATION_MAP: dict[tuple[str, str], tuple[str, str | None]] = {
    ("claude", "command"): (".apm/prompts", ".prompt.md"),
    ("claude", "agent"): (".apm/agents", ".agent.md"),
    ("claude", "root-instructions"): (".apm/instructions", ".instructions.md"),
    ("claude", "hook-script"): (".apm/hooks/scripts", None),
    ("codex", "agent"): (".apm/agents", ".agent.md"),
    ("codex", "command"): (".apm/prompts", ".prompt.md"),
    ("codex", "root-instructions"): (".apm/instructions", ".instructions.md"),
    ("codex", "skill"): (".apm/skills", None),
    ("codex", "instruction"): (".apm/instructions", ".instructions.md"),
    ("cursor", "rule"): (".apm/instructions", ".instructions.md"),
    ("cursor", "agent"): (".apm/agents", ".agent.md"),
    ("opencode", "command"): (".apm/prompts", ".prompt.md"),
    ("opencode", "agent"): (".apm/agents", ".agent.md"),
    ("gemini", "command"): (".apm/prompts", ".prompt.md"),
    ("gemini", "root-instructions"): (".apm/instructions", ".instructions.md"),
    ("windsurf", "rule"): (".apm/instructions", ".instructions.md"),
    ("windsurf", "workflow"): (".apm/prompts", ".prompt.md"),
    ("copilot", "command"): (".apm/prompts", ".prompt.md"),
    ("copilot", "hook"): (".apm/hooks", None),
    ("copilot", "hook-script"): (".apm/hooks/scripts", None),
    ("copilot", "instruction"): (".apm/instructions", ".instructions.md"),
    ("copilot", "skill"): (".apm/skills", None),
    ("copilot", "agent"): (".apm/agents", ".agent.md"),
    ("agents", "root-instructions"): (".apm/instructions", ".instructions.md"),
    ("agents", "style"): (".apm/styles", ".style.md"),
    # Skills: the SKILL.md file anchors the discovery, but the entire parent
    # directory is the deployable unit.  Extension None = copy dir as-is.
    ("claude", "skill"): (".apm/skills", None),
    ("agent-skills", "skill"): (".apm/skills", None),
    ("cursor", "skill"): (".apm/skills", None),
    ("opencode", "skill"): (".apm/skills", None),
    ("gemini", "skill"): (".apm/skills", None),
    ("windsurf", "skill"): (".apm/skills", None),
    # APM-native files misplaced outside .apm/ or .github/ (e.g. .claude/agents/*.agent.md)
    ("apm", "agent"): (".apm/agents", None),
    ("apm", "instruction"): (".apm/instructions", None),
    ("apm", "chatmode"): (".apm/chatmodes", None),
    ("apm", "context"): (".apm/context", None),
    ("apm", "prompt"): (".apm/prompts", None),
    ("apm", "hook"): (".apm/hooks", None),
    ("apm", "hook-script"): (".apm/hooks/scripts", None),
    ("apm", "style"): (".apm/styles", None),
    ("apm", "command"): (".apm/prompts", None),
}

# Known compound APM extensions -- stripped before applying target extension.
_APM_EXTENSIONS = (
    ".prompt.md",
    ".agent.md",
    ".instructions.md",
    ".chatmode.md",
    ".style.md",
    ".context.md",
    ".memory.md",
)


@dataclass(frozen=True)
class MigrationAction:
    """One file (or directory for skills) to copy into .apm/."""

    source: Path
    dest: Path
    tool: str
    kind: str
    is_dir: bool = False
    wrap_as_skill: bool = False


def _migration_dest_name(source_name: str, target_ext: str | None) -> str:
    """Return the destination filename for a migration action."""
    if target_ext is None:
        return source_name
    if source_name.endswith(target_ext):
        return source_name
    for known in _APM_EXTENSIONS:
        if source_name.endswith(known):
            return source_name[: -len(known)] + target_ext
    return Path(source_name).stem + target_ext


def compute_migration_plan(
    findings: tuple[ContextDiscoveryFinding, ...],
    project_root: Path,
) -> list[MigrationAction]:
    """Return the file copies needed to migrate findings into .apm/.

    Includes project-scoped convertible findings with a known migration mapping,
    plus APM-native files that live outside the standard ``.apm/`` and
    ``.github/`` locations (e.g. ``.claude/agents/*.agent.md``).
    Already-migrated files (dest already exists) are excluded so that running
    ``--write`` twice is safe.
    """
    actions: list[MigrationAction] = []
    seen_dests: set[Path] = set()
    apm_dir = project_root / ".apm"
    github_dir = project_root / ".github"

    for finding in findings:
        if finding.scope != "project":
            continue
        if finding.importability == IMPORT_APM_NATIVE:
            # Only migrate APM-native files that are misplaced outside standard locations.
            if finding.path.is_relative_to(apm_dir) or finding.path.is_relative_to(github_dir):
                continue
        elif finding.importability != IMPORT_CONVERTIBLE:
            continue

        mapping = _MIGRATION_MAP.get((finding.tool, finding.kind))
        if mapping is None:
            continue

        target_subdir, target_ext = mapping

        # Skills: SKILL.md-anchored findings migrate the whole parent directory;
        # plain .md skill files (e.g. Claude .claude/skills/*.md) are wrapped
        # into a proper skill directory with SKILL.md so the install pipeline
        # can discover and deploy them.
        if finding.kind == "skill" and finding.path.name == "SKILL.md":
            skill_dir = finding.path.parent
            skill_name = skill_dir.name
            dest = project_root / target_subdir / skill_name
            if dest in seen_dests or dest.exists():
                continue
            seen_dests.add(dest)
            actions.append(
                MigrationAction(
                    source=skill_dir, dest=dest, tool=finding.tool, kind=finding.kind, is_dir=True
                )
            )
            continue

        if finding.kind == "skill":
            # Plain .md skill: wrap into <stem>/SKILL.md directory structure
            # so the install pipeline can find and deploy it.
            skill_name = finding.path.stem
            dest = project_root / target_subdir / skill_name
            if dest in seen_dests or dest.exists():
                continue
            seen_dests.add(dest)
            actions.append(
                MigrationAction(
                    source=finding.path,
                    dest=dest,
                    tool=finding.tool,
                    kind=finding.kind,
                    is_dir=False,
                    wrap_as_skill=True,
                )
            )
            continue

        dest_name = _migration_dest_name(finding.path.name, target_ext)
        dest = project_root / target_subdir / dest_name

        if dest in seen_dests:
            dest = project_root / target_subdir / f"{finding.tool}-{dest_name}"
        seen_dests.add(dest)

        if dest.exists():
            continue  # already migrated -- skip silently

        actions.append(
            MigrationAction(source=finding.path, dest=dest, tool=finding.tool, kind=finding.kind)
        )

    return actions


def execute_migration(actions: list[MigrationAction]) -> list[MigrationAction]:
    """Copy files according to *actions*, creating .apm/ subdirs as needed.

    Returns the actions that were applied.  Skips any whose destination already
    exists (safe to call multiple times).
    """
    applied: list[MigrationAction] = []
    for action in actions:
        if action.dest.exists():
            continue
        action.dest.parent.mkdir(parents=True, exist_ok=True)
        if action.is_dir:
            shutil.copytree(action.source, action.dest)
        elif action.wrap_as_skill:
            # Plain .md skill: create <dest>/SKILL.md from the source file
            # so the install pipeline recognises it as a deployable skill.
            action.dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(action.source, action.dest / "SKILL.md")
        else:
            shutil.copy2(action.source, action.dest)
        applied.append(action)
    return applied


@dataclass(frozen=True)
class ContextDiscoveryRule:
    """A known agent context path pattern."""

    tool: str
    kind: str
    patterns: tuple[str, ...]
    importability: str
    reason: str


@dataclass(frozen=True)
class ContextDiscoveryFinding:
    """One discovered context/config file."""

    path: Path
    display_path: str
    scope: str
    tool: str
    kind: str
    importability: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.display_path,
            "scope": self.scope,
            "tool": self.tool,
            "type": self.kind,
            "importability": self.importability,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ContextDiscoveryResult:
    """Complete discovery result for a project."""

    project_root: Path
    detected_target: str
    target_reason: str
    existing_apm_yml: bool
    findings: tuple[ContextDiscoveryFinding, ...]
    proposed_manifest: dict[str, Any]
    migration_plan: tuple[MigrationAction, ...]

    def summary(self) -> dict[str, Any]:
        by_scope = Counter(f.scope for f in self.findings)
        by_importability = Counter(f.importability for f in self.findings)
        return {
            "total_files": len(self.findings),
            "by_scope": dict(sorted(by_scope.items())),
            "by_importability": dict(sorted(by_importability.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": redact_text(str(self.project_root)),
            "existing_apm_yml": self.existing_apm_yml,
            "detected_target": {
                "target": self.detected_target,
                "reason": self.target_reason,
            },
            "summary": self.summary(),
            "files": [finding.to_dict() for finding in self.findings],
            "proposed_apm_yml": self.proposed_manifest,
            "migration_plan": [
                {
                    "source": redact_text(str(portable_relpath(a.source, self.project_root))),
                    "dest": redact_text(str(portable_relpath(a.dest, self.project_root))),
                    "tool": a.tool,
                    "kind": a.kind,
                }
                for a in self.migration_plan
            ],
        }


APM_NATIVE_RULES: tuple[ContextDiscoveryRule, ...] = (
    ContextDiscoveryRule(
        "apm",
        "instruction",
        (
            ".apm/instructions/*.instructions.md",
            ".github/instructions/*.instructions.md",
            "**/*.instructions.md",
        ),
        IMPORT_APM_NATIVE,
        "APM instruction primitive",
    ),
    ContextDiscoveryRule(
        "apm",
        "agent",
        (
            ".apm/agents/*.agent.md",
            ".github/agents/*.agent.md",
            "**/*.agent.md",
        ),
        IMPORT_APM_NATIVE,
        "APM agent primitive",
    ),
    ContextDiscoveryRule(
        "apm",
        "chatmode",
        (
            ".apm/chatmodes/*.chatmode.md",
            ".github/chatmodes/*.chatmode.md",
            "**/*.chatmode.md",
        ),
        IMPORT_APM_NATIVE,
        "APM chatmode primitive",
    ),
    ContextDiscoveryRule(
        "apm",
        "context",
        (
            ".apm/context/*.context.md",
            ".apm/memory/*.memory.md",
            ".github/context/*.context.md",
            ".github/memory/*.memory.md",
            "**/*.context.md",
            "**/*.memory.md",
        ),
        IMPORT_APM_NATIVE,
        "APM context primitive",
    ),
    ContextDiscoveryRule(
        "apm",
        "skill",
        ("SKILL.md", ".apm/skills/*/SKILL.md", ".github/skills/*/SKILL.md"),
        IMPORT_APM_NATIVE,
        "APM skill primitive",
    ),
    ContextDiscoveryRule(
        "apm",
        "hook",
        ("hooks/*.json", ".apm/hooks/*.json"),
        IMPORT_APM_NATIVE,
        "APM hook definition",
    ),
    ContextDiscoveryRule(
        "apm",
        "hook-script",
        (
            "hooks/scripts/**/*.sh",
            "hooks/scripts/**/*.py",
            ".apm/hooks/scripts/**/*.sh",
            ".apm/hooks/scripts/**/*.py",
        ),
        IMPORT_APM_NATIVE,
        "APM hook script",
    ),
    ContextDiscoveryRule(
        "apm",
        "command",
        (".apm/prompts/**/*.prompt.md",),
        IMPORT_APM_NATIVE,
        "APM command prompt",
    ),
    ContextDiscoveryRule(
        "apm",
        "style",
        (".apm/styles/*.style.md", ".github/styles/*.style.md"),
        IMPORT_APM_NATIVE,
        "APM output style primitive",
    ),
)

TOOL_CONTEXT_RULES: tuple[ContextDiscoveryRule, ...] = (
    ContextDiscoveryRule(
        "agents",
        "root-instructions",
        ("AGENTS.md",),
        IMPORT_CONVERTIBLE,
        "standard agent instructions file",
    ),
    ContextDiscoveryRule(
        "claude",
        "root-instructions",
        ("CLAUDE.md", ".claude/CLAUDE.md"),
        IMPORT_CONVERTIBLE,
        "Claude instructions file",
    ),
    ContextDiscoveryRule(
        "gemini",
        "root-instructions",
        ("GEMINI.md", ".gemini/GEMINI.md"),
        IMPORT_CONVERTIBLE,
        "Gemini instructions file",
    ),
    ContextDiscoveryRule(
        "copilot",
        "instruction",
        (".github/copilot-instructions.md",),
        IMPORT_CONVERTIBLE,
        "Copilot instructions file",
    ),
    ContextDiscoveryRule(
        "claude",
        "command",
        (".claude/commands/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Claude command prompt",
    ),
    ContextDiscoveryRule(
        "claude",
        "agent",
        (".claude/agents/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Claude agent file",
    ),
    ContextDiscoveryRule(
        "claude",
        "skill",
        (".claude/skills/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Claude skill file",
    ),
    ContextDiscoveryRule(
        "codex",
        "agent",
        (".codex/agents/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Codex agent file",
    ),
    ContextDiscoveryRule(
        "codex",
        "root-instructions",
        ("CODEX.md", ".codex/CODEX.md"),
        IMPORT_CONVERTIBLE,
        "Codex instructions file",
    ),
    ContextDiscoveryRule(
        "codex",
        "skill",
        (".codex/skills/**/SKILL.md",),
        IMPORT_CONVERTIBLE,
        "Codex skill file",
    ),
    ContextDiscoveryRule(
        "codex",
        "instruction",
        (".codex/instructions/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Codex instruction file",
    ),
    ContextDiscoveryRule(
        "copilot",
        "skill",
        (".github/skills/**/SKILL.md",),
        IMPORT_CONVERTIBLE,
        "Copilot skill file",
    ),
    ContextDiscoveryRule(
        "copilot",
        "agent",
        (".github/agents/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Copilot agent file",
    ),
    ContextDiscoveryRule(
        "agent-skills",
        "skill",
        (".agents/skills/**/SKILL.md",),
        IMPORT_CONVERTIBLE,
        "shared agent skill file",
    ),
    ContextDiscoveryRule(
        "cursor",
        "rule",
        (".cursor/rules/**/*.md", ".cursorrules"),
        IMPORT_CONVERTIBLE,
        "Cursor rule file",
    ),
    ContextDiscoveryRule(
        "cursor",
        "agent",
        (".cursor/agents/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Cursor agent file",
    ),
    ContextDiscoveryRule(
        "cursor",
        "skill",
        (".cursor/skills/**/SKILL.md",),
        IMPORT_CONVERTIBLE,
        "Cursor skill file",
    ),
    ContextDiscoveryRule(
        "opencode",
        "agent",
        (".opencode/agents/**/*.md",),
        IMPORT_CONVERTIBLE,
        "OpenCode agent file",
    ),
    ContextDiscoveryRule(
        "opencode",
        "command",
        (".opencode/commands/**/*.md",),
        IMPORT_CONVERTIBLE,
        "OpenCode command prompt",
    ),
    ContextDiscoveryRule(
        "opencode",
        "skill",
        (".opencode/skills/**/SKILL.md",),
        IMPORT_CONVERTIBLE,
        "OpenCode skill file",
    ),
    ContextDiscoveryRule(
        "gemini",
        "command",
        (".gemini/commands/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Gemini command prompt",
    ),
    ContextDiscoveryRule(
        "gemini",
        "skill",
        (".gemini/skills/**/SKILL.md",),
        IMPORT_CONVERTIBLE,
        "Gemini skill file",
    ),
    ContextDiscoveryRule(
        "windsurf",
        "rule",
        (".windsurf/rules/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Windsurf rule file",
    ),
    ContextDiscoveryRule(
        "windsurf",
        "skill",
        (".windsurf/skills/**/SKILL.md",),
        IMPORT_CONVERTIBLE,
        "Windsurf skill file",
    ),
    ContextDiscoveryRule(
        "windsurf",
        "workflow",
        (".windsurf/workflows/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Windsurf workflow file",
    ),
    # --- harness: commands (executable prompts / slash commands) ---
    ContextDiscoveryRule(
        "copilot",
        "command",
        (".github/prompts/**/*.prompt.md",),
        IMPORT_CONVERTIBLE,
        "Copilot custom prompt",
    ),
    ContextDiscoveryRule(
        "codex",
        "command",
        (".codex/commands/**/*.md",),
        IMPORT_CONVERTIBLE,
        "Codex command prompt",
    ),
    # --- harness: hooks (pre/post tool-use event handlers) ---
    ContextDiscoveryRule(
        "copilot",
        "hook",
        (".github/hooks/*.json",),
        IMPORT_CONVERTIBLE,
        "Copilot hook definition",
    ),
    ContextDiscoveryRule(
        "copilot",
        "hook-script",
        (
            ".github/hooks/scripts/**/*.sh",
            ".github/hooks/scripts/**/*.py",
        ),
        IMPORT_CONVERTIBLE,
        "Copilot hook script",
    ),
    ContextDiscoveryRule(
        "claude",
        "hook-script",
        (
            ".claude/hooks/scripts/**/*.sh",
            ".claude/hooks/scripts/**/*.py",
        ),
        IMPORT_CONVERTIBLE,
        "Claude hook script",
    ),
    # --- harness: styles (output/response style definitions) ---
    ContextDiscoveryRule(
        "agents",
        "style",
        ("STYLE.md",),
        IMPORT_CONVERTIBLE,
        "project output style guide",
    ),
    ContextDiscoveryRule(
        "mcp",
        "config",
        (".mcp.json", ".lsp.json"),
        IMPORT_REFERENCE_ONLY,
        "tool configuration; review before translating to apm.yml dependencies",
    ),
    ContextDiscoveryRule(
        "claude",
        "settings",
        (".claude/settings.json", ".claude/settings.local.json"),
        IMPORT_REFERENCE_ONLY,
        "Claude settings file",
    ),
    ContextDiscoveryRule(
        "codex",
        "settings",
        (".codex/config.toml", ".codex/hooks.json"),
        IMPORT_REFERENCE_ONLY,
        "Codex configuration file",
    ),
    ContextDiscoveryRule(
        "cursor",
        "settings",
        (".cursor/settings.json", ".cursor/hooks.json"),
        IMPORT_REFERENCE_ONLY,
        "Cursor configuration file",
    ),
    ContextDiscoveryRule(
        "opencode",
        "settings",
        (".opencode/opencode.json", ".opencode/hooks.json"),
        IMPORT_REFERENCE_ONLY,
        "OpenCode configuration file",
    ),
    ContextDiscoveryRule(
        "gemini",
        "settings",
        (".gemini/settings.json",),
        IMPORT_REFERENCE_ONLY,
        "Gemini settings file",
    ),
    ContextDiscoveryRule(
        "windsurf",
        "settings",
        (".windsurf/hooks.json",),
        IMPORT_REFERENCE_ONLY,
        "Windsurf hooks file",
    ),
)

PROJECT_RULES = APM_NATIVE_RULES + TOOL_CONTEXT_RULES

USER_RULES: tuple[ContextDiscoveryRule, ...] = tuple(
    ContextDiscoveryRule(
        rule.tool,
        rule.kind,
        rule.patterns,
        rule.importability,
        rule.reason,
    )
    for rule in TOOL_CONTEXT_RULES
)

SYSTEM_RULES: tuple[ContextDiscoveryRule, ...] = (
    ContextDiscoveryRule(
        "apm",
        "instruction",
        ("apm/AGENTS.md", "apm/instructions/**/*.md"),
        IMPORT_REFERENCE_ONLY,
        "system-level APM context location",
    ),
    ContextDiscoveryRule(
        "codex",
        "settings",
        ("codex/config.toml",),
        IMPORT_REFERENCE_ONLY,
        "system-level Codex configuration",
    ),
    ContextDiscoveryRule(
        "gemini",
        "settings",
        ("gemini/settings.json",),
        IMPORT_REFERENCE_ONLY,
        "system-level Gemini configuration",
    ),
)


def discover_agent_context(
    project_root: Path,
    config: dict[str, Any],
    *,
    home_dir: Path | None = None,
    system_dirs: tuple[Path, ...] | None = None,
) -> ContextDiscoveryResult:
    """Discover known agent context files and build an ``apm.yml`` proposal."""

    project_root = project_root.resolve()
    home_dir = Path.home() if home_dir is None else home_dir
    system_dirs = _default_system_dirs() if system_dirs is None else system_dirs

    findings: list[ContextDiscoveryFinding] = []
    findings.extend(_scan_scope(project_root, "project", PROJECT_RULES, project_root, home_dir))

    if home_dir.exists():
        findings.extend(_scan_scope(home_dir, "user", USER_RULES, project_root, home_dir))

    for system_dir in system_dirs:
        if system_dir.exists():
            findings.extend(_scan_scope(system_dir, "system", SYSTEM_RULES, project_root, home_dir))

    target, target_reason = detect_target(project_root)
    existing_manifest = _load_existing_manifest(project_root)
    proposed_manifest = build_proposed_manifest(
        config,
        project_root,
        detected_target=target,
        existing_manifest=existing_manifest,
        findings=tuple(findings),
    )

    sorted_findings = tuple(sorted(findings, key=_finding_sort_key))
    migration_plan = tuple(compute_migration_plan(sorted_findings, project_root))

    return ContextDiscoveryResult(
        project_root=project_root,
        detected_target=target,
        target_reason=target_reason,
        existing_apm_yml=existing_manifest is not None,
        findings=sorted_findings,
        proposed_manifest=proposed_manifest,
        migration_plan=migration_plan,
    )


def build_proposed_manifest(
    config: dict[str, Any],
    project_root: Path,
    *,
    detected_target: str | None = None,
    existing_manifest: dict[str, Any] | None = None,
    findings: tuple[ContextDiscoveryFinding, ...] = (),
) -> dict[str, Any]:
    """Build a conservative manifest proposal, preserving existing fields."""

    manifest: dict[str, Any] = dict(existing_manifest or {})
    manifest.setdefault("name", config["name"])
    manifest.setdefault("version", config["version"])
    manifest.setdefault("description", config["description"])
    manifest.setdefault("author", config["author"])

    dependencies = manifest.get("dependencies")
    dependencies = {} if not isinstance(dependencies, dict) else dict(dependencies)
    dependencies.setdefault("apm", [])
    dependencies.setdefault("mcp", [])
    manifest["dependencies"] = dependencies

    # Collect root-level dirs that hold APM-native or convertible project-scope
    # files. .apm/ is the canonical location and always scanned -- keep it
    # implicit. Every other dir (e.g. .github/, .claude/, .codex/) is listed
    # explicitly so users can see at a glance where their agent context lives.
    _implicit_prefixes = (".apm/",)
    extra_dirs: list[str] = sorted(
        {
            f.display_path.split("/")[0]
            for f in findings
            if f.scope == "project"
            and f.importability in (IMPORT_APM_NATIVE, IMPORT_CONVERTIBLE)
            and "/" in f.display_path
            and not any(f.display_path.startswith(p) for p in _implicit_prefixes)
        }
    )
    existing_includes = manifest.get("includes")
    if isinstance(existing_includes, list):
        # Merge new dirs into the existing list, preserving order and deduping.
        merged = list(existing_includes)
        for d in extra_dirs:
            if d not in merged:
                merged.append(d)
        manifest["includes"] = merged
    elif extra_dirs:
        manifest["includes"] = extra_dirs
    else:
        manifest.setdefault("includes", "auto")

    manifest.setdefault("scripts", {})

    target = detected_target
    if target is None:
        target, _reason = detect_target(project_root)
    user_target = _manifest_target(target)
    if user_target and "target" not in manifest:
        manifest["target"] = user_target

    return manifest


def format_discovery_result(
    result: ContextDiscoveryResult, output_format: str, *, write: bool = False
) -> str:
    """Format a discovery result for CLI output."""

    if output_format == "json":
        return json.dumps(result.to_dict(), indent=2) + "\n"
    if output_format == "yaml":
        return yaml_to_str(result.to_dict())
    return _format_text_result(result, write=write)


def write_proposed_manifest(result: ContextDiscoveryResult, path: Path) -> None:
    """Write the proposed manifest to ``path``."""

    from .utils.yaml_io import dump_yaml

    dump_yaml(result.proposed_manifest, path)


def redact_text(value: str) -> str:
    """Redact token-like strings and URL credentials from diagnostic output."""

    value = _URL_USERINFO_RE.sub(r"\1\2", value)
    return _TOKEN_RE.sub("<redacted-token>", value)


def _format_text_result(result: ContextDiscoveryResult, *, write: bool = False) -> str:
    lines = [
        "Agent context discovery preview",
        "",
        f"Project: {redact_text(str(result.project_root))}",
        f"Existing apm.yml: {'yes' if result.existing_apm_yml else 'no'}",
        f"Detected target: {result.detected_target} ({result.target_reason})",
        "",
    ]

    summary = result.summary()
    lines.append(f"Found {summary['total_files']} context/config file(s).")
    if result.findings:
        lines.extend(_format_findings_table(result.findings))
    else:
        lines.append("No known agent context files found.")

    lines.extend(
        [
            "",
            "Proposed apm.yml:",
            yaml_to_str(result.proposed_manifest).rstrip(),
        ]
    )

    # Migration plan section
    if result.migration_plan:
        lines.append("")
        if write:
            lines.append(f"Migrating {len(result.migration_plan)} convertible file(s) to .apm/:")
        else:
            lines.append(
                f"Migration plan ({len(result.migration_plan)} convertible file(s) -> .apm/):"
            )
        lines.extend(_format_migration_table(result.migration_plan, result.project_root))
        if not write:
            lines.append("  Re-run with --write to migrate files and create apm.yml.")
    else:
        lines.append("")
        lines.append(
            "Writing proposed apm.yml."
            if write
            else "Preview only. Re-run with --write to create or update apm.yml."
        )

    return "\n".join(lines) + "\n"


def _format_migration_table(actions: tuple[MigrationAction, ...], project_root: Path) -> list[str]:
    rows = [
        (
            str(portable_relpath(a.source, project_root)),
            "->",
            str(portable_relpath(a.dest, project_root)),
        )
        for a in actions
    ]
    if not rows:
        return []
    src_w = max(len(r[0]) for r in rows)
    lines = []
    for src, arrow, dst in rows:
        lines.append(f"  {src.ljust(src_w)}  {arrow}  {dst}")
    return lines


def _format_findings_table(findings: tuple[ContextDiscoveryFinding, ...]) -> list[str]:
    headers = ("Scope", "Tool", "Type", "Importability", "Path")
    rows = [
        (
            finding.scope,
            finding.tool,
            finding.kind,
            finding.importability,
            finding.display_path,
        )
        for finding in findings
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    lines = [
        "",
        "  ".join(headers[index].ljust(widths[index]) for index in range(len(headers))),
        "  ".join("-" * widths[index] for index in range(len(headers))),
    ]
    for row in rows:
        lines.append("  ".join(row[index].ljust(widths[index]) for index in range(len(row))))
    return lines


def _scan_scope(
    base_dir: Path,
    scope: str,
    rules: tuple[ContextDiscoveryRule, ...],
    project_root: Path,
    home_dir: Path,
) -> list[ContextDiscoveryFinding]:
    findings: list[ContextDiscoveryFinding] = []
    seen: set[Path] = set()
    candidate_files = (
        _iter_candidate_files(base_dir)
        if scope == "project"
        else _iter_rule_candidate_files(base_dir, rules)
    )

    for file_path in candidate_files:
        rel_path = portable_relpath(file_path, base_dir)
        rule = _matching_rule(rel_path, rules)
        if rule is None:
            continue
        resolved = file_path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_binary(file_path):
            findings.append(
                ContextDiscoveryFinding(
                    path=file_path,
                    display_path=_display_path(file_path, scope, project_root, home_dir),
                    scope=scope,
                    tool=rule.tool,
                    kind=rule.kind,
                    importability=IMPORT_IGNORED,
                    reason="binary file ignored",
                )
            )
            continue
        findings.append(
            ContextDiscoveryFinding(
                path=file_path,
                display_path=_display_path(file_path, scope, project_root, home_dir),
                scope=scope,
                tool=rule.tool,
                kind=rule.kind,
                importability=rule.importability,
                reason=rule.reason,
            )
        )
    return findings


def _iter_candidate_files(base_dir: Path) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(base_dir):
        current = Path(root)
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in DEFAULT_SKIP_DIRS and not (current / name).is_symlink()
        )
        for name in sorted(names):
            file_path = current / name
            if file_path.is_file() and not file_path.is_symlink():
                files.append(file_path)
    return files


def _iter_rule_candidate_files(
    base_dir: Path, rules: tuple[ContextDiscoveryRule, ...]
) -> list[Path]:
    """Find files for user/system scopes without walking arbitrary trees."""

    files: set[Path] = set()
    for rule in rules:
        for pattern in rule.patterns:
            for file_path in base_dir.glob(pattern):
                if _has_skipped_part(file_path, base_dir):
                    continue
                if file_path.is_file() and not file_path.is_symlink():
                    files.add(file_path)
    return sorted(files)


def _has_skipped_part(file_path: Path, base_dir: Path) -> bool:
    try:
        rel_parts = file_path.relative_to(base_dir).parts
    except ValueError:
        return True
    return any(part in DEFAULT_SKIP_DIRS for part in rel_parts)


def _matching_rule(
    rel_path: str, rules: tuple[ContextDiscoveryRule, ...]
) -> ContextDiscoveryRule | None:
    normalized = rel_path.replace("\\", "/")
    for rule in rules:
        if any(_glob_match(normalized, pattern) for pattern in rule.patterns):
            return rule
    return None


def _display_path(file_path: Path, scope: str, project_root: Path, home_dir: Path) -> str:
    if scope == "project":
        return redact_text(portable_relpath(file_path, project_root))
    if scope == "user":
        try:
            rel = portable_relpath(file_path, home_dir)
            return redact_text(f"~/{rel}")
        except ValueError:
            return redact_text(str(file_path))
    return redact_text(str(file_path))


def _finding_sort_key(finding: ContextDiscoveryFinding) -> tuple[int, str, int, str]:
    scope_order = {"project": 0, "user": 1, "system": 2}
    import_order = {
        IMPORT_APM_NATIVE: 0,
        IMPORT_CONVERTIBLE: 1,
        IMPORT_REFERENCE_ONLY: 2,
        IMPORT_IGNORED: 3,
    }
    return (
        scope_order.get(finding.scope, 99),
        finding.tool,
        import_order.get(finding.importability, 99),
        finding.display_path,
    )


def _is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:2048]
    except OSError:
        return False
    return b"\0" in chunk


def _load_existing_manifest(project_root: Path) -> dict[str, Any] | None:
    path = project_root / APM_YML
    if not path.exists():
        return None
    try:
        data = load_yaml(path)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _manifest_target(target: str) -> str | None:
    if target == "minimal":
        return None
    if target == "vscode":
        return "copilot"
    return target


def _default_system_dirs() -> tuple[Path, ...]:
    raw = os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg")
    paths = []
    for item in raw.split(":"):
        if not item:
            continue
        path = Path(item)
        if path.is_absolute():
            paths.append(path)
    return tuple(paths)


def echo_discovery_result(
    result: ContextDiscoveryResult, output_format: str, *, write: bool = False
) -> None:
    """Emit discovery output through Click."""

    click.echo(format_discovery_result(result, output_format, write=write), nl=False)
